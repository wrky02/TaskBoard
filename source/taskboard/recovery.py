"""Durable local recovery, immutable task history and scheduled verified backups.

The worker calls ``tick`` from its normal idle/heartbeat path. Heavy snapshot,
hash and ZIP work runs on a dedicated connection, never the UI's connection.
Attachment deletion is logical: physical cleanup is deliberately explicit.
"""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile

JOURNAL = '.recovery-operation.json'
COMPONENTS = ('tasks.sqlite3', 'attachments', 'history-objects', 'staging', 'vault.json')
DEFAULT_CONFIG = dict(enabled=True, target='', interval_minutes=15,
                      retain_daily=7, retain_weekly=4, retain_monthly=12,
                      recent_hours=24)
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='taskboard-backup')
_JOBS = {}
_LOCK = threading.RLock()


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _sync_directory(path):
    if os.name == 'nt':
        return  # Windows rename durability is exercised by the installed tests.
    descriptor = os.open(path, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _replace_with_retry(source, destination):
    """Retry transient Windows sharing violations during an atomic generation swap."""
    deadline = time.monotonic() + 3.0
    delay = 0.025
    while True:
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                if getattr(exc, 'winerror', None) in (5, 32, 33):
                    owners = []
                    try:
                        import psutil
                        expected = os.path.normcase(os.path.abspath(os.fspath(source)))
                        for process in psutil.process_iter(['pid', 'name']):
                            try:
                                if any(os.path.normcase(os.path.abspath(item.path)) == expected
                                       for item in process.open_files()):
                                    owners.append(f"{process.info['name']} ({process.info['pid']})")
                            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                                continue
                    except ImportError:
                        pass
                    if owners:
                        raise PermissionError(exc.errno, f'{exc.strerror}; file handle held by {", ".join(owners)}',
                                              exc.filename, exc.winerror, exc.filename2) from exc
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def durable_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path); _sync_directory(path.parent)
    finally: temporary.unlink(missing_ok=True)


def _checkpoint(name):
    """Process-level fault injection, only enabled explicitly by a test process."""
    if os.environ.get('TASKBOARD_RECOVERY_CRASH_AT') == name:
        os._exit(86)


def _safe_child(root, name):
    if not isinstance(name, str) or not name or Path(name).name != name or name in ('.', '..'):
        raise ValueError('恢复日志包含无效路径')
    child = root / name
    if child.is_symlink(): raise ValueError('恢复路径不能为符号链接')
    return child


def recover_interrupted(root):
    """Rollback an uncommitted generation before opening/creating any database.

    No user file is deleted: displaced partial files go into the operation's
    interrupted directory. A committed generation keeps its predecessor.
    """
    root = Path(root); path = root / JOURNAL
    if not path.exists(): return None
    operation = json.loads(path.read_text(encoding='utf-8'))
    if operation.get('format') != 1: raise ValueError('不支持的恢复日志版本，请保留数据目录')
    staging = _safe_child(root, operation['staging'])
    previous = _safe_child(root, operation['previous'])
    interrupted = _safe_child(root, operation['interrupted'])
    names = operation.get('components', [])
    if not names or any(name not in COMPONENTS for name in names):
        raise ValueError('恢复日志组件无效')
    if operation.get('phase') != 'committed':
        interrupted.mkdir(exist_ok=True)
        for name in names:
            target, old, pending = root/name, previous/name, staging/name
            had_old = bool(operation['had_old'].get(name))
            if old.exists():
                if target.exists():
                    displaced = interrupted/name
                    if displaced.exists(): raise ValueError('恢复现场存在冲突，未覆盖任何文件')
                    _replace_with_retry(target, displaced)
                _replace_with_retry(old, target)
            elif had_old:
                # A target without its predecessor was untouched, or an earlier
                # recovery already rolled this component back before interruption.
                if not target.exists():
                    raise ValueError('旧数据副本不完整，已停止自动恢复；请保留整个数据目录')
            elif target.exists() and not pending.exists():
                displaced = interrupted/name
                if displaced.exists(): raise ValueError('恢复现场存在冲突，未覆盖任何文件')
                _replace_with_retry(target, displaced)
        operation['phase'] = 'rolled-back'
    operation['recovered_at'] = utcnow()
    records = root/'recovery-records'; records.mkdir(exist_ok=True)
    durable_json(records/(operation['id']+'.json'), operation)
    path.unlink(); _sync_directory(root)
    return operation


def replace_generation(root, staging, kind='restore'):
    """Install a fully validated staged generation; callers close DB handles first."""
    root, staging = Path(root), Path(staging)
    if staging.parent.resolve() != root.resolve(): raise ValueError('暂存目录必须位于工作区内')
    recover_interrupted(root)
    token = uuid.uuid4().hex
    previous = root/('previous-generation-'+token)
    previous.mkdir()
    names = [name for name in COMPONENTS if (staging/name).exists() or (root/name).exists()]
    # Absence of a vault file is an intentional component when restoring plaintext.
    operation = dict(format=1,id=token,kind=kind,phase='prepared',created_at=utcnow(),
                     staging=staging.name, previous=previous.name,
                     interrupted='interrupted-generation-'+token, components=names,
                     had_old={name:(root/name).exists() for name in names})
    durable_json(root/JOURNAL, operation); _checkpoint('prepared')
    try:
        for name in names:
            target = root/name
            if target.exists(): _replace_with_retry(target, previous/name)
            _sync_directory(root); _checkpoint('old-'+name)
            if (staging/name).exists(): _replace_with_retry(staging/name, target)
            _sync_directory(root); _checkpoint('new-'+name)
        operation['phase']='committed'; durable_json(root/JOURNAL, operation)
        _checkpoint('committed')
        recover_interrupted(root)
        if staging.exists() and not any(staging.iterdir()): staging.rmdir()
        return previous
    except BaseException:
        recover_interrupted(root)
        raise


def ensure_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS task_history(id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL,created_at TEXT NOT NULL,reason TEXT NOT NULL,title TEXT NOT NULL,digest TEXT NOT NULL,data TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_task_history_task ON task_history(task_id,id DESC)')
    db.execute('CREATE TABLE IF NOT EXISTS history_objects(sha256 TEXT PRIMARY KEY,bytes INTEGER NOT NULL,created_at TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS history_references(version_id INTEGER REFERENCES task_history(id) ON DELETE CASCADE,sha256 TEXT NOT NULL REFERENCES history_objects(sha256),stored_name TEXT NOT NULL,PRIMARY KEY(version_id,stored_name))')
    db.execute('CREATE TABLE IF NOT EXISTS retained_files(stored_name TEXT PRIMARY KEY,retained_at TEXT NOT NULL)')


def migrate(db):
    with db: ensure_schema(db)


def _table_exists(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _rows(db, table, tid, order='id'):
    if not _table_exists(db, table): return []
    return [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE task_id=? ORDER BY {order}', (tid,))]


def task_snapshot(db, tid):
    row = db.execute('SELECT * FROM tasks WHERE id=?',(int(tid),)).fetchone()
    if not row: raise ValueError('任务不存在')
    data = dict(task=dict(row), tags=_rows(db,'task_tags',tid,'tag_id'),
                checklist=_rows(db,'checklist_items',tid,'sort_order,id'),
                milestones=_rows(db,'milestones',tid,'sort_order,id'),
                dependencies=_rows(db,'task_dependencies',tid,'prerequisite'),
                files=_rows(db,'attachments',tid))
    options = db.execute('SELECT data FROM task_options WHERE task_id=?',(tid,)).fetchone() if _table_exists(db,'task_options') else None
    data['options'] = json.loads(options[0]) if options else {}
    if _table_exists(db,'pro_tasks'):
        import pro_features as pro
        data['pro']=pro.enrich_snapshot(db,{'tasks':[dict(row)],'archived':[],'trash':[]})['tasks'][0].get('pro',{})
    if _table_exists(db,'attachment_versions'):
        versions={r['file_id']:dict(r) for r in db.execute('SELECT v.* FROM attachment_versions v JOIN attachments a ON a.id=v.file_id WHERE a.task_id=?',(tid,))}
        for file in data['files']:
            if file['id'] in versions:file['version']=versions[file['id']]
    return data


def _read_file(path):
    try:
        import vault
        return vault.read_bytes(path)
    except ImportError: return Path(path).read_bytes()


def _write_file(path, data):
    try:
        import vault
        vault.write_bytes(path, data)
    except ImportError:
        with Path(path).open('wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())


def history_record(db, task_id, reason='save'):
    """Called inside the caller's mutation transaction, before and/or after save."""
    import storage as s
    ensure_schema(db)
    snapshot = task_snapshot(db, task_id)
    folder=s.ROOT/'history-objects'; folder.mkdir(exist_ok=True)
    for file in snapshot['files']:
        source=s.FILES/file['stored_name']
        if not source.is_file(): raise ValueError('历史保存失败：附件文件缺失')
        content=_read_file(source); digest=hashlib.sha256(content).hexdigest()
        destination=folder/digest
        if not destination.exists():
            # The vault binds ciphertext to its final relative path and performs
            # its own atomic temporary write. Renaming encrypted paths breaks AAD.
            _write_file(destination,content); _sync_directory(folder)
        elif hashlib.sha256(_read_file(destination)).hexdigest()!=digest:
            raise ValueError('历史附件校验失败，未覆盖原文件')
        file['sha256']=digest; file['bytes']=len(content)
        db.execute('INSERT OR IGNORE INTO history_objects VALUES (?,?,?)',(digest,len(content),utcnow()))
    encoded=json.dumps(snapshot,ensure_ascii=False,sort_keys=True)
    digest=hashlib.sha256(encoded.encode()).hexdigest()
    previous=db.execute('SELECT id,digest FROM task_history WHERE task_id=? ORDER BY id DESC LIMIT 1',(task_id,)).fetchone()
    if previous and previous['digest']==digest:return previous['id']
    version=db.execute('INSERT INTO task_history(task_id,created_at,reason,title,digest,data) VALUES (?,?,?,?,?,?)',
                       (task_id,utcnow(),str(reason)[:60],snapshot['task']['title'],digest,encoded)).lastrowid
    db.executemany('INSERT INTO history_references VALUES (?,?,?)',[(version,f['sha256'],f['stored_name']) for f in snapshot['files']])
    return version


def retain_removed_file(db, stored_name):
    owns_transaction=not db.in_transaction
    ensure_schema(db)
    db.execute('INSERT OR IGNORE INTO retained_files VALUES (?,?)',(stored_name,utcnow()))
    if owns_transaction:db.commit()
    return True


def history_list(db, task_id):
    ensure_schema(db)
    return [dict(r) for r in db.execute('SELECT h.id,h.task_id,h.created_at,h.reason,h.title,(SELECT COUNT(*) FROM history_references r WHERE r.version_id=h.id) AS files FROM task_history h WHERE task_id=? ORDER BY id DESC',(int(task_id),))]


def _history(db, version_id):
    row=db.execute('SELECT * FROM task_history WHERE id=?',(int(version_id),)).fetchone()
    if not row:raise ValueError('历史版本不存在')
    value=json.loads(row['data'])
    if hashlib.sha256(row['data'].encode()).hexdigest()!=row['digest']:raise ValueError('历史版本校验失败')
    return dict(row),value


def history_diff(db, version_id, other_version_id=None):
    record, before=_history(db,version_id)
    if other_version_id:
        other,after=_history(db,other_version_id)
        if other['task_id']!=record['task_id']:raise ValueError('只能比较同一任务的版本')
    else:
        try:after=task_snapshot(db,record['task_id'])
        except ValueError:after={}
    changes=[]
    labels={'title':'标题','body':'说明','status':'状态','due_date':'截止日期','project_id':'项目','parent_id':'父任务','checklist':'检查清单','milestones':'里程碑','dependencies':'依赖','tags':'标签','options':'优先级与计划','pro':'项目、子任务与计划','files':'附件'}
    for key in sorted(set(before.get('task',{}))|set(after.get('task',{}))):
        if key in ('created_at','updated_at','sort_order'):continue
        a,b=before.get('task',{}).get(key),after.get('task',{}).get(key)
        if a!=b:changes.append(dict(field=key,label=labels.get(key,key),before=a,after=b))
    for key in ('checklist','milestones','dependencies','tags','options','pro','files'):
        a,b=before.get(key),after.get(key)
        if key=='files':
            a=[{k:v for k,v in f.items() if k not in ('sha256','bytes')} for f in a or []]
            b=[{k:v for k,v in f.items() if k not in ('sha256','bytes')} for f in b or []]
        if a!=b:changes.append(dict(field=key,label=labels[key],before=a,after=b))
    record.pop('data');record.pop('digest')
    return dict(version=record,changes=changes)


def history_restore_payload(db, version_id, as_new=False):
    import storage as s
    record,snapshot=_history(db,version_id);old=snapshot['task']
    current=s.task(db,record['task_id'],True)
    if not as_new and (not current or current['deleted_at'] or current['archived_at']):
        raise ValueError('原任务不在可编辑看板，请先恢复/取消归档，或恢复为新任务')
    payload=dict(old,title=old['title'],note=old['body'],due=old['due_date'],
                 options=snapshot['options'],tags=[v['tag_id'] for v in snapshot['tags']],
                 checklist=snapshot['checklist'],milestones=snapshot['milestones'],
                 dependencies=[v['prerequisite'] for v in snapshot['dependencies']],new_files=[])
    payload['removed_files']=[r['id'] for r in s.attachments(db,record['task_id'])] if not as_new else []
    payload['file_versions']=[]
    if snapshot.get('pro'):
        import pro_features as pro
        payload['pro']={k:v for k,v in snapshot['pro'].items() if k in pro.META_FIELDS or k=='related_ids'}
        payload['pro']['series_scope']='current'
        payload['pro']['reminders']=[]
        for reminder in snapshot['pro'].get('reminders',[]):
            at=datetime.fromisoformat(reminder['at'].replace('Z','+00:00'))
            state=reminder.get('status','pending')
            if at.astimezone(timezone.utc)<datetime.now(timezone.utc) and state=='pending':state='ack'
            payload['pro']['reminders'].append(dict(at=reminder['at'],status=state))
    if as_new:
        payload.pop('id',None);payload.pop('updated_at',None)
        payload['options']={**payload['options'],'repeat':'off','snooze':0,'acknowledged':''}
    else:payload['id']=record['task_id'];payload['updated_at']=current['updated_at']
    families={}
    for file in snapshot['files']:
        content=_read_file(s.ROOT/'history-objects'/file['sha256'])
        if hashlib.sha256(content).hexdigest()!=file['sha256']:raise ValueError('历史附件缺失或校验失败')
        entry=dict(name=file['filename'],data=base64.b64encode(content).decode())
        version=file.get('version',{})
        family=version.get('family',file['stored_name'])
        entry['history_version']=dict(family=families.setdefault(family,uuid.uuid4().hex),
                                      current=version.get('current',1),
                                      version_label=file.get('version_label','初稿'),
                                      version_note=file.get('version_note',''))
        payload['new_files'].append(entry)
    return payload


def config(db):
    row=db.execute("SELECT value FROM settings WHERE key='recovery_config'").fetchone()
    return {**DEFAULT_CONFIG,**(json.loads(row[0]) if row else {})}


def configure(db, options):
    value={**config(db),**{k:v for k,v in options.items() if k in DEFAULT_CONFIG}}
    value['enabled']=bool(value['enabled']);value['target']=str(value['target']).strip()
    for name,minimum,maximum in [('interval_minutes',1,1440),('retain_daily',1,365),('retain_weekly',0,104),('retain_monthly',0,120),('recent_hours',1,168)]:
        value[name]=int(value[name])
        if not minimum<=value[name]<=maximum:raise ValueError('备份配置超出支持范围：'+name)
    if value['target'] and not Path(value['target']).is_absolute():raise ValueError('备份位置必须为绝对路径')
    with db:db.execute("INSERT OR REPLACE INTO settings VALUES ('recovery_config',?)",(json.dumps(value),))
    return status(db)


def status(db):
    import storage as s
    setting=config(db);target=Path(setting['target']) if setting['target'] else s.ROOT/'backups'
    row=db.execute("SELECT value FROM settings WHERE key='recovery_last'").fetchone()
    last=json.loads(row[0]) if row else {}
    with _LOCK:
        job=_JOBS.get(str(s.ROOT.resolve()),{})
        public={k:v for k,v in job.items() if k in ('id','stage','started_at','error','finished_at','destination')}
    return dict(config=setting,target=str(target),target_available=target.is_dir(),last=last,job=public,
                protection='local' if not setting['target'] else 'selected-location',
                history_versions=db.execute('SELECT COUNT(*) FROM task_history').fetchone()[0] if _table_exists(db,'task_history') else 0)


def _backup_job(root, target, job):
    import storage as s
    root,target=Path(root),Path(target)
    job['stage']='snapshot'
    if not target.exists():
        if target==root/'backups':target.mkdir(parents=True,exist_ok=True)
        else:raise ValueError('备份目标不可用，请连接该磁盘或重新选择位置')
    destination=target/('recovery-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+job['id'][:8]+'.zip')
    try:
        import vault
        connection=vault.connection(root,readonly=True)
    except ImportError:connection=sqlite3.connect((root/'tasks.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    try:
        job['stage']='copy-and-verify'
        s.backup(connection,destination)
    finally:connection.close()
    job.update(stage='complete',finished_at=utcnow(),destination=str(destination))
    return dict(path=str(destination),snapshot_started_at=job['started_at'],completed_at=job['finished_at'],verified=True)


def _retention(target, settings):
    """Prune only this scheduler's verified archives after a newer success."""
    candidates=sorted(Path(target).glob('recovery-*.zip'),reverse=True)
    keep=set();days=set();weeks=set();months=set();cutoff=time.time()-settings['recent_hours']*3600
    for item in candidates:
        stamp=datetime.fromtimestamp(item.stat().st_mtime,timezone.utc)
        if item.stat().st_mtime>=cutoff:keep.add(item)
        for seen,key,count in [(days,stamp.strftime('%Y-%m-%d'),settings['retain_daily']),
                               (weeks,stamp.strftime('%G-%V'),settings['retain_weekly']),
                               (months,stamp.strftime('%Y-%m'),settings['retain_monthly'])]:
            if key not in seen and len(seen)<count:seen.add(key);keep.add(item)
    # Retired recovery points remain recoverable in an explicit folder. Permanent
    # cleanup is a separate, user-reviewed action, never implicit data deletion.
    retired=Path(target)/'retired-recovery-points'
    for item in candidates:
        if item not in keep:
            retired.mkdir(exist_ok=True);os.replace(item,retired/item.name)


def tick(db, now=None, force=False):
    import storage as s
    key=str(s.ROOT.resolve());now=time.time() if now is None else float(now)
    settings=config(db)
    with _LOCK:
        job=_JOBS.get(key)
        if job and job.get('future'):
            if not job['future'].done():return status(db)
            future=job.pop('future')
            try:
                result=future.result()
                before=db.total_changes
                with db:db.execute("INSERT OR REPLACE INTO settings VALUES ('recovery_last',?)",(json.dumps(result),))
                _retention(Path(result['path']).parent,settings)
                job['last_changes']=job['source_changes']+(db.total_changes-before)
            except Exception as exc:job.update(stage='failed',error=str(exc),finished_at=utcnow())
            job['next_at']=now+settings['interval_minutes']*60
        if not force and (not settings['enabled'] or (job and now<job.get('next_at',now+1))):return status(db)
        if not force and job and job.get('last_changes')==db.total_changes:
            job['next_at']=now+settings['interval_minutes']*60;return status(db)
        target=Path(settings['target']) if settings['target'] else s.ROOT/'backups'
        job=dict(id=uuid.uuid4().hex,stage='queued',started_at=utcnow(),next_at=now+settings['interval_minutes']*60,source_changes=db.total_changes)
        _JOBS[key]=job
        job['future']=_EXECUTOR.submit(_backup_job,s.ROOT,target,job)
    return status(db)


def wait_for_jobs(root=None, timeout=30):
    """Shutdown/restore barrier; caller must not move a live backup's source tree."""
    with _LOCK:
        jobs=list(_JOBS.items())
    for key,job in jobs:
        if root is None or Path(key)==Path(root).resolve():
            future=job.get('future')
            if future:
                try:future.result(timeout=timeout)
                except TimeoutError:raise
                except Exception:pass  # Completed failures are reported by tick.


def export_content(db, destination):
    """Portable, full-fidelity public content; excludes settings and credentials."""
    import storage as s
    forbidden={'settings','drafts','undo_actions','sqlite_sequence','retained_files'}
    tables={}
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        name=row[0]
        if name in forbidden or not name.replace('_','').isalnum():continue
        tables[name]=[dict(r) for r in db.execute('SELECT * FROM "'+name+'"')]
    data=dict(format='taskboard-content',format_version=1,exported_at=utcnow(),tables=tables,
              excluded=['password-verifiers','key-material','local-settings','unsaved-drafts'])
    destination=Path(destination);temporary=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('content.json',json.dumps(data,ensure_ascii=False,indent=2))
            for row in db.execute('SELECT stored_name FROM attachments'):
                archive.writestr('attachments/'+row[0],_read_file(s.FILES/row[0]))
            if _table_exists(db,'history_objects'):
                for row in db.execute('SELECT sha256 FROM history_objects'):
                    archive.writestr('history-objects/'+row[0],_read_file(s.ROOT/'history-objects'/row[0]))
        os.replace(temporary,destination)
    finally:temporary.unlink(missing_ok=True)
    return dict(path=str(destination),tasks=len(tables.get('tasks',[])),attachments=len(tables.get('attachments',[])))
