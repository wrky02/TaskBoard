"""Private stdio RPC for the WPF desktop. No listening socket or browser.
The parent owns stdin/stdout. EOF closes the database and releases the 2.x lock.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import queue
import threading
from datetime import date
import copy
import zipfile
import storage as s
import features as f
import domain
import pro_features as pro
import recovery
import attachment_service as attachments
import vault
from instance_lock import InstanceLock

VERSION = '4.0.0'

class Worker:
    def __init__(self):
        self.root = s.resolve_root()
        self.root.mkdir(parents=True, exist_ok=True)
        vault.recover_migration(self.root)
        recovery.recover_interrupted(self.root)
        self.db = None
        self.auth = False
        self.failures = 0
        self.retry_at = 0
        self.preview = None
        self.preview_credentials={}
        self.cache = None
        self.cache_key = None
        self.last_maintenance = 0
        if not vault.is_enabled(self.root): self.open_database()

    def open_database(self):
        self.db = s.connect()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS native_operations(id TEXT PRIMARY KEY,method TEXT NOT NULL,digest TEXT NOT NULL,status TEXT NOT NULL,result TEXT,created TEXT NOT NULL)")
        self.cache = None; self.cache_key = None

    def maintenance(self):
        if self.db is None: return
        if time.monotonic()-self.last_maintenance < 5: return
        self.last_maintenance=time.monotonic()
        recovery.tick(self.db)
        if self.auth: pro.tick(self.db)

    def execute(self, method, item, operation_id=None):
        # Mutation IDs survive broken pipes. Unknown outcomes are never blindly
        # replayed; task saves commit their result inside the task transaction.
        tracked = method in {'save','action','bulk','history-restore','attachment-version-restore'}
        if not tracked or not operation_id or not self.auth:
            return self.dispatch(method,item)
        if not isinstance(operation_id,str) or len(operation_id)>80: raise ValueError('操作编号无效')
        encoded=json.dumps(item,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        prior=self.db.execute('SELECT * FROM native_operations WHERE id=?',(operation_id,)).fetchone()
        if prior:
            if prior['method']!=method or prior['digest']!=digest: raise ValueError('同一操作编号不能用于不同请求')
            if prior['status']=='done': return {**json.loads(prior['result'] or '{}'),'state':self.state()}
            raise ValueError('此操作结果尚待核对；请刷新检查，不能重复提交')
        with self.db:
            self.db.execute('INSERT INTO native_operations VALUES (?,?,?,?,?,?)',(operation_id,method,digest,'pending',None,s.now()))
        payload=copy.deepcopy(item)
        if method=='save': payload['_operation_id']=operation_id
        try:
            result=self.dispatch(method,payload)
            small={k:v for k,v in result.items() if k!='state'}
            with self.db:self.db.execute("UPDATE native_operations SET status='done',result=? WHERE id=?",(json.dumps(small,ensure_ascii=False),operation_id))
            return result
        except Exception:
            # Leave pending for results that may have committed before an I/O
            # failure. The UI can query operation-status and inspect the task.
            raise

    def state(self):
        self.maintenance()
        key=(id(self.db),self.db.total_changes,date.today().isoformat())
        if self.cache is not None and self.cache_key==key:
            return self.cache
        result=domain.snapshot(self.db)
        result['version']=VERSION;result['info']['version']=VERSION
        result['recovery']=recovery.status(self.db)
        result['vault']=dict(enabled=vault.is_enabled(self.root),unlocked=vault.is_unlocked(self.root))
        result['info']['encrypted']=result['vault']['enabled']
        for group in ('tasks','trash','archived'):
            for task in result[group]:
                for file in task['files']:
                    file.pop('url',None)
                    # Legacy plain-workspace clients remain compatible; the
                    # professional editor always uses authenticated byte RPC.
                    file['path']='' if result['vault']['enabled'] else str(domain.safe_file(file['stored_name']))
        self.cache=result;self.cache_key=key
        return result

    def dispatch(self, method, item):
        db=self.db
        if method=='status':
            return dict(first=not s.has_password(db) if db is not None else False,version=VERSION,encrypted=vault.is_enabled(self.root))
        if method=='vault-status':
            return dict(enabled=vault.is_enabled(self.root),unlocked=vault.is_unlocked(self.root),cipher='SQLCipher + AES-256-GCM')
        if method in ('login','vault-recover'):
            if time.monotonic()<self.retry_at:raise ValueError('尝试过于频繁，请 30 秒后再试')
            password=str(item.get('password',''))
            try:
                if method=='vault-recover':
                    if not vault.is_enabled(self.root):raise ValueError('当前工作区未启用加密恢复密钥')
                    if item.get('new_password')!=item.get('confirm'):raise ValueError('两次密码不一致')
                    password=str(item.get('new_password',''))
                    if len(password)<8:raise ValueError('密码至少 8 位')
                    vault.recover_password(self.root,item['recovery_key'],password)
                    if self.db is None:self.open_database()
                    s.set_password(self.db,password)
                elif vault.is_enabled(self.root):
                    vault.unlock(self.root,password=password)
                    if self.db is None:self.open_database()
                else:
                    if not s.has_password(db):
                        if password!=item.get('confirm'):raise ValueError('两次密码不一致')
                        s.set_password(db,password)
                    elif not s.check_password(db,password):raise ValueError('密码错误')
            except Exception:
                self.failures+=1
                if self.failures>=5:self.retry_at=time.monotonic()+30
                raise
            self.auth=True;self.failures=0;return self.state()
        if not self.auth:raise ValueError('请先解锁工作区')
        if method=='lock':
            recovery.wait_for_jobs(self.root)
            self.auth=False
            if vault.is_enabled(self.root):
                self.db.close();self.db=None;vault.lock(self.root);self.cache=None
            return {}
        if method=='state':return self.state()
        if method=='task-get':
            page=domain.snapshot(db,[int(item['id'])])
            tasks=page['tasks']+page['archived']+page['trash']
            if not tasks:raise ValueError('任务不存在')
            return dict(task=tasks[0])
        if method=='maintenance':
            self.maintenance();return dict(recovery=recovery.status(db))
        if method=='operation-status':
            row=db.execute('SELECT method,status,result,created FROM native_operations WHERE id=?',(item['id'],)).fetchone()
            return dict(row) if row else dict(status='unknown')
        if method=='vault-enable':
            if vault.is_enabled(self.root):raise ValueError('工作区已经启用加密')
            if not s.check_password(db,item.get('password','')):raise ValueError('密码错误')
            recovery.wait_for_jobs(self.root)
            db.close();self.db=None
            try:result=vault.enable(self.root,item['password'])
            finally:self.open_database()
            return dict(result,state=self.state())
        if method=='vault-new-recovery-key':
            return dict(recovery_key=vault.new_recovery_key(self.root,item['password']))
        if method.startswith('attachment-'):
            result=attachments.handle(db,method,item)
            if method=='attachment-version-restore':result['state']=self.state()
            return result
        if method.startswith('recovery-'):
            if method=='recovery-status':return recovery.status(db)
            if method=='recovery-configure':recovery.configure(db,item)
            elif method=='recovery-run':recovery.tick(db,force=True)
            elif method=='recovery-verify':
                info=f.preview_backup(Path(item['path']));return dict(info,verified=True)
            else:raise ValueError('恢复操作无效')
            return recovery.status(db)
        if method=='history-list':return dict(versions=recovery.history_list(db,int(item['id'])))
        if method=='history-diff':return recovery.history_diff(db,int(item['version_id']),item.get('other_version_id'))
        if method=='history-restore':
            payload=recovery.history_restore_payload(db,int(item['version_id']),bool(item.get('as_new')))
            payload['_history_reason']='恢复历史版本'
            payload['_trusted_history']=True
            tid=domain.save_draft(db,payload);return dict(id=tid,state=self.state())
        if method=='activity-clear':
            mode=item.get('mode','all')
            with db:
                if mode=='all':db.execute('DELETE FROM activity')
                elif mode=='before':
                    before=date.fromisoformat(item['before']).isoformat()
                    db.execute('DELETE FROM activity WHERE at<?',(before,))
                else:raise ValueError('清理范围无效')
            return self.state()
        if method.startswith('pro-'):
            result=pro.handle(db,method,item)
            readonly={'pro-query','pro-review','pro-impact','pro-series-preview','pro-reminders','pro-export','pro-import-preview'}
            if method not in readonly and not result.get('requires_confirmation'):
                result['state']=self.state()
            return result
        if method == 'save':
            item=dict(item)
            item.pop('_trusted_history',None)
            for file in item.get('new_files',[]):file.pop('history_version',None)
            # Native attachments use selected local paths, then the same atomic
            # domain transaction as 2.1; drafts embed bytes for crash recovery.
            files = list(item.get('new_files', []))
            total = sum(int(x.get('size',0)) if x.get('token') else len(base64.b64decode(x['data'],validate=True)) for x in files)
            for filename in item.get('file_paths', []):
                path = Path(filename)
                total += path.stat().st_size
                if total > s.MAX_FILE: raise ValueError('本次新增附件不能超过 100 MB')
                files.append(dict(name=path.name, data=base64.b64encode(path.read_bytes()).decode()))
            if item.get('copy_files_from'):
                source_id=int(item['copy_files_from'])
                if not s.task(db,source_id,True):raise ValueError('要复制的任务不存在')
                for source_file in db.execute('SELECT a.* FROM attachments a LEFT JOIN attachment_versions v ON a.id=v.file_id WHERE a.task_id=? AND COALESCE(v.current,1)=1',(source_id,)):
                    raw=attachments.read_bytes(domain.safe_file(source_file['stored_name']))
                    total+=len(raw)
                    if total>s.MAX_FILE:raise ValueError('复制附件与新增附件总计不得超过 100 MB')
                    files.append(dict(name=source_file['filename'],data=base64.b64encode(raw).decode()))
            item['new_files'] = files
            tid = domain.save_draft(db, item)
            if item.get('draft_key'): f.save_draft(db, dict(key=item['draft_key'], action='delete'))
            return dict(state=self.state(), id=tid)
        if method == 'action':
            tid = int(item['id']); action = item['action']
            before = f.undo_record(db, tid) if action in ('move', 'trash', 'archive') else None
            if action == 'move':
                pro.validate_transition(db,tid,item['status'])
                s.move(db,tid,item['status'],item.get('index'))
                with db:pro.transitioned(db,tid,item['status'])
            elif action == 'trash': s.trash(db, tid)
            elif action == 'archive': s.archive_completed(db, tid)
            elif action == 'restore': s.restore_task(db, tid)
            elif action == 'erase': s.erase(db, tid)
            elif action == 'unarchive': s.unarchive_task(db, tid)
            elif action == 'duplicate': s.duplicate_task(db, tid)
            else: raise ValueError('操作无效')
            if action not in ('erase','duplicate'):
                with db: recovery.history_record(db,tid,'任务操作：'+action)
            return dict(state=self.state(), undo=f.make_undo(db, before) if before else None)
        if method == 'undo': f.undo(db, item['key'])
        elif method == 'tag':
            action = item['action']
            if action == 'create': s.add_tag(db, item['name'])
            elif action == 'rename': s.rename_tag(db, int(item['id']), item['name'])
            elif action == 'delete': s.delete_tag(db, int(item['id']))
            elif action in ('merge', 'color'): f.tag_update(db, item)
            else: raise ValueError('标签操作无效')
        elif method == 'view': f.save_view(db, item)
        elif method == 'bulk':
            result = dict(ok=[], failed=[])
            available={t['id']:t for t in self.state()['tasks']}
            for tid in item['ids']:
                try:
                    task = copy.deepcopy(available[tid])
                    payload = dict(task, due=task['due_date'], tags=[t['id'] for t in task['tags']], dependencies=[t['id'] for t in task['dependencies']])
                    changes = item['changes']
                    for key in ('due', 'status', 'tags'):
                        if key in changes: payload[key] = changes[key]
                    if 'priority' in changes: payload['options'] = dict(task['options'], priority=changes['priority'])
                    domain.save_draft(db, payload); result['ok'].append(tid)
                except (ValueError, KeyError, StopIteration) as exc: result['failed'].append(dict(id=tid, error=str(exc)))
            return dict(state=self.state(), result=result)
        elif method == 'reminders': return dict(reminders=f.reminders(db))
        elif method == 'reminder': f.acknowledge(db, item); return {}
        elif method == 'drafts': return dict(drafts=[dict(id=r['id'], updated=r['updated'], title=json.loads(r['data']).get('title', '')) for r in db.execute('SELECT * FROM drafts ORDER BY updated DESC')])
        elif method == 'draft-get':
            row = db.execute('SELECT data FROM drafts WHERE id=?', (item['key'],)).fetchone()
            if not row: raise ValueError('草稿不存在')
            return json.loads(row[0])
        elif method == 'draft-save': f.save_draft(db, item); return {}
        elif method == 'settings':
            behavior = item.get('close_behavior',s.get_setting(db,'close_behavior','ask'))
            if behavior not in ('ask', 'tray', 'exit'): raise ValueError('关闭行为无效')
            names=s.get_display_names(db)
            s.set_display_names(db,item.get('system_name',names['system_name']),item.get('user_name',names['user_name']))
            with db: db.execute("INSERT OR REPLACE INTO settings VALUES ('close_behavior',?)", (behavior,))
        elif method == 'password':
            if vault.is_enabled(self.root):vault.unlock(self.root,password=item['old'])
            elif not s.check_password(db,item['old']):raise ValueError('旧密码错误')
            if item['new'] != item['confirm']: raise ValueError('两次密码不一致')
            if vault.is_enabled(self.root):vault.change_password(self.root,item['old'],item['new'])
            s.set_password(db,item['new'])
        elif method == 'diagnostics':
            report = dict(f.diagnostics(db), version=VERSION, interface='WPF', transport='private-stdio', browser=False)
            Path(item['path']).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'); return report
        elif method == 'health': return s.attachment_health_check(db)
        elif method == 'export':
            dest = Path(item['path']); kind = item['format']
            if kind == 'zip': s.backup(db, dest)
            elif kind == 'csv': s.export_csv(db, dest)
            elif kind == 'xlsx': s.export_xlsx(db, dest)
            else: raise ValueError('导出格式无效')
            return {}
        elif method == 'preview':
            if self.preview: Path(self.preview).unlink(missing_ok=True); self.preview = None
            source = Path(item['path'])
            fd, name = tempfile.mkstemp(prefix='native-import-', suffix='.zip', dir=s.ROOT); os.close(fd)
            target = Path(name)
            try:
                if source.is_dir():
                    if source.resolve() == s.ROOT.resolve(): raise ValueError('不能导入正在使用的数据目录')
                    # SQLite backup includes WAL; never copy a live DB file alone.
                    with tempfile.TemporaryDirectory() as tmp:
                        src = sqlite3.connect((source/'tasks.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)
                        snapshot_copy = sqlite3.connect(Path(tmp)/'tasks.sqlite3')
                        try: src.backup(snapshot_copy)
                        finally: src.close(); snapshot_copy.close()
                        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
                            z.write(Path(tmp)/'tasks.sqlite3', 'tasks.sqlite3')
                            for file in (source/'attachments').glob('*'):
                                if file.is_file(): z.write(file, 'attachments/'+file.name)
                else: shutil.copyfile(source, target)
                credentials={k:item[k] for k in ('password','recovery_key') if item.get(k)}
                result=f.preview_backup(target,**credentials);self.preview=str(target);self.preview_credentials=credentials
                return result
            except Exception: target.unlink(missing_ok=True); raise
        elif method == 'restore':
            if not self.preview: raise ValueError('请先预览备份')
            try: self.db,safety=s.restore_backup(db,self.preview,**self.preview_credentials)
            except Exception:
                db.close(); self.db = s.connect(); raise
            finally: Path(self.preview).unlink(missing_ok=True);self.preview=None;self.preview_credentials={};self.cache=None;self.cache_key=None
            return dict(state=self.state(), safety=str(safety))
        else: raise ValueError('未知命令')
        return self.state()

    def close(self):
        recovery.wait_for_jobs(self.root)
        if self.db is not None:self.db.close()
        vault.lock(self.root)
        if self.preview: Path(self.preview).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--data-root', type=Path); parser.add_argument('--install-root', type=Path)
    args = parser.parse_args()
    if args.data_root: os.environ['TASKBOARD_DATA_ROOT'] = str(args.data_root.resolve())
    root = args.install_root or Path(sys.executable).parent.parent
    # Determine lock location before migration, matching the 2.x launcher.
    target = root/'Data'
    location = s.resolve_root() if os.environ.get('TASKBOARD_DATA_ROOT') or not s.writable(target) else target
    location.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(location)
    worker = None
    try:
        if not lock.acquire(): raise ValueError('此数据目录已由另一个任务看板实例使用，请先关闭旧版程序')
        s.prepare_data_root(target)
        worker = Worker()
        incoming=queue.Queue()
        def read_input():
            try:
                for line in sys.stdin:incoming.put(line)
            finally:incoming.put(None)
        threading.Thread(target=read_input,daemon=True).start()
        while True:
            try:raw=incoming.get(timeout=1)
            except queue.Empty:
                try:worker.maintenance()
                except Exception:pass
                continue
            if raw is None:break
            request = {}
            try:
                request = json.loads(raw)
                result = worker.execute(request['method'],request.get('params') or {},request.get('operation_id'))
                reply = dict(id=request.get('id'), result=result)
            except Exception as exc:
                reply = dict(id=request.get('id'), error=str(exc))
            print(json.dumps(reply, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False), flush=True)
        return 1
    finally:
        if worker: worker.close()
        lock.close()
    return 0

if __name__ == '__main__':
    if hasattr(sys.stdin, 'reconfigure'): sys.stdin.reconfigure(encoding='utf-8')
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
