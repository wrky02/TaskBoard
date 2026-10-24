"""Shared task application rules. No HTTP server, browser or GUI imports."""
import base64
import json
import uuid
from pathlib import Path
from datetime import date
from html.parser import HTMLParser
import storage as store
import features
import pro_features
import recovery
import attachment_service as attachments
from collections import defaultdict
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


def snapshot(db, task_ids=None):
    """Build a snapshot with a fixed number of queries, never N+1 per task."""
    params=tuple(dict.fromkeys(map(int,task_ids))) if task_ids is not None else ()
    restriction=(' WHERE id IN ('+','.join('?' for _ in params)+')') if params else (' WHERE 0' if task_ids is not None else '')
    task_restriction=restriction.replace('id IN','task_id IN')
    rows=[dict(t) for t in db.execute('SELECT * FROM tasks'+restriction+' ORDER BY sort_order,id DESC',params)]
    taskmap = {t['id']: t for t in rows}
    options = {r['task_id']: json.loads(r['data']) for r in db.execute('SELECT * FROM task_options'+task_restriction,params)}
    tags = {t['id']: dict(t) for t in db.execute('SELECT * FROM tags ORDER BY name COLLATE NOCASE')}
    colors = {r['tag_id']: r['color'] for r in db.execute('SELECT * FROM tag_colors')}
    tasktags, deps, checks, milestones, files = (defaultdict(list) for _ in range(5))
    counts = defaultdict(int)
    for r in db.execute('SELECT * FROM task_tags'+task_restriction+' ORDER BY tag_id',params):
        if r['tag_id'] in tags:
            t = tags[r['tag_id']]
            tasktags[r['task_id']].append(dict(id=t['id'], name=t['name'], color=colors.get(t['id'], 'purple')))
            counts[t['id']] += 1
    target_rows=db.execute('SELECT t.id,t.title,t.status,t.deleted_at,d.task_id FROM task_dependencies d JOIN tasks t ON t.id=d.prerequisite'+task_restriction.replace('task_id IN','d.task_id IN')+' ORDER BY t.id',params)
    for r in target_rows:
        target=dict(r)
        if target:
            deps[r['task_id']].append({k: target[k] for k in ('id','title','status','deleted_at')})
    for table, groups in [('checklist_items', checks), ('milestones', milestones)]:
        for r in db.execute(f'SELECT * FROM {table}'+task_restriction+' ORDER BY task_id,sort_order,id',params):
            groups[r['task_id']].append(dict(r))
    for r in db.execute('SELECT a.*,v.size AS cached_size,v.current FROM attachments a LEFT JOIN attachment_versions v ON v.file_id=a.id'+task_restriction.replace('task_id IN','a.task_id IN')+' ORDER BY a.id',params):
        if r['current'] == 0:
            continue
        f = dict(r); cached = f.pop('cached_size'); f.pop('current')
        path = store.FILES / f['stored_name']
        f['size'] = cached if cached is not None else attachments.file_size(path) if path.is_file() else 0
        f['url'] = '/api/file?id=' + str(f['id'])
        files[f['task_id']].append(f)
    result = {'tasks': [], 'trash': [], 'archived': []}
    for t in rows:
        tid=t['id']
        t.update(options={**features.DEFAULT, **options.get(tid, {})}, dependencies=deps[tid],
                 blocked=[d for d in deps[tid] if d['status']!='完成' or d['deleted_at']],
                 note=PlainNotes.convert(t['body']), tags=tasktags[tid], files=files[tid],
                 checklist=checks[tid], milestones=milestones[tid])
        result['trash' if t['deleted_at'] else 'archived' if t['archived_at'] else 'tasks'].append(t)
    result.update(tags=[dict(t, color=colors.get(t['id'],'purple'), count=counts[t['id']]) for t in tags.values()],
                  views=[{**dict(r),'filters':json.loads(r['data'])} for r in db.execute('SELECT * FROM saved_views ORDER BY name')],
                  info=features.backup_info(db), activity=[dict(a) for a in store.activity(db,300)],
                  names=store.get_display_names(db), view_state=store.get_board_view_state(db),
                  reminder_mode=store.get_reminder_mode(db), today=date.today().isoformat(), version=VERSION)
    return pro_features.enrich_snapshot(db, result)


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
    item = dict(item)
    pro_features.validate_save(db, item)
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
            if f.get('token'):
                filename, raw = attachments.staged_content(db, f)
            else:
                filename, raw = f['name'], base64.b64decode(f['data'], validate=True)
            total_size += len(raw)
            if total_size > store.MAX_FILE: raise ValueError('本次新增附件总大小不能超过 100 MB')
            if not raw or len(raw) > store.MAX_FILE: raise ValueError('单个附件不能为空且不能超过 100 MB')
            name = Path(str(filename).replace('\\', '/')).name[:240]
            ext = Path(name).suffix.lower()
            stored = uuid.uuid4().hex + (ext if __import__('re').fullmatch(r'\.[a-z0-9]{1,11}', ext) else '.bin')
            path = safe_file(stored)
            staged.append((path,name,stored,raw,f.get('replaces_id'),f.get('history_version') if item.get('_trusted_history') else None))
            attachments.write_bytes(path, raw)
        stamp = __import__('datetime').datetime.now().isoformat(timespec='microseconds')
        with db:
            if old:
                recovery.history_record(db, tid, '修改前')
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
            for path, name, stored, raw, replaces_id, history_version in staged:
                # Only inert raster types may be displayed inline by the server.
                is_image = Path(name).suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
                fid = db.execute('INSERT INTO attachments(task_id,filename,stored_name,is_image,created_at) VALUES(?,?,?,?,?)',
                           (tid, name, stored, int(is_image), stamp)).lastrowid
                attachments.register(db,fid,raw,replaces_id)
                if history_version:
                    db.execute('UPDATE attachment_versions SET family=?,current=? WHERE file_id=?',(history_version['family'],int(bool(history_version['current'])),fid))
                    db.execute('UPDATE attachments SET version_label=?,version_note=? WHERE id=?',(history_version.get('version_label','初稿'),history_version.get('version_note',''),fid))
            pro_features.saved(db, tid, item)
            features.completed(db,tid,old['status'] if old else None)
            store._event(db, tid, title, '编辑任务' if old else '创建任务', '内容、检查清单、里程碑与附件一并保存')
            recovery.history_record(db, tid, item.get('_history_reason', '正式保存'))
            if item.get('_operation_id'):
                db.execute("UPDATE native_operations SET status='done',result=? WHERE id=?",
                           (json.dumps(dict(id=tid)), item['_operation_id']))
    except Exception:
        for path, *_ in staged: path.unlink(missing_ok=True)
        raise
    # Failure to unlink is harmless: health check reports the orphan; references
    # have already committed and must never be rolled back after this point.
    for fid in removed:
        try: recovery.retain_removed_file(db, old_files[fid]['stored_name'])
        except OSError: pass
    return tid


