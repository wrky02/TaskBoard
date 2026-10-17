"""TaskBoard 2 data layer. Upgrades legacy SQLite databases with recovery backups.
Data root resolution order (V1.3): an explicit DATA_ROOT override (used by the
installed app to keep data under the install directory's Data\\ subfolder),
otherwise falls back to the legacy LOCALAPPDATA/TaskBoardAccio location so
existing V1.2 installs keep working without manual intervention.
"""
from __future__ import annotations
import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path


def resolve_root():
    """Pick the data root. An override file written by the installer/launcher
    (data_root.txt next to the executable) takes priority; otherwise use the
    historical per-user location so upgrades never lose existing data."""
    override = os.environ.get('TASKBOARD_DATA_ROOT')
    if override:
        return Path(override)
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'TaskBoardAccio'


ROOT = resolve_root()
DB = ROOT / 'tasks.sqlite3'
FILES = ROOT / 'attachments'
STATES = ('计划中', '进行中', '完成')
MAX_FILE = 100 * 1024 * 1024
MAX_BACKUP_FILE = 2 * 1024 * 1024 * 1024


def now(): return datetime.now().isoformat(timespec='seconds')


def writable(path: Path) -> bool:
    """Prove a directory is writable by a normal user: create it and write
    a real file into it, then remove the probe. Returning True from a mere
    permission bit check is not reliable on Windows (ACLs, virtualization),
    so we perform an actual write."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ('.writetest-' + uuid.uuid4().hex)
        probe.write_bytes(b'ok')
        probe.unlink()
        return True
    except OSError:
        return False


def migrate_data_root(install_data_dir):
    """One-time, safe migration of the legacy per-user data directory into
    install_data_dir (the application's own Data\\ folder). Returns a status
    dict describing what happened; never raises on expected conditions.

    Safety rules:
    - Only runs if install_data_dir exists/can be created and is writable.
    - If install_data_dir already has a tasks.sqlite3, migration is skipped
      (already migrated or user placed data there) — never overwrites.
    - If the legacy directory has no database, there's nothing to migrate.
    - Copies first (not move), verifies the copy, then renames the legacy
      directory aside (never deletes it), so a failure or user mistake can
      always be recovered from the legacy location.
    """
    install_data_dir = Path(install_data_dir)
    legacy_root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'TaskBoardAccio'
    legacy_db = legacy_root / 'tasks.sqlite3'
    target_db = install_data_dir / 'tasks.sqlite3'

    # Check the target first: once migrated, the legacy directory gets
    # renamed aside and will no longer be found at its original path, so
    # "target already has data" must take priority over "legacy db missing"
    # to correctly report already_migrated instead of nothing_to_migrate.
    if target_db.is_file():
        return {'status': 'already_migrated'}
    if not legacy_db.is_file():
        return {'status': 'nothing_to_migrate'}
    if not writable(install_data_dir):
        return {'status': 'target_not_writable', 'target': str(install_data_dir)}

    target_files = install_data_dir / 'attachments'
    if target_files.exists() and any(target_files.iterdir()):
        return {'status': 'failed', 'error': '目标目录已有附件但没有数据库，请先检查原文件；未覆盖任何文件'}

    staging = install_data_dir / ('migrate-staging-' + uuid.uuid4().hex)
    installed_db = installed_files = False
    try:
        staging.mkdir(parents=True)
        # SQLite backup includes committed WAL pages; copying just the main
        # file can silently lose recent changes.
        source = sqlite3.connect(f'file:{legacy_db.as_posix()}?mode=ro', uri=True)
        target = sqlite3.connect(staging / 'tasks.sqlite3')
        try: source.backup(target)
        finally: target.close(); source.close()
        legacy_files = legacy_root / 'attachments'
        if legacy_files.is_dir():
            shutil.copytree(legacy_files, staging / 'attachments')
        else:
            (staging / 'attachments').mkdir()

        # Verify the copied database before committing to the new location.
        check = sqlite3.connect(f'file:{(staging / "tasks.sqlite3").as_posix()}?mode=ro', uri=True)
        try:
            if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('迁移副本数据库校验失败')
            task_count = check.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
            attachment_rows = check.execute('SELECT stored_name FROM attachments').fetchall()
        finally:
            check.close()
        missing = [r[0] for r in attachment_rows if not (staging / 'attachments' / r[0]).is_file()]
        if missing:
            raise ValueError(f'迁移副本缺少 {len(missing)} 个附件文件')

        # Commit: move the verified staging content into place, then rename
        # (not delete) the legacy directory so it remains recoverable.
        os.replace(staging / 'tasks.sqlite3', target_db)
        installed_db = True
        if target_files.exists(): target_files.rmdir()  # verified empty above
        os.replace(staging / 'attachments', target_files)
        installed_files = True
        legacy_renamed = legacy_root.parent / ('TaskBoardAccio-migrated-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        try:
            os.replace(legacy_root, legacy_renamed)
        except OSError:
            legacy_renamed = legacy_root  # rename failed (e.g. file lock); legacy stays in place, which is still safe
        return {'status': 'migrated', 'tasks': task_count, 'attachments': len(attachment_rows), 'legacy_kept_at': str(legacy_renamed)}
    except Exception as exc:
        if installed_db: target_db.unlink(missing_ok=True)
        if installed_files: shutil.rmtree(target_files, ignore_errors=True)
        return {'status': 'failed', 'error': str(exc)}
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

def prepare_data_root(install_data_dir):
    """Choose the startup data location without hiding migration failures.

    Explicit overrides are authoritative (including isolated development data).
    Never select a fresh destination after a failed legacy migration.
    """
    if os.environ.get('TASKBOARD_DATA_ROOT'):
        return {'status': 'explicit', 'path': str(resolve_root())}
    install_data_dir = Path(install_data_dir)
    if not writable(install_data_dir):
        return {'status': 'fallback', 'path': str(resolve_root())}
    migration = migrate_data_root(install_data_dir)
    if migration['status'] not in {'migrated', 'already_migrated', 'nothing_to_migrate'}:
        raise RuntimeError('数据迁移未完成，已停止启动以保护原有数据。'
                           + migration.get('error', migration['status']))
    os.environ['TASKBOARD_DATA_ROOT'] = str(install_data_dir)
    return {**migration, 'path': str(install_data_dir)}


def retire_legacy_schema(db):
    """Back up the complete old dataset before retiring obsolete schema."""
    fields = {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}
    has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='templates'").fetchone()
    if not has_table and 'template_id' not in fields:
        return
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError('SQLite 版本过低，无法安全升级；原数据库保持不变')
    recovery = ROOT / ('before-v2-' + uuid.uuid4().hex + '.zip')
    backup(db, recovery)
    db.execute('BEGIN IMMEDIATE')
    with db:
        if 'template_id' in fields:
            db.execute('ALTER TABLE tasks DROP COLUMN template_id')
        db.execute('DROP TABLE IF EXISTS templates')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('升级数据库完整性检查失败')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('升级数据库关联检查失败')


def connect():
    # Re-resolve the data root on every connect() call (not just at import
    # time) so that an app-level data-root decision made before the first
    # connect() — or a test harness exercising migration across multiple
    # simulated "installs" in one process — is always honored.
    global ROOT, DB, FILES
    ROOT = resolve_root()
    DB = ROOT / 'tasks.sqlite3'
    FILES = ROOT / 'attachments'
    ROOT.mkdir(parents=True, exist_ok=True)
    FILES.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    try: retire_legacy_schema(db)
    except Exception:
        db.close()
        raise
    db.executescript('''
      CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '计划中' CHECK(status IN ('计划中','进行中','完成')),
        due_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS attachments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        filename TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE,
        is_image INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
    ''')
    fields = {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}
    legacy_order = 'sort_order' not in fields
    if legacy_order: db.execute('ALTER TABLE tasks ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0')
    if 'deleted_at' not in fields: db.execute('ALTER TABLE tasks ADD COLUMN deleted_at TEXT')
    if 'archived_at' not in fields: db.execute('ALTER TABLE tasks ADD COLUMN archived_at TEXT')
    attachment_fields = {r['name'] for r in db.execute('PRAGMA table_info(attachments)')}
    if 'version_note' not in attachment_fields: db.execute("ALTER TABLE attachments ADD COLUMN version_note TEXT NOT NULL DEFAULT ''")
    if 'version_label' not in attachment_fields: db.execute("ALTER TABLE attachments ADD COLUMN version_label TEXT NOT NULL DEFAULT '初稿'")
    db.executescript('''
      CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
      CREATE TABLE IF NOT EXISTS task_tags(task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY(task_id,tag_id));
      CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER,
        title TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS checklist_items(id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        label TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS milestones(id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        name TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0, confirmed_at TEXT, sort_order INTEGER NOT NULL DEFAULT 0);
      CREATE INDEX IF NOT EXISTS idx_tasks_board ON tasks(deleted_at,status,sort_order);
      CREATE INDEX IF NOT EXISTS idx_activity_at ON activity(id DESC);
      CREATE INDEX IF NOT EXISTS idx_checklist_task ON checklist_items(task_id,sort_order);
      CREATE INDEX IF NOT EXISTS idx_milestones_task ON milestones(task_id,sort_order);
    ''')
    # Old records were ordered by descending ID; convert that order to explicit ranks.
    if legacy_order:
        for state in STATES:
            rows = db.execute('SELECT id FROM tasks WHERE status=? AND deleted_at IS NULL ORDER BY id DESC', (state,)).fetchall()
            for rank, row in enumerate(rows, 1):
                db.execute('UPDATE tasks SET sort_order=? WHERE id=?', (rank * 1000, row['id']))
    db.commit()
    return db


def _event(db, task_id, title, action, detail=''):
    db.execute('INSERT INTO activity(task_id,title,action,detail,at) VALUES (?,?,?,?,?)', (task_id,title,action,detail,now()))


def has_password(db): return db.execute("SELECT 1 FROM settings WHERE key='password'").fetchone() is not None

def set_password(db, password):
    if len(password) < 8: raise ValueError('密码至少 8 位')
    salt = secrets.token_hex(24)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), bytes.fromhex(salt), 310000).hex()
    with db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('salt',?)",(salt,))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('password',?)",(digest,))

def check_password(db, password):
    values = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('salt','password')").fetchall())
    if len(values) != 2: return False
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), bytes.fromhex(values['salt']), 310000).hex()
    return hmac.compare_digest(values['password'],digest)


def get_setting(db, key, default=''):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_display_names(db, system_name, user_name):
    system_name = (system_name or '')[:60]
    if not system_name.strip(): system_name = ''
    user_name = (user_name or '').strip()[:40]
    with db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('system_name',?)", (system_name,))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('user_name',?)", (user_name,))
        _event(db, None, '设置', '修改显示名称', f'系统名称="{system_name}" 使用人="{user_name}"')


def get_display_names(db):
    return {'system_name': get_setting(db, 'system_name', ''), 'user_name': get_setting(db, 'user_name', '')}


def set_board_view_state(db, tag_filter, query):
    with db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('view_tag_filter',?)", (str(tag_filter or ''),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('view_query',?)", (str(query or ''),))


def get_board_view_state(db):
    return {'tag_filter': get_setting(db, 'view_tag_filter', ''), 'query': get_setting(db, 'view_query', '')}


def set_reminder_mode(db, mode):
    if mode not in {'login', 'off'}: raise ValueError('提醒模式无效')
    with db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('reminder_mode',?)", (mode,))


def get_reminder_mode(db):
    return get_setting(db, 'reminder_mode', 'login')


def tasks(db, query='', tag_id=None, trashed=False, archived=False):
    if archived:
        sql = "SELECT t.* FROM tasks t WHERE t.deleted_at IS NULL AND t.archived_at IS NOT NULL"
    else:
        sql = "SELECT t.* FROM tasks t WHERE t.deleted_at IS NOT NULL" if trashed else "SELECT t.* FROM tasks t WHERE t.deleted_at IS NULL AND t.archived_at IS NULL"
    params = []
    if query:
        sql += ' AND (t.title LIKE ? OR t.body LIKE ?)'
        params.extend([f'%{query}%',f'%{query}%'])
    if tag_id:
        sql += ' AND EXISTS(SELECT 1 FROM task_tags x WHERE x.task_id=t.id AND x.tag_id=?)'; params.append(int(tag_id))
    sql += ' ORDER BY t.sort_order ASC, t.id DESC' if not trashed and not archived else ' ORDER BY t.deleted_at DESC, t.id DESC' if trashed else ' ORDER BY t.archived_at DESC, t.id DESC'
    return db.execute(sql,params).fetchall()

def task(db, task_id, allow_deleted=False):
    return db.execute('SELECT * FROM tasks WHERE id=?' + ('' if allow_deleted else ' AND deleted_at IS NULL'),(int(task_id),)).fetchone()

def tags(db): return db.execute('SELECT * FROM tags ORDER BY name COLLATE NOCASE').fetchall()

def task_tag_ids(db, task_id):
    return [r[0] for r in db.execute('SELECT tag_id FROM task_tags WHERE task_id=? ORDER BY tag_id',(task_id,))]

def _replace_tags(db, task_id, ids):
    ids = list(dict.fromkeys(int(v) for v in ids))
    for tag_id in ids:
        if not db.execute('SELECT 1 FROM tags WHERE id=?',(tag_id,)).fetchone(): raise ValueError('标签不存在')
    db.execute('DELETE FROM task_tags WHERE task_id=?',(task_id,))
    db.executemany('INSERT INTO task_tags VALUES (?,?)', [(task_id,tag_id) for tag_id in ids])

def add_tag(db,name):
    name = name.strip()
    if not name or len(name)>32: raise ValueError('标签名称需在 1–32 字之间')
    with db:
        cur=db.execute('INSERT INTO tags(name) VALUES (?)',(name,))
        _event(db,None,'标签','创建标签',name)
    return cur.lastrowid

def rename_tag(db,tag_id,name):
    name=name.strip()
    if not name or len(name)>32: raise ValueError('标签名称需在 1–32 字之间')
    with db:
        cur=db.execute('UPDATE tags SET name=? WHERE id=?',(name,int(tag_id)))
        if not cur.rowcount: raise ValueError('标签不存在')
        _event(db,None,'标签','编辑标签',name)

def delete_tag(db,tag_id):
    with db:
        old=db.execute('SELECT name FROM tags WHERE id=?',(int(tag_id),)).fetchone()
        if not old: return
        db.execute('DELETE FROM tags WHERE id=?',(int(tag_id),))
        _event(db,None,'标签','删除标签',old['name'])

def save_task(db,title,body,status,due,tag_ids=(),task_id=None):
    title=title.strip()
    if not title or len(title)>240: raise ValueError('任务标题不能为空且不超过 240 字')
    if status not in STATES: raise ValueError('任务阶段无效')
    if due: date.fromisoformat(due)
    old=task(db,task_id) if task_id is not None else None
    if task_id is not None and not old: raise ValueError('任务不存在')
    with db:
        if old:
            if old['status'] != status:
                rank=(db.execute('SELECT COALESCE(MAX(sort_order),0) FROM tasks WHERE status=? AND deleted_at IS NULL',(status,)).fetchone()[0] or 0)+1000
            else:
                rank=old['sort_order']
            db.execute('UPDATE tasks SET title=?,body=?,status=?,due_date=?,sort_order=?,updated_at=? WHERE id=?', (title,body,status,due,rank,now(),task_id))
            _replace_tags(db,task_id,tag_ids)
            changes=[]
            if old['status']!=status: changes.append(f"阶段 {old['status']} → {status}")
            if old['due_date']!=due: changes.append('截止日期已修改')
            if old['title']!=title or old['body']!=body: changes.append('标题或备注已修改')
            _event(db,task_id,title,'编辑任务','；'.join(changes) or '标签/内容已保存')
            if old['status']!=status: _event(db,task_id,title,'状态变更',f"{old['status']} → {status}")
            return task_id
        rank=(db.execute('SELECT COALESCE(MAX(sort_order),0) FROM tasks WHERE status=? AND deleted_at IS NULL',(status,)).fetchone()[0] or 0)+1000
        cur=db.execute('INSERT INTO tasks(title,body,status,due_date,created_at,updated_at,sort_order) VALUES (?,?,?,?,?,?,?)',(title,body,status,due,now(),now(),rank))
        _replace_tags(db,cur.lastrowid,tag_ids)
        _event(db,cur.lastrowid,title,'创建任务',status)
        return cur.lastrowid

def move(db,task_id,target,index=None):
    if target not in STATES: raise ValueError('目标阶段无效')
    old=task(db,task_id)
    if not old: raise ValueError('任务不存在')
    if old['archived_at']: raise ValueError('请先取消归档再调整任务阶段')
    with db:
        ids=[r['id'] for r in db.execute('SELECT id FROM tasks WHERE deleted_at IS NULL AND status=? ORDER BY sort_order,id DESC',(target,)) if r['id']!=task_id]
        index = len(ids) if index is None else max(0,min(int(index),len(ids)))
        ids.insert(index,int(task_id))
        if old['status']==target:
            current=[r['id'] for r in db.execute('SELECT id FROM tasks WHERE deleted_at IS NULL AND status=? ORDER BY sort_order,id DESC',(target,))]
            if ids==current:return ids
        for n,tid in enumerate(ids,1):
            db.execute('UPDATE tasks SET status=?,sort_order=?,updated_at=? WHERE id=?',(target,n*1000,now(),tid))
        if old['status']!=target: _event(db,task_id,old['title'],'状态变更',f"{old['status']} → {target}")
        else: _event(db,task_id,old['title'],'拖拽排序',f'{target} 第 {index+1} 位')
    return ids

def trash(db,task_id):
    old=task(db,task_id)
    if not old: raise ValueError('任务不存在')
    with db:
        db.execute('UPDATE tasks SET deleted_at=?,updated_at=? WHERE id=?',(now(),now(),task_id))
        _event(db,task_id,old['title'],'移入回收站')

def restore_task(db,task_id):
    old=task(db,task_id,True)
    if not old or not old['deleted_at']: raise ValueError('回收站中没有此任务')
    rank=(db.execute('SELECT COALESCE(MAX(sort_order),0) FROM tasks WHERE status=? AND deleted_at IS NULL',(old['status'],)).fetchone()[0] or 0)+1000
    with db:
        db.execute('UPDATE tasks SET deleted_at=NULL,sort_order=?,updated_at=? WHERE id=?',(rank,now(),task_id))
        _event(db,task_id,old['title'],'恢复任务',old['status'])

def erase(db,task_id):
    old=task(db,task_id,True)
    if not old or not old['deleted_at']: raise ValueError('只能彻底删除回收站中的任务')
    names=[r['stored_name'] for r in db.execute('SELECT stored_name FROM attachments WHERE task_id=?',(task_id,))]
    with db:
        db.execute('DELETE FROM tasks WHERE id=?',(task_id,))
        _event(db,task_id,old['title'],'彻底删除')
    for name in names: (FILES/name).unlink(missing_ok=True)


def duplicate_task(db, task_id):
    """Copy a task's title/body/due/tags/checklist into a new 计划中 task.
    Attachments are intentionally not copied (they are per-file objects on
    disk); the new task starts with no attachments."""
    old = task(db, task_id)
    if not old: raise ValueError('任务不存在')
    tag_ids = task_tag_ids(db, task_id)
    with db:
        new_id = save_task(db, old['title'] + '（副本）', old['body'], '计划中', old['due_date'], tag_ids)
        for item in db.execute('SELECT label,sort_order FROM checklist_items WHERE task_id=? ORDER BY sort_order', (task_id,)):
            db.execute('INSERT INTO checklist_items(task_id,label,sort_order) VALUES (?,?,?)', (new_id, item['label'], item['sort_order']))
        _event(db, new_id, old['title'] + '（副本）', '复制任务', f'来源任务 #{task_id}')
    return new_id


def archive_completed(db, task_id):
    old = task(db, task_id)
    if not old: raise ValueError('任务不存在')
    if old['status'] != '完成': raise ValueError('只能归档已完成的任务')
    with db:
        db.execute('UPDATE tasks SET archived_at=?,updated_at=? WHERE id=?', (now(), now(), task_id))
        _event(db, task_id, old['title'], '归档任务')


def unarchive_task(db, task_id):
    old = task(db, task_id)
    if not old: raise ValueError('任务不存在')
    with db:
        db.execute('UPDATE tasks SET archived_at=NULL,updated_at=? WHERE id=?', (now(), task_id))
        _event(db, task_id, old['title'], '取消归档')


def bulk_update_status(db, task_ids, target):
    if target not in STATES: raise ValueError('目标阶段无效')
    results = {'ok': [], 'failed': []}
    for tid in task_ids:
        try:
            move(db, int(tid), target)
            results['ok'].append(int(tid))
        except Exception as exc:
            results['failed'].append({'id': int(tid), 'error': str(exc)})
    return results


def bulk_add_tag(db, task_ids, tag_id):
    tag_id = int(tag_id)
    if not db.execute('SELECT 1 FROM tags WHERE id=?', (tag_id,)).fetchone(): raise ValueError('标签不存在')
    results = {'ok': [], 'failed': []}
    for tid in task_ids:
        try:
            tid = int(tid)
            old = task(db, tid)
            if not old: raise ValueError('任务不存在')
            existing = set(task_tag_ids(db, tid))
            existing.add(tag_id)
            with db:
                _replace_tags(db, tid, existing)
                _event(db, tid, old['title'], '批量加标签')
            results['ok'].append(tid)
        except Exception as exc:
            results['failed'].append({'id': int(tid), 'error': str(exc)})
    return results


def bulk_trash(db, task_ids):
    results = {'ok': [], 'failed': []}
    for tid in task_ids:
        try:
            trash(db, int(tid))
            results['ok'].append(int(tid))
        except Exception as exc:
            results['failed'].append({'id': int(tid), 'error': str(exc)})
    return results

def attachments(db,task_id): return db.execute('SELECT * FROM attachments WHERE task_id=? ORDER BY id',(task_id,)).fetchall()

def add_file_bytes(db,task_id,filename,content,image=False):
    if not task(db,task_id): raise ValueError('任务不存在')
    if not content or len(content)>MAX_FILE: raise ValueError('附件不能为空且不能超过 100 MB')
    filename=Path(filename).name[:240]
    ext=Path(filename).suffix.lower()
    stored=uuid.uuid4().hex+(ext if len(ext)<12 else '.bin')
    path=FILES/stored; path.write_bytes(content)
    try:
        with db:
            cur=db.execute('INSERT INTO attachments(task_id,filename,stored_name,is_image,created_at) VALUES(?,?,?,?,?)',(task_id,filename,stored,int(image),now()))
            old=task(db,task_id)
            _event(db,task_id,old['title'],'添加附件',filename)
            return cur.lastrowid
    except Exception:
        path.unlink(missing_ok=True); raise

def add_file_path(db,task_id,source):
    source=Path(source)
    if not source.is_file() or source.stat().st_size>MAX_FILE: raise ValueError('文件不存在或超过 100 MB')
    return add_file_bytes(db,task_id,source.name,source.read_bytes(),source.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.bmp','.gif'})

def remove_file(db,attachment_id):
    row=db.execute('SELECT * FROM attachments WHERE id=?',(attachment_id,)).fetchone()
    if not row:return
    old=task(db,row['task_id'],True)
    with db:
        db.execute('DELETE FROM attachments WHERE id=?',(attachment_id,))
        _event(db,row['task_id'],old['title'] if old else '任务','移除附件',row['filename'])
    (FILES/row['stored_name']).unlink(missing_ok=True)

def activity(db,limit=200,task_id=None):
    if task_id:
        return db.execute('SELECT * FROM activity WHERE task_id=? ORDER BY id DESC LIMIT ?',(task_id,limit)).fetchall()
    return db.execute('SELECT * FROM activity ORDER BY id DESC LIMIT ?',(limit,)).fetchall()

def reminders(db,today=None):
    today = today or date.today()
    end=(today+timedelta(days=3)).isoformat()
    rows=db.execute('SELECT * FROM tasks WHERE deleted_at IS NULL AND status<>? AND due_date IS NOT NULL AND due_date<=? ORDER BY due_date,sort_order',('完成',end)).fetchall()
    overdue=[r for r in rows if r['due_date']<today.isoformat()]
    soon=[r for r in rows if r['due_date']>=today.isoformat()]
    return overdue,soon


def backup(db,destination):
    destination=Path(destination)
    temp=ROOT/('snapshot-'+uuid.uuid4().hex+'.sqlite3')
    try:
        target=sqlite3.connect(temp)
        try: db.backup(target)
        finally: target.close()
        names=[r['stored_name'] for r in db.execute('SELECT stored_name FROM attachments')]
        for name in names:
            if not (FILES/name).is_file(): raise ValueError('附件缺失，未生成不完整备份：'+name)
        with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
            z.write(temp,'tasks.sqlite3')
            for name in names: z.write(FILES/name,'attachments/'+name)
        validate_backup(destination)
    finally: temp.unlink(missing_ok=True)

def validate_backup(source):
    source=Path(source)
    with zipfile.ZipFile(source) as z:
        infos=z.infolist()
        if len(infos)>10000 or not infos or any(i.file_size>MAX_BACKUP_FILE for i in infos): raise ValueError('备份文件过大或为空')
        for info in infos:
            name=info.filename
            if info.compress_size and info.file_size / info.compress_size > 10000: raise ValueError('备份压缩比异常')
            if name=='tasks.sqlite3':continue
            if not re.fullmatch(r'attachments/[a-f0-9]{32}(?:\.[a-zA-Z0-9]{1,11})?',name):raise ValueError('备份包含无效路径')
        if sum(i.file_size for i in infos)>4*1024*1024*1024:raise ValueError('备份解压体积过大')
        if len([i for i in infos if i.filename=='tasks.sqlite3'])!=1:raise ValueError('缺少数据库')
        if z.testzip() is not None:raise ValueError('备份压缩包损坏')
        with tempfile.TemporaryDirectory(prefix='taskboard-verify-') as tmp:
            dbpath=Path(tmp)/'test.sqlite3'; dbpath.write_bytes(z.read('tasks.sqlite3'))
            test=sqlite3.connect(f'file:{dbpath.as_posix()}?mode=ro',uri=True)
            try:
                if test.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('备份数据库损坏')
                for table in ('settings','tasks','attachments'):
                    if not test.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():raise ValueError('数据库结构不符')
                refs=[r[0] for r in test.execute('SELECT stored_name FROM attachments')]
                missing=set(refs)-{i.filename.split('/',1)[1] for i in infos if i.filename.startswith('attachments/')}
                if missing:raise ValueError('备份缺少附件')
            finally: test.close()
    return True

def restore_backup(db,source):
    """Validate first; preserve a recovery ZIP, stage and atomically swap files.
    Returns a fresh SQLite connection and safety ZIP path; on failure, old db stays readable.
    Caller must stop editing during this operation and replace its connection.
    """
    validate_backup(source)
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    safety=ROOT/f'pre-restore-{stamp}-{uuid.uuid4().hex[:6]}.zip'
    backup(db,safety)
    staged=ROOT/('restore-'+uuid.uuid4().hex)
    staged.mkdir()
    old_db=ROOT/('old-'+uuid.uuid4().hex+'.sqlite3')
    old_files=ROOT/('old-files-'+uuid.uuid4().hex)
    swapped_db=swapped_files=False
    fresh = None
    try:
        with zipfile.ZipFile(source) as z:
            (staged/'attachments').mkdir()
            for item in z.infolist():
                dest=staged/item.filename
                dest.write_bytes(z.read(item))
        db.close()
        os.replace(DB,old_db); swapped_db=True
        os.replace(FILES,old_files); swapped_files=True
        os.replace(staged/'tasks.sqlite3',DB)
        os.replace(staged/'attachments',FILES)
        fresh=connect()
        if fresh.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('恢复后的数据库校验失败')
        # Windows clean-up of old files is best-effort; restoration is already committed.
        try: old_db.unlink(missing_ok=True); shutil.rmtree(old_files)
        except OSError: pass
        return fresh,safety
    except Exception:
        # Close the replacement connection before renaming on Windows.
        if fresh is not None: fresh.close()
        # Revert to the original state if any step of the replacement fails.
        try:
            if swapped_db and old_db.exists(): os.replace(old_db,DB)
            if swapped_files and old_files.exists():
                if FILES.exists(): shutil.rmtree(FILES)
                os.replace(old_files,FILES)
        finally:
            if staged.exists():shutil.rmtree(staged,ignore_errors=True)
        raise
    finally:
        if staged.exists():shutil.rmtree(staged,ignore_errors=True)


def checklist_items(db, task_id):
    return db.execute('SELECT * FROM checklist_items WHERE task_id=? ORDER BY sort_order,id', (task_id,)).fetchall()


def add_checklist_item(db, task_id, label):
    label = str(label).strip()[:120]
    if not label: raise ValueError('检查项内容不能为空')
    if not task(db, task_id): raise ValueError('任务不存在')
    with db:
        rank = (db.execute('SELECT COALESCE(MAX(sort_order),0) FROM checklist_items WHERE task_id=?', (task_id,)).fetchone()[0] or 0) + 10
        cur = db.execute('INSERT INTO checklist_items(task_id,label,sort_order) VALUES (?,?,?)', (task_id, label, rank))
        return cur.lastrowid


def toggle_checklist_item(db, item_id, done):
    with db:
        cur = db.execute('UPDATE checklist_items SET done=? WHERE id=?', (int(bool(done)), int(item_id)))
        if not cur.rowcount: raise ValueError('检查项不存在')


def delete_checklist_item(db, item_id):
    with db:
        db.execute('DELETE FROM checklist_items WHERE id=?', (int(item_id),))


def milestones(db, task_id):
    return db.execute('SELECT * FROM milestones WHERE task_id=? ORDER BY sort_order,id', (task_id,)).fetchall()


def add_milestone(db, task_id, name):
    name = str(name).strip()[:120]
    if not name: raise ValueError('里程碑名称不能为空')
    if not task(db, task_id): raise ValueError('任务不存在')
    with db:
        rank = (db.execute('SELECT COALESCE(MAX(sort_order),0) FROM milestones WHERE task_id=?', (task_id,)).fetchone()[0] or 0) + 10
        cur = db.execute('INSERT INTO milestones(task_id,name,sort_order) VALUES (?,?,?)', (task_id, name, rank))
        old = task(db, task_id)
        _event(db, task_id, old['title'], '新增里程碑', name)
        return cur.lastrowid


def confirm_milestone(db, milestone_id, confirmed):
    row = db.execute('SELECT * FROM milestones WHERE id=?', (int(milestone_id),)).fetchone()
    if not row: raise ValueError('里程碑不存在')
    with db:
        db.execute('UPDATE milestones SET confirmed=?,confirmed_at=? WHERE id=?',
                   (int(bool(confirmed)), now() if confirmed else None, int(milestone_id)))
        old = task(db, row['task_id'], True)
        if old:
            _event(db, row['task_id'], old['title'], '里程碑确认' if confirmed else '里程碑取消确认', row['name'])


def delete_milestone(db, milestone_id):
    with db:
        db.execute('DELETE FROM milestones WHERE id=?', (int(milestone_id),))


def set_attachment_version(db, attachment_id, version_label, version_note):
    version_label = str(version_label).strip()[:20] or '初稿'
    version_note = str(version_note).strip()[:200]
    with db:
        cur = db.execute('UPDATE attachments SET version_label=?,version_note=? WHERE id=?',
                          (version_label, version_note, int(attachment_id)))
        if not cur.rowcount: raise ValueError('附件不存在')


def attachment_health_check(db):
    """Compare the attachments table against the actual files on disk.
    Read-only: never deletes or modifies either side. Returns a report the
    UI can show; the user decides what, if anything, to do about it."""
    db_rows = db.execute('SELECT a.id,a.filename,a.stored_name,a.task_id,t.title FROM attachments a '
                          'LEFT JOIN tasks t ON t.id=a.task_id').fetchall()
    missing = []
    for row in db_rows:
        if not (FILES / row['stored_name']).is_file():
            missing.append({'attachment_id': row['id'], 'filename': row['filename'],
                             'task_id': row['task_id'], 'task_title': row['title'] or '(任务已不存在)'})
    referenced = {row['stored_name'] for row in db_rows}
    orphaned = []
    if FILES.is_dir():
        for p in FILES.iterdir():
            if p.is_file() and p.name not in referenced and not p.name.startswith('.'):
                orphaned.append({'stored_name': p.name, 'size': p.stat().st_size})
    return {'checked_at': now(), 'total_attachments': len(db_rows),
            'missing': missing, 'orphaned_files': orphaned}


def calendar_tasks(db, year, month):
    """Tasks with a due date in the given month, excluding trashed/archived."""
    start = f'{year:04d}-{month:02d}-01'
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = f'{end_year:04d}-{end_month:02d}-01'
    return db.execute('SELECT * FROM tasks WHERE deleted_at IS NULL AND archived_at IS NULL '
                       'AND due_date>=? AND due_date<? ORDER BY due_date,sort_order', (start, end)).fetchall()


def _export_rows(db, query='', tag_id=None):
    rows = list(tasks(db, query, tag_id)) + list(tasks(db, query, tag_id, archived=True))
    tagmap = {t['id']: t['name'] for t in tags(db)}
    out = []
    for t in rows:
        task_tags = ','.join(tagmap[i] for i in task_tag_ids(db, t['id']) if i in tagmap)
        note = re.sub(r'<[^>]+>', ' ', t['body'])[:200].strip()
        out.append({'id': t['id'], 'title': t['title'], 'status': t['status'],
                    'due_date': t['due_date'] or '', 'tags': task_tags, 'note': note,
                    'attachments': len(attachments(db, t['id']))})
    return out


def export_csv(db, destination, query='', tag_id=None):
    rows = _export_rows(db, query, tag_id)
    destination = Path(destination)
    # utf-8-sig so Excel on Windows opens Chinese text correctly without BOM confusion.
    with open(destination, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['ID', '标题', '状态', '截止日期', '标签', '备注摘要', '附件数'])
        for r in rows:
            # Guard against formula injection if the CSV is later opened in Excel.
            def safe(v):
                v = str(v)
                return "'" + v if v and (v[0] in ('\t', '\r', '\n') or v.lstrip().startswith(('=', '+', '-', '@'))) else v
            writer.writerow([r['id'], safe(r['title']), r['status'], r['due_date'], safe(r['tags']), safe(r['note']), r['attachments']])
    return len(rows)


def export_xlsx(db, destination, query='', tag_id=None):
    from openpyxl import Workbook
    rows = _export_rows(db, query, tag_id)
    wb = Workbook()
    ws = wb.active
    ws.title = '任务清单'
    ws.append(['ID', '标题', '状态', '截止日期', '标签', '备注摘要', '附件数'])
    for r in rows:
        def safe(v):
            v = str(v)
            return "'" + v if v and (v[0] in ('\t', '\r', '\n') or v.lstrip().startswith(('=', '+', '-', '@'))) else v
        ws.append([r['id'], safe(r['title']), r['status'], r['due_date'], safe(r['tags']), safe(r['note']), r['attachments']])
    wb.save(destination)
    return len(rows)
