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
import zipfile
import storage as s
import features as f
import domain
from instance_lock import InstanceLock

VERSION = '3.0.1'

class Worker:
    def __init__(self):
        self.db = s.connect()
        self.auth = False
        self.failures = 0
        self.retry_at = 0
        self.preview = None
        try:
            f.auto_backup(self.db)
            with self.db: self.db.execute("DELETE FROM settings WHERE key='backup_error'")
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            with self.db: self.db.execute("INSERT OR REPLACE INTO settings VALUES ('backup_error',?)", (str(exc),))

    def state(self):
        result = domain.snapshot(self.db)
        result['version'] = VERSION
        result['info']['version'] = VERSION
        # Paths are shared only with the owning native process, after unlocking.
        for group in ('tasks', 'trash', 'archived'):
            for task in result[group]:
                for file in task['files']:
                    file.pop('url', None)
                    file['path'] = str(domain.safe_file(file['stored_name']))
        return result

    def dispatch(self, method, item):
        db = self.db
        if method == 'status': return dict(first=not s.has_password(db), version=VERSION)
        if method == 'login':
            if time.monotonic() < self.retry_at: raise ValueError('尝试过于频繁，请 30 秒后再试')
            password = str(item.get('password', ''))
            if not s.has_password(db):
                if password != item.get('confirm'): raise ValueError('两次密码不一致')
                s.set_password(db, password)
            elif not s.check_password(db, password):
                self.failures += 1
                if self.failures >= 5: self.retry_at = time.monotonic() + 30
                raise ValueError('密码错误')
            self.auth = True; self.failures = 0
            return self.state()
        if not self.auth: raise ValueError('请先解锁工作区')
        if method == 'lock': self.auth = False; return {}
        if method == 'state': return self.state()
        if method == 'save':
            item = dict(item)
            # Native attachments use selected local paths, then the same atomic
            # domain transaction as 2.1; drafts embed bytes for crash recovery.
            files = list(item.get('new_files', []))
            total = sum(len(base64.b64decode(x['data'], validate=True)) for x in files)
            for filename in item.get('file_paths', []):
                path = Path(filename)
                total += path.stat().st_size
                if total > s.MAX_FILE: raise ValueError('本次新增附件不能超过 100 MB')
                files.append(dict(name=path.name, data=base64.b64encode(path.read_bytes()).decode()))
            item['new_files'] = files
            tid = domain.save_draft(db, item)
            if item.get('draft_key'): f.save_draft(db, dict(key=item['draft_key'], action='delete'))
            return dict(state=self.state(), id=tid)
        if method == 'action':
            tid = int(item['id']); action = item['action']
            before = f.undo_record(db, tid) if action in ('move', 'trash', 'archive') else None
            if action == 'move': s.move(db, tid, item['status'], item.get('index'))
            elif action == 'trash': s.trash(db, tid)
            elif action == 'archive': s.archive_completed(db, tid)
            elif action == 'restore': s.restore_task(db, tid)
            elif action == 'erase': s.erase(db, tid)
            elif action == 'unarchive': s.unarchive_task(db, tid)
            elif action == 'duplicate': s.duplicate_task(db, tid)
            else: raise ValueError('操作无效')
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
            for tid in item['ids']:
                try:
                    task = next(t for t in self.state()['tasks'] if t['id'] == tid)
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
            behavior = item['close_behavior']
            if behavior not in ('ask', 'tray', 'exit'): raise ValueError('关闭行为无效')
            s.set_display_names(db, item['system_name'], item['user_name'])
            with db: db.execute("INSERT OR REPLACE INTO settings VALUES ('close_behavior',?)", (behavior,))
        elif method == 'password':
            if not s.check_password(db, item['old']): raise ValueError('旧密码错误')
            if item['new'] != item['confirm']: raise ValueError('两次密码不一致')
            s.set_password(db, item['new'])
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
                        copy = sqlite3.connect(Path(tmp)/'tasks.sqlite3')
                        try: src.backup(copy)
                        finally: src.close(); copy.close()
                        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
                            z.write(Path(tmp)/'tasks.sqlite3', 'tasks.sqlite3')
                            for file in (source/'attachments').glob('*'):
                                if file.is_file(): z.write(file, 'attachments/'+file.name)
                else: shutil.copyfile(source, target)
                result = f.preview_backup(target); self.preview = str(target)
                return result
            except Exception: target.unlink(missing_ok=True); raise
        elif method == 'restore':
            if not self.preview: raise ValueError('请先预览备份')
            try: self.db, safety = s.restore_backup(db, self.preview)
            except Exception:
                db.close(); self.db = s.connect(); raise
            finally: Path(self.preview).unlink(missing_ok=True); self.preview = None
            return dict(state=self.state(), safety=str(safety))
        else: raise ValueError('未知命令')
        return self.state()

    def close(self):
        self.db.close()
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
        for raw in sys.stdin:
            request = {}
            try:
                request = json.loads(raw)
                result = worker.dispatch(request['method'], request.get('params') or {})
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
