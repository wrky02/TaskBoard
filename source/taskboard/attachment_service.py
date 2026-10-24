"""Private native attachment staging, immutable versions and byte access.

Only user-selected exports materialize a plaintext copy. Drafts store random
tokens; chunks and completed objects use the workspace's encryption adapter.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
import storage as s

CHUNK = 256 * 1024


def read_bytes(path):
    import vault
    return vault.read_bytes(Path(path))


def write_bytes(path, data):
    import vault
    vault.write_bytes(Path(path), data)


def file_size(path):
    import vault
    return vault.file_size(Path(path))


def migrate(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS native_staged_files(
            token TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
            received INTEGER NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, sha256 TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS attachment_versions(
            file_id INTEGER PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
            family TEXT NOT NULL, current INTEGER NOT NULL DEFAULT 1,
            size INTEGER NOT NULL, sha256 TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_attachment_family ON attachment_versions(family);
        CREATE INDEX IF NOT EXISTS idx_attachment_task ON attachments(task_id,id);
    ''')


def token_path(token):
    if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{32}', token):
        raise ValueError('附件暂存标识无效')
    return s.ROOT / 'staging' / token


def _staged(db, token, finished=False):
    token_path(token)
    row = db.execute('SELECT * FROM native_staged_files WHERE token=?', (token,)).fetchone()
    if not row or (finished and not row['finished']):
        raise ValueError('附件尚未完成导入或暂存已失效，请重新选择')
    return dict(row)


def stage_start(db, item):
    name = Path(str(item.get('name', '')).replace('\\', '/')).name[:240]
    size = int(item.get('size', 0))
    if not name or size <= 0 or size > s.MAX_FILE:
        raise ValueError('请选择非空附件，单个附件不得超过 100 MB')
    token = uuid.uuid4().hex
    token_path(token).mkdir(parents=True)
    with db:
        db.execute('INSERT INTO native_staged_files(token,name,size,created) VALUES (?,?,?,?)',
                   (token, name, size, time.time()))
    return dict(token=token, chunk_size=CHUNK)


def stage_chunk(db, item):
    row = _staged(db, item['token'])
    raw = base64.b64decode(item['data'], validate=True)
    offset = int(item['offset'])
    if row['finished'] or offset != row['received'] or offset % CHUNK or not raw or len(raw) > CHUNK:
        raise ValueError('附件分块顺序无效，请重新导入')
    if offset + len(raw) > row['size'] or (len(raw) < CHUNK and offset + len(raw) != row['size']):
        raise ValueError('附件分块长度不一致')
    write_bytes(token_path(row['token']) / f'{offset // CHUNK:08d}.chunk', raw)
    with db:
        db.execute('UPDATE native_staged_files SET received=? WHERE token=?', (offset + len(raw), row['token']))
    return dict(received=offset + len(raw), size=row['size'])


def stage_finish(db, item):
    row = _staged(db, item['token'])
    if row['received'] != row['size']:
        raise ValueError('附件尚未传输完成')
    folder = token_path(row['token'])
    if not row['finished']:
        data = b''.join(read_bytes(folder / f'{n:08d}.chunk') for n in range((row['size'] + CHUNK - 1) // CHUNK))
        if len(data) != row['size']:
            raise ValueError('附件暂存长度校验失败')
        digest = hashlib.sha256(data).hexdigest()
        write_bytes(folder / 'object', data)
        with db:
            db.execute('UPDATE native_staged_files SET finished=1,sha256=? WHERE token=?', (digest, row['token']))
        for path in folder.glob('*.chunk'):
            path.unlink(missing_ok=True)
    row = _staged(db, row['token'], True)
    return dict(staged=dict(token=row['token'], name=row['name'], size=row['size'],
                           is_image=is_image(row['name']), sha256=row['sha256']))


def stage_abort(db, item):
    row = _staged(db,item['token'])
    if row['finished']:
        return dict(retained=True)
    # A persisted draft is a real reference. Removing a UI selection must not
    # invalidate another saved draft; unreferenced objects can be swept later.
    for draft in db.execute('SELECT data FROM drafts'):
        if row['token'] in draft[0]:
            return dict(retained=True)
    import shutil
    shutil.rmtree(token_path(row['token']), ignore_errors=True)
    with db:
        db.execute('DELETE FROM native_staged_files WHERE token=?', (row['token'],))
    return {}


def is_image(name):
    return Path(name).suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}


def staged_content(db, item):
    row = _staged(db, item['token'], True)
    data = read_bytes(token_path(row['token']) / 'object')
    if len(data) != row['size'] or hashlib.sha256(data).hexdigest() != row['sha256']:
        raise ValueError('附件暂存校验失败，请重新选择文件')
    return row['name'], data


def describe(db, item):
    if item.get('token'):
        row = _staged(db, item['token'], True)
        return row['name'], token_path(row['token']) / 'object', row['size']
    row = db.execute('SELECT * FROM attachments WHERE id=?', (int(item.get('file_id', item.get('id', 0))),)).fetchone()
    if not row:
        raise ValueError('附件不存在')
    from domain import safe_file
    path = safe_file(row['stored_name'])
    return row['filename'], path, file_size(path)


def register(db, file_id, data, replaces_id=None):
    family = uuid.uuid4().hex
    if replaces_id:
        previous = db.execute('SELECT a.*,v.family FROM attachments a LEFT JOIN attachment_versions v ON a.id=v.file_id WHERE a.id=?', (int(replaces_id),)).fetchone()
        current = db.execute('SELECT task_id FROM attachments WHERE id=?', (file_id,)).fetchone()
        if not previous or previous['task_id'] != current['task_id']:
            raise ValueError('待替换的附件不属于此任务')
        family = previous['family'] or uuid.uuid4().hex
        if previous['family'] is None:
            old_data = read_bytes(s.FILES / previous['stored_name'])
            db.execute('INSERT INTO attachment_versions VALUES (?,?,?,?,?)',
                       (previous['id'], family, 1, len(old_data), hashlib.sha256(old_data).hexdigest()))
        db.execute('UPDATE attachment_versions SET current=0 WHERE family=?', (family,))
    db.execute('INSERT INTO attachment_versions VALUES (?,?,?,?,?)',
               (file_id, family, 1, len(data), hashlib.sha256(data).hexdigest()))


def versions(db, file_id):
    row = db.execute('SELECT family FROM attachment_versions WHERE file_id=?', (file_id,)).fetchone()
    if row:
        rows = db.execute('SELECT a.*,v.size,v.sha256,v.current FROM attachments a JOIN attachment_versions v ON a.id=v.file_id WHERE v.family=? ORDER BY a.id DESC', (row[0],))
    else:
        rows = db.execute("SELECT *,0 AS size,'' AS sha256,1 AS current FROM attachments WHERE id=?", (file_id,))
    result = []
    for item in rows:
        value = dict(item)
        if not value['size']:
            value['size'] = file_size(s.FILES / value['stored_name'])
        value['is_current'] = bool(value.pop('current'))
        value.pop('stored_name', None)
        result.append(value)
    return dict(versions=result)


def restore_version(db, file_id):
    row = db.execute('SELECT a.*,v.family FROM attachments a JOIN attachment_versions v ON a.id=v.file_id WHERE a.id=?', (file_id,)).fetchone()
    if not row:
        raise ValueError('此附件没有可切换的版本链')
    task = s.task(db, row['task_id'])
    if not task or task['archived_at']:
        raise ValueError('请先恢复任务，再切换附件版本')
    read_bytes(s.FILES / row['stored_name'])  # authenticate before changing references
    import recovery
    with db:
        recovery.history_record(db, row['task_id'], '恢复附件前')
        db.execute('UPDATE attachment_versions SET current=0 WHERE family=?', (row['family'],))
        db.execute('UPDATE attachment_versions SET current=1 WHERE file_id=?', (file_id,))
        db.execute('UPDATE tasks SET updated_at=? WHERE id=?', (s.now(), row['task_id']))
        s._event(db, row['task_id'], task['title'], '恢复附件版本', row['filename'])
        recovery.history_record(db, row['task_id'], '恢复附件版本')
    return dict(id=row['task_id'])


def handle(db, method, item):
    actions = {'attachment-stage-start':stage_start, 'attachment-stage-chunk':stage_chunk,
               'attachment-stage-finish':stage_finish, 'attachment-stage-abort':stage_abort}
    if method in actions:
        return actions[method](db, item)
    if method == 'attachment-versions':
        return versions(db, int(item['id']))
    if method == 'attachment-version-restore':
        return restore_version(db, int(item['id']))
    name, source, size = describe(db, item)
    if method == 'attachment-read':
        if not is_image(name) or size > s.MAX_FILE:
            raise ValueError('此文件请另存后使用对应程序打开')
        return dict(name=name, size=size, is_image=True, data=base64.b64encode(read_bytes(source)).decode())
    if method == 'attachment-export':
        target = Path(item['path']).resolve()
        if target == s.ROOT.resolve() or s.ROOT.resolve() in target.parents:
            raise ValueError('请选择工作区数据目录之外的位置')
        # The user explicitly chooses the plaintext export destination.
        temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('wb') as file:
                file.write(read_bytes(source)); file.flush(); os.fsync(file.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return dict(path=str(target))
    raise ValueError('附件操作无效')
