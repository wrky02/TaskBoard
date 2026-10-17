"""Shared task application rules. No HTTP server, browser or GUI imports."""
import base64
import json
import uuid
from pathlib import Path
from datetime import date
from html.parser import HTMLParser
import storage as store
import features
VERSION = '2.1.0'


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
        result['options'] = features.options(db,t['id'])
        result['dependencies'] = features.dependencies(db,t['id'])
        result['blocked'] = features.blocked(db,t['id'])
        result['note'] = PlainNotes.convert(t['body'])
        result['tags'] = [{'id': i, 'name': tagmap[i], 'color':(db.execute('SELECT color FROM tag_colors WHERE tag_id=?',(i,)).fetchone() or ['purple'])[0]} for i in store.task_tag_ids(db, t['id']) if i in tagmap]
        result['files'] = [{**dict(f), 'size': (store.FILES/f['stored_name']).stat().st_size if (store.FILES/f['stored_name']).is_file() else 0, 'url': '/api/file?id=' + str(f['id'])} for f in store.attachments(db, t['id'])]
        result['checklist'] = [dict(c) for c in store.checklist_items(db, t['id'])]
        result['milestones'] = [dict(m) for m in store.milestones(db, t['id'])]
        return result
    return {'tasks': [view(t) for t in store.tasks(db)],
            'trash': [view(t) for t in store.tasks(db, trashed=True)],
            'archived': [view(t) for t in store.tasks(db, archived=True)],
            'tags': [{**dict(t), 'color': (db.execute('SELECT color FROM tag_colors WHERE tag_id=?',(t['id'],)).fetchone() or ['purple'])[0], 'count':db.execute('SELECT COUNT(*) FROM task_tags WHERE tag_id=?',(t['id'],)).fetchone()[0]} for t in store.tags(db)],
            'views':[{**dict(r),'filters':json.loads(r['data'])} for r in db.execute('SELECT * FROM saved_views ORDER BY name')],
            'info':features.backup_info(db),
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
                features.save_options(db,tid,item.get('options'),item.get('dependencies',[r['id'] for r in features.dependencies(db,tid)]),due)
                features.validate_completion(db,tid,status)
                db.execute('UPDATE tasks SET title=?,body=?,status=?,due_date=?,sort_order=?,updated_at=? WHERE id=?',
                           (title, item.get('note', ''), status, due, rank, stamp, tid))
            else:
                tid = db.execute('INSERT INTO tasks(title,body,status,due_date,created_at,updated_at,sort_order) VALUES(?,?,?,?,?,?,?)',
                                 (title, item.get('note', ''), status, due, stamp, stamp, rank)).lastrowid
            if not old:
                features.save_options(db,tid,item.get('options'),item.get('dependencies',[]),due)
                features.validate_completion(db,tid,status)
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
            features.completed(db,tid,old['status'] if old else None)
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


