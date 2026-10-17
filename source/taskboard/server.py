"""TaskBoard 2: loopback-only HTTP application, independent of Qt."""
from __future__ import annotations
import base64
import hashlib
import hmac
import io
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import date
from html.parser import HTMLParser
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs, quote
import storage as store

VERSION = '2.0.0'
WEB = Path(__file__).resolve().parent / 'web'


class PlainNotes(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []
    def handle_data(self, data): self.parts.append(data)
    def handle_starttag(self, tag, attrs):
        if tag in {'br', 'p', 'div', 'li'}: self.parts.append('\n')
    @classmethod
    def convert(cls, text):
        if '<' not in text: return text
        p = cls(); p.feed(text); return ''.join(p.parts).strip()


def snapshot(db):
    tagmap = {t['id']: t['name'] for t in store.tags(db)}
    def view(t):
        result = dict(t)
        result['note'] = PlainNotes.convert(t['body'])
        result['tags'] = [{'id': i, 'name': tagmap[i]} for i in store.task_tag_ids(db, t['id']) if i in tagmap]
        result['files'] = [{**dict(f), 'url': '/api/file?id=' + str(f['id'])} for f in store.attachments(db, t['id'])]
        result['checklist'] = [dict(c) for c in store.checklist_items(db, t['id'])]
        result['milestones'] = [dict(m) for m in store.milestones(db, t['id'])]
        return result
    return {'tasks': [view(t) for t in store.tasks(db)],
            'trash': [view(t) for t in store.tasks(db, trashed=True)],
            'archived': [view(t) for t in store.tasks(db, archived=True)],
            'tags': [dict(t) for t in store.tags(db)],
            'activity': [dict(a) for a in store.activity(db, 300)],
            'names': store.get_display_names(db), 'view_state': store.get_board_view_state(db),
            'reminder_mode': store.get_reminder_mode(db), 'today': date.today().isoformat(), 'version': VERSION}


def safe_file(name):
    if not isinstance(name, str) or not __import__('re').fullmatch(r'[a-f0-9]{32}(?:\.[a-zA-Z0-9]{1,11})?', name):
        raise ValueError('附件存储路径无效')
    p = (store.FILES / name).resolve()
    if p.parent != store.FILES.resolve(): raise ValueError('附件路径无效')
    return p


def save_draft(db, item):
    """Commit task, manual checklist, milestones and attachments together.

    Files are staged before the SQLite transaction; failed saves remove only
    new files, leaving previous references and files untouched.
    """
    title = str(item.get('title', '')).strip()
    if not title or len(title) > 240: raise ValueError('任务标题需为 1–240 字')
    status = item.get('status', '计划中')
    if status not in store.STATES: raise ValueError('任务阶段无效')
    due = item.get('due') or None
    if due: date.fromisoformat(due)
    tid = int(item['id']) if item.get('id') else None
    old = store.task(db, tid) if tid else None
    if tid and (not old or old['archived_at']): raise ValueError('任务不存在或已归档')
    if old and item.get('updated_at') != old['updated_at']:
        raise ValueError('任务已在其他页面更新，请关闭后重新打开，避免覆盖')
    tag_ids = list(dict.fromkeys(int(i) for i in item.get('tags', [])))
    for tag in tag_ids:
        if not db.execute('SELECT 1 FROM tags WHERE id=?', (tag,)).fetchone(): raise ValueError('标签不存在')
    checks = item.get('checklist', [])
    milestones = item.get('milestones', [])
    for c in checks:
        if not str(c.get('label', '')).strip() or len(c['label']) > 120: raise ValueError('检查项需为 1–120 字')
    for m in milestones:
        if not str(m.get('name', '')).strip() or len(m['name']) > 120: raise ValueError('里程碑需为 1–120 字')
    previous_milestones = {m['id']: m for m in store.milestones(db, tid)} if tid else {}
    old_files = {f['id']: f for f in store.attachments(db, tid)} if tid else {}
    removed = {int(i) for i in item.get('removed_files', [])}
    if not removed <= old_files.keys(): raise ValueError('附件不属于当前任务')
    versions = item.get('file_versions', [])
    for f in versions:
        if int(f['id']) not in old_files: raise ValueError('附件不属于当前任务')
    staged = []
    total_size = 0
    try:
        for f in item.get('new_files', []):
            raw = base64.b64decode(f['data'], validate=True)
            total_size += len(raw)
            if total_size > store.MAX_FILE: raise ValueError('本次新增附件总大小不能超过 100 MB')
            if not raw or len(raw) > store.MAX_FILE: raise ValueError('单个附件不能为空且不能超过 100 MB')
            name = Path(str(f['name']).replace('\\', '/')).name[:240]
            ext = Path(name).suffix.lower()
            stored = uuid.uuid4().hex + (ext if __import__('re').fullmatch(r'\.[a-z0-9]{1,11}', ext) else '.bin')
            path = safe_file(stored)
            staged.append((path, name, stored))
            path.write_bytes(raw)
        stamp = __import__('datetime').datetime.now().isoformat(timespec='microseconds')
        with db:
            rank = old['sort_order'] if old and old['status'] == status else db.execute('SELECT COALESCE(MAX(sort_order),0)+1000 FROM tasks WHERE status=?', (status,)).fetchone()[0]
            if tid:
                db.execute('UPDATE tasks SET title=?,body=?,status=?,due_date=?,sort_order=?,updated_at=? WHERE id=?',
                           (title, item.get('note', ''), status, due, rank, stamp, tid))
            else:
                tid = db.execute('INSERT INTO tasks(title,body,status,due_date,created_at,updated_at,sort_order) VALUES(?,?,?,?,?,?,?)',
                                 (title, item.get('note', ''), status, due, stamp, stamp, rank)).lastrowid
            store._replace_tags(db, tid, tag_ids)
            db.execute('DELETE FROM checklist_items WHERE task_id=?', (tid,))
            db.executemany('INSERT INTO checklist_items(task_id,label,done,sort_order) VALUES(?,?,?,?)',
                           [(tid, c['label'].strip(), int(bool(c.get('done'))), n) for n, c in enumerate(checks)])
            db.execute('DELETE FROM milestones WHERE task_id=?', (tid,))
            for n, m in enumerate(milestones):
                prior = previous_milestones.get(m.get('id'))
                confirmed = bool(m.get('confirmed'))
                confirmed_at = ((prior['confirmed_at'] if prior and prior['confirmed'] else stamp) if confirmed else None)
                db.execute('INSERT INTO milestones(task_id,name,confirmed,confirmed_at,sort_order) VALUES(?,?,?,?,?)',
                           (tid, m['name'].strip(), int(confirmed), confirmed_at, n))
            for fid in removed: db.execute('DELETE FROM attachments WHERE id=?', (fid,))
            for f in versions:
                db.execute('UPDATE attachments SET version_label=?,version_note=? WHERE id=?',
                           (str(f.get('version_label', '初稿'))[:20], str(f.get('version_note', ''))[:200], int(f['id'])))
            for path, name, stored in staged:
                # Only inert raster types may be displayed inline by the server.
                is_image = Path(name).suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
                db.execute('INSERT INTO attachments(task_id,filename,stored_name,is_image,created_at) VALUES(?,?,?,?,?)',
                           (tid, name, stored, int(is_image), stamp))
            store._event(db, tid, title, '编辑任务' if old else '创建任务', '内容、检查清单、里程碑与附件一并保存')
    except Exception:
        for path, _, _ in staged: path.unlink(missing_ok=True)
        raise
    # Failure to unlink is harmless: health check reports the orphan; references
    # have already committed and must never be rolled back after this point.
    for fid in removed:
        try: safe_file(old_files[fid]['stored_name']).unlink(missing_ok=True)
        except OSError: pass
    return tid


class TaskServer(HTTPServer):
    allow_reuse_address = False
    def __init__(self, address=('127.0.0.1', 0)):
        if address[0] != '127.0.0.1': raise ValueError('仅允许监听回环地址')
        self.capability = secrets.token_urlsafe(32)
        self.sessions = {}
        self.failures = 0
        self.retry_at = 0
        super().__init__(address, Handler)
    @property
    def origin(self): return f'http://127.0.0.1:{self.server_port}'
    @property
    def launch_url(self): return self.origin + '/#' + self.capability


class Handler(BaseHTTPRequestHandler):
    server_version = 'TaskBoard'
    def log_message(self, *args): pass  # Never log capabilities, cookies or passwords.
    def respond(self, status, data=None, content=None, mime='application/json; charset=utf-8', headers=None):
        if content is None: content = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)
    def run_request(self):
        db = None
        upload_path = None
        try:
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                return self.respond(403, {'error': 'Host 无效'})
            path = urlsplit(self.path).path
            if self.command == 'GET' and path in {'/', '/app.js', '/style.css'}:
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                return self.respond(200, content=(WEB / name).read_bytes(), mime={'/':'text/html', '/app.js':'text/javascript', '/style.css':'text/css'}[path] + '; charset=utf-8')
            jar = cookies.SimpleCookie()
            jar.load(self.headers.get('Cookie', ''))
            cookie_name = 'tb_session_' + str(self.server.server_port)
            sid = jar[cookie_name].value if cookie_name in jar else ''
            authenticated = self.server.sessions.get(sid, 0) > time.time()
            if path not in {'/api/login', '/api/status'} and not authenticated:
                return self.respond(401, {'error': '请先登录'})
            if self.command == 'POST':
                if self.headers.get('Origin') != self.server.origin or not hmac.compare_digest(self.headers.get('X-TaskBoard', ''), self.server.capability):
                    return self.respond(403, {'error': '请求来源无效，请从程序入口打开'})
                length = int(self.headers.get('Content-Length', '0'))
                binary_restore = path == '/api/restore' and self.headers.get('Content-Type') == 'application/zip'
                limit = store.MAX_BACKUP_FILE if binary_restore else 145 * 1024 * 1024
                if length < 0 or length > limit:
                    return self.respond(413, {'error': '上传文件超过大小限制'})
                self.connection.settimeout(30)
                if binary_restore:
                    import tempfile
                    fd, filename = tempfile.mkstemp(prefix='upload-', suffix='.zip', dir=store.ROOT)
                    upload_path = Path(filename)
                    with os.fdopen(fd, 'wb') as target:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk: raise ValueError('上传内容不完整')
                            target.write(chunk); remaining -= len(chunk)
                    item = {}
                else:
                    if self.headers.get('Content-Type') != 'application/json': raise ValueError('仅接受 JSON 请求')
                    raw = self.rfile.read(length)
                    if len(raw) != length: raise ValueError('上传内容不完整')
                    item = json.loads(raw or b'{}')
                    if not isinstance(item, dict): raise ValueError('请求数据无效')
            else: item = {}
            # Read-only status includes no user data.
            db = store.connect()
            if path == '/api/status' and self.command == 'GET':
                return self.respond(200, {'first': not store.has_password(db), 'authenticated': authenticated, 'version': VERSION})
            if path == '/api/login' and self.command == 'POST':
                if time.time() < self.server.retry_at: return self.respond(429, {'error': '尝试过于频繁，请稍后再试'})
                password = str(item.get('password', ''))
                if not store.has_password(db):
                    if password != item.get('confirm'): raise ValueError('两次密码不一致')
                    store.set_password(db, password)
                elif not store.check_password(db, password):
                    self.server.failures += 1
                    if self.server.failures >= 5: self.server.retry_at = time.time() + 30
                    return self.respond(401, {'error': '密码错误'})
                self.server.failures = 0
                sid = secrets.token_urlsafe(32)
                self.server.sessions[sid] = time.time() + 12 * 3600
                return self.respond(200, snapshot(db), headers={'Set-Cookie': f'{cookie_name}={sid}; HttpOnly; SameSite=Strict; Path=/'})
            if not authenticated: return self.respond(401, {'error': '请先登录'})
            if self.command == 'GET': return self.read_api(path, db)
            if self.command != 'POST': return self.respond(405, {'error': '请求方法无效'})
            if path == '/api/lock':
                self.server.sessions.clear()
                return self.respond(200, {'ok': True}, headers={'Set-Cookie': f'{cookie_name}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/'})
            if path == '/api/shutdown':
                self.server.sessions.clear()
                self.respond(200, {'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == '/api/task/save': save_draft(db, item)
            elif path == '/api/task/action':
                actions = {'move': lambda: store.move(db, int(item['id']), item['status'], item.get('index')),
                           'trash': lambda: store.trash(db, int(item['id'])),
                           'restore': lambda: store.restore_task(db, int(item['id'])),
                           'erase': lambda: store.erase(db, int(item['id'])),
                           'duplicate': lambda: store.duplicate_task(db, int(item['id'])),
                           'archive': lambda: store.archive_completed(db, int(item['id'])),
                           'unarchive': lambda: store.unarchive_task(db, int(item['id']))}
                action = actions.get(item.get('action'))
                if not action: raise ValueError('操作无效')
                action()
            elif path == '/api/tag':
                action = item.get('action')
                if action == 'create': store.add_tag(db, item['name'])
                elif action == 'rename': store.rename_tag(db, int(item['id']), item['name'])
                elif action == 'delete': store.delete_tag(db, int(item['id']))
                else: raise ValueError('标签操作无效')
            elif path == '/api/bulk':
                ids = [int(i) for i in item['ids']]
                if item['action'] == 'status': result = store.bulk_update_status(db, ids, item['status'])
                elif item['action'] == 'tag': result = store.bulk_add_tag(db, ids, item['tag'])
                elif item['action'] == 'trash': result = store.bulk_trash(db, ids)
                else: raise ValueError('批量操作无效')
                return self.respond(200, {'result': result, 'state': snapshot(db)})
            elif path == '/api/settings':
                store.set_display_names(db, item['system_name'], item['user_name'])
                store.set_reminder_mode(db, item['reminder_mode'])
            elif path == '/api/view':
                store.set_board_view_state(db, item.get('tag_filter'), item.get('query'))
                return self.respond(200, {'ok': True})
            elif path == '/api/password':
                if not store.check_password(db, item['old']): raise ValueError('旧密码错误')
                if item['new'] != item['confirm']: raise ValueError('两次密码不一致')
                store.set_password(db, item['new'])
                self.server.sessions = {sid: time.time() + 12 * 3600}
            elif path == '/api/restore':
                if upload_path is None: raise ValueError('请选择完整 ZIP 备份')
                try: fresh, safety = store.restore_backup(db, upload_path)
                except Exception:
                    # Validation can fail before restore closes the old handle.
                    # Always close it before reconnecting (Windows locks files).
                    db.close()
                    db = store.connect()
                    raise
                else: db = fresh
                self.server.sessions = {sid: time.time() + 12 * 3600}
                return self.respond(200, {'state': snapshot(db), 'safety': safety.name})
            else: return self.respond(404, {'error': '接口不存在'})
            return self.respond(200, snapshot(db))
        except (ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError, zipfile.BadZipFile) as exc:
            return self.respond(400, {'error': str(exc)})
        except OSError:
            return self.respond(500, {'error': '文件读写失败，请检查数据目录和磁盘空间'})
        except Exception:
            return self.respond(500, {'error': '操作失败，数据未能完成保存'})
        finally:
            if db is not None: db.close()
            if upload_path is not None: upload_path.unlink(missing_ok=True)
    def read_api(self, path, db):
        if path == '/api/state': return self.respond(200, snapshot(db))
        if path == '/api/health': return self.respond(200, store.attachment_health_check(db))
        if path == '/api/file':
            params = parse_qs(urlsplit(self.path).query)
            f = db.execute('SELECT * FROM attachments WHERE id=?', (int(params.get('id', ['0'])[0]),)).fetchone()
            if not f: return self.respond(404, {'error': '附件不存在'})
            p = safe_file(f['stored_name'])
            if not p.is_file(): return self.respond(404, {'error': '附件文件缺失'})
            mime = mimetypes.guess_type(f['filename'])[0] or 'application/octet-stream'
            inline = mime in {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp'} and params.get('download') != ['1']
            return self.respond(200, content=p.read_bytes(), mime=mime, headers={'Content-Disposition': ('inline' if inline else 'attachment') + "; filename*=UTF-8''" + quote(f['filename'])})
        if path in {'/api/backup', '/api/export/csv', '/api/export/xlsx'}:
            ext = 'zip' if path == '/api/backup' else path.rsplit('/', 1)[-1]
            with __import__('tempfile').TemporaryDirectory(dir=store.ROOT) as tmp:
                p = Path(tmp) / ('TaskBoard-' + date.today().isoformat() + '.' + ext)
                if ext == 'zip': store.backup(db, p)
                elif ext == 'csv': store.export_csv(db, p)
                else: store.export_xlsx(db, p)
                return self.respond(200, content=p.read_bytes(), mime='application/octet-stream', headers={'Content-Disposition': f'attachment; filename="{p.name}"'})
        return self.respond(404, {'error': '接口不存在'})
    do_GET = run_request
    do_POST = run_request
