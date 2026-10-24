"""Transactional 2.1 workspace features; no browser or Windows dependencies."""
import calendar
import json
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
import storage as s

DEFAULT = dict(priority=2, today='', repeat='off', repeat_basis='due', anchor=0,
               reminder='off', reminder_time='09:00', snooze=0, acknowledged='')
COLORS = ('purple', 'blue', 'green', 'orange', 'red', 'gray')

def migrate(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='task_options'").fetchone(): return
    if db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]:
        s.backup(db, s.ROOT / ('before-v2.1-'+uuid.uuid4().hex[:8]+'.zip'))
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE task_options(task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, data TEXT NOT NULL)')
        db.execute('CREATE TABLE task_dependencies(task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE, prerequisite INTEGER REFERENCES tasks(id), PRIMARY KEY(task_id,prerequisite), CHECK(task_id<>prerequisite))')
        db.execute('CREATE TABLE recurrence(source INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, successor INTEGER REFERENCES tasks(id) ON DELETE SET NULL)')
        db.execute('CREATE TABLE saved_views(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, data TEXT NOT NULL)')
        db.execute('CREATE TABLE tag_colors(tag_id INTEGER PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE, color TEXT NOT NULL)')
        db.execute('CREATE TABLE drafts(id TEXT PRIMARY KEY, data TEXT NOT NULL, updated TEXT NOT NULL)')
        db.execute('CREATE TABLE undo_actions(id TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL)')

def options(db, tid):
    row=db.execute('SELECT data FROM task_options WHERE task_id=?',(tid,)).fetchone()
    return {**DEFAULT, **(json.loads(row[0]) if row else {})}

def put_options(db,tid,value):
    db.execute('INSERT OR REPLACE INTO task_options VALUES (?,?)',(tid,json.dumps(value,ensure_ascii=False)))

def dependencies(db,tid):
    return [dict(r) for r in db.execute('SELECT t.id,t.title,t.status,t.deleted_at FROM tasks t JOIN task_dependencies d ON d.prerequisite=t.id WHERE d.task_id=? ORDER BY t.id',(tid,))]

def blocked(db,tid):
    return [r for r in dependencies(db,tid) if r['status']!='完成' or r['deleted_at']]

def validate_completion(db,tid,status):
    if status=='完成' and blocked(db,tid): raise ValueError('请先完成依赖任务，或移除依赖：'+ '、'.join(r['title'] for r in blocked(db,tid)))

def save_options(db,tid,values,dep_ids,due):
    old=options(db,tid); value={**old,**{k:v for k,v in (values or {}).items() if k in DEFAULT and k not in ('snooze','acknowledged','anchor')}}
    if type(value['priority']) is not int or value['priority'] not in range(4): raise ValueError('优先级无效')
    if value['today']: date.fromisoformat(value['today'])
    if value['repeat'] not in ('off','daily','weekly','monthly') or value['repeat_basis'] not in ('due','completion'): raise ValueError('重复规则无效')
    if value['reminder'] not in ('off','today','before') or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value['reminder_time']): raise ValueError('提醒时间无效')
    if not due and (value['repeat']!='off' or value['reminder']!='off'): raise ValueError('重复任务或提醒需要截止日期')
    previous=db.execute('SELECT due_date FROM tasks WHERE id=?',(tid,)).fetchone()[0]
    if due and (not old['anchor'] or previous!=due): value['anchor']=date.fromisoformat(due).day
    if previous!=due or any(old[k]!=value[k] for k in ('reminder','reminder_time')):
        value['snooze']=0; value['acknowledged']=''
    ids=list(dict.fromkeys(int(x) for x in dep_ids))
    if len(ids)>100: raise ValueError('每项任务最多 100 个依赖')
    previous_ids={r['id'] for r in dependencies(db,tid)}
    graph={}
    for r in db.execute('SELECT * FROM task_dependencies'): graph.setdefault(r['task_id'],[]).append(r['prerequisite'])
    for target in ids:
        row=s.task(db,target,True)
        if not row or (row['deleted_at'] and target not in previous_ids): raise ValueError('依赖任务不存在或在回收站')
        todo=[target]; seen=set()
        while todo:
            node=todo.pop()
            if node==tid: raise ValueError('不能依赖自身或形成循环依赖')
            if node not in seen: seen.add(node); todo.extend(graph.get(node,[]))
    db.execute('DELETE FROM task_dependencies WHERE task_id=?',(tid,))
    db.executemany('INSERT INTO task_dependencies VALUES (?,?)',[(tid,x) for x in ids])
    put_options(db,tid,value)

def next_due(due,rule,basis,anchor,today=None):
    today=today or date.today(); base=date.fromisoformat(due) if basis=='due' else today
    if rule in ('daily','weekly'):
        days=1 if rule=='daily' else 7
        count=max(1,(today-base).days//days+1) if basis=='due' else 1
        try: return (base+timedelta(days=count*days)).isoformat()
        except OverflowError: raise ValueError('下一次日期超出支持范围')
    day=anchor if basis=='due' else base.day
    for n in range(1,120001):
        total=base.year*12+base.month-1+n; year,month=divmod(total,12);month+=1
        if year>9999: raise ValueError('下一次日期超出支持范围')
        result=date(year,month,min(day,calendar.monthrange(year,month)[1]))
        if result>today:return result.isoformat()
    raise ValueError('重复规则超出范围')

def completed(db,tid,previous_status):
    task=s.task(db,tid); value=options(db,tid)
    if not task or task['status']!='完成' or previous_status=='完成' or value['repeat']=='off':return None
    if db.execute('SELECT 1 FROM recurrence WHERE source=?',(tid,)).fetchone():return None
    due=next_due(task['due_date'],value['repeat'],value['repeat_basis'],value['anchor'])
    stamp=s.now();rank=db.execute("SELECT COALESCE(MAX(sort_order),0)+1000 FROM tasks WHERE status='计划中'").fetchone()[0]
    child=db.execute("INSERT INTO tasks(title,body,status,due_date,created_at,updated_at,sort_order) VALUES (?,?,'计划中',?,?,?,?)",(task['title'],task['body'],due,stamp,stamp,rank)).lastrowid
    value.update(today='',snooze=0,acknowledged='');put_options(db,child,value)
    for table,columns in [('task_tags','tag_id'),('task_dependencies','prerequisite')]:
        db.execute(f'INSERT INTO {table} SELECT ?,{columns} FROM {table} WHERE task_id=?',(child,tid))
    db.execute('INSERT INTO checklist_items(task_id,label,sort_order) SELECT ?,label,sort_order FROM checklist_items WHERE task_id=?',(child,tid))
    db.execute('INSERT INTO milestones(task_id,name,sort_order) SELECT ?,name,sort_order FROM milestones WHERE task_id=?',(child,tid))
    db.execute('INSERT INTO recurrence VALUES (?,?)',(tid,child))
    s._event(db,child,task['title'],'生成重复任务',f'来源 #{tid}；截止 {due}；不复制附件')
    return child

def reminder_key(task,value):return f"{task['due_date']}|{value['reminder']}|{value['reminder_time']}"

def reminders(db,at=None):
    at=at or datetime.now();result=[]
    for task in s.tasks(db):
        value=options(db,task['id'])
        if task['status']=='完成' or not task['due_date'] or value['reminder']=='off':continue
        key=reminder_key(task,value)
        when=datetime.fromisoformat(task['due_date']+'T'+value['reminder_time'])
        if value['reminder']=='before':when-=timedelta(days=1)
        if key!=value['acknowledged'] and at>=when and at.timestamp()>=value['snooze']:
            result.append(dict(id=task['id'],title=task['title'],due=task['due_date'],key=key))
    return result

def acknowledge(db,item):
    task=s.task(db,int(item['id']))
    if not task:raise ValueError('任务不存在')
    value=options(db,task['id'])
    if item['key']!=reminder_key(task,value):raise ValueError('提醒已更新，请刷新后重试')
    if item.get('action')=='snooze':
        minutes=int(item.get('minutes',15))
        if minutes not in (15,60,1440):raise ValueError('稍后提醒时间无效')
        value['snooze']=time.time()+minutes*60
    elif item.get('action')=='ack':value['acknowledged']=item['key'];value['snooze']=0
    else:raise ValueError('提醒操作无效')
    with db:put_options(db,task['id'],value)

def backup_info(db):
    items=sorted((s.ROOT/'backups').glob('auto-*.zip'),reverse=True) if (s.ROOT/'backups').exists() else []
    return dict(version='2.1.0', data_path=str(s.ROOT), last_backup=items[0].name if items else None,
                backup_count=len(items), backup_error=s.get_setting(db,'backup_error',''), close_behavior=s.get_setting(db,'close_behavior','ask'))

def auto_backup(db):
    folder=s.ROOT/'backups';folder.mkdir(exist_ok=True)
    path=folder/('auto-'+date.today().isoformat()+'.zip')
    if path.exists():return
    temp=folder/('pending-'+uuid.uuid4().hex+'.zip')
    try:
        s.backup(db,temp);temp.replace(path)
        for stale in sorted(folder.glob('auto-*.zip'),reverse=True)[7:]:stale.unlink()
    finally:temp.unlink(missing_ok=True)

def preview_backup(path):
    import zipfile,tempfile
    s.validate_backup(path)
    with zipfile.ZipFile(path) as z, tempfile.TemporaryDirectory() as tmp:
        target=Path(tmp)/'tasks.sqlite3';target.write_bytes(z.read('tasks.sqlite3'))
        db=sqlite3.connect(target)
        try:return dict(tasks=db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],attachments=db.execute('SELECT COUNT(*) FROM attachments').fetchone()[0],bytes=sum(i.file_size for i in z.infolist()),modified='-'.join(map(str,z.getinfo('tasks.sqlite3').date_time[:3])))
        finally:db.close()

def diagnostics(db):
    import platform
    return dict(version='2.1.0',os=platform.system(),os_version=platform.release(),python=platform.python_version(),sqlite=sqlite3.sqlite_version,
                tasks=db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],attachments=db.execute('SELECT COUNT(*) FROM attachments').fetchone()[0],
                integrity=db.execute('PRAGMA integrity_check').fetchone()[0],backups=backup_info(db)['backup_count'],
                excluded=['任务正文','任务标题','附件内容与名称','用户名','本地路径','密码与启动凭据','草稿内容'])

def save_view(db,item):
    if item.get('action')=='delete':
        with db:db.execute('DELETE FROM saved_views WHERE id=?',(int(item['id']),))
        return
    name=str(item['name']).strip()
    if not name or len(name)>40:raise ValueError('视图名称需为 1–40 字')
    value=item['filters']
    if not isinstance(value,dict) or len(json.dumps(value))>4000:raise ValueError('筛选条件无效')
    with db:db.execute('INSERT INTO saved_views(name,data) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET data=excluded.data',(name,json.dumps(value)))

def tag_update(db,item):
    tid=int(item['id'])
    if not db.execute('SELECT 1 FROM tags WHERE id=?',(tid,)).fetchone():raise ValueError('标签不存在')
    with db:
        if item['action']=='color':
            if item['color'] not in COLORS:raise ValueError('颜色无效')
            db.execute('INSERT OR REPLACE INTO tag_colors VALUES (?,?)',(tid,item['color']))
        elif item['action']=='merge':
            target=int(item['target'])
            if target==tid or not db.execute('SELECT 1 FROM tags WHERE id=?',(target,)).fetchone():raise ValueError('合并目标无效')
            db.execute('INSERT OR IGNORE INTO task_tags SELECT task_id,? FROM task_tags WHERE tag_id=?',(target,tid))
            db.execute('DELETE FROM tags WHERE id=?',(tid,))
            s._event(db,None,'标签','合并标签',f'#{tid} → #{target}')

def save_draft(db,item):
    key=str(item['key'])
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',key):raise ValueError('草稿编号无效')
    if item.get('action')=='delete':
        with db:db.execute('DELETE FROM drafts WHERE id=?',(key,))
        return
    data=json.dumps(item['draft'],ensure_ascii=False)
    if len(data.encode())>140*1024*1024:raise ValueError('草稿过大')
    if db.execute('SELECT COUNT(*) FROM drafts').fetchone()[0]>=20 and not db.execute('SELECT 1 FROM drafts WHERE id=?',(key,)).fetchone():raise ValueError('草稿超过 20 份，请先恢复或删除旧草稿')
    with db:db.execute('INSERT OR REPLACE INTO drafts VALUES (?,?,?)',(key,data,s.now()))

def undo_record(db,tid):
    task=s.task(db,tid,True)
    return {**{k:task[k] for k in ('id','status','sort_order','deleted_at','archived_at','updated_at')},'ordering':[dict(r) for r in db.execute('SELECT id,status,sort_order,updated_at FROM tasks WHERE deleted_at IS NULL')]}

def make_undo(db,before):
    after=undo_record(db,before['id']);key=uuid.uuid4().hex
    prior={r['id']:r for r in before.pop('ordering')}
    changed=[dict(before=prior[r['id']],after=r) for r in after.pop('ordering') if r['id']!=before['id'] and r['id'] in prior and any(r[k]!=prior[r['id']][k] for k in ('status','sort_order'))]
    with db:
        db.execute('DELETE FROM undo_actions WHERE expires<?',(time.time(),))
        db.execute('INSERT INTO undo_actions VALUES (?,?,?)',(key,json.dumps(dict(before=before,after=after,ordering=changed)),time.time()+15))
    return key

def undo(db,key):
    row=db.execute('SELECT * FROM undo_actions WHERE id=?',(key,)).fetchone()
    if not row or row['expires']<time.time():raise ValueError('撤销时间已过')
    data=json.loads(row['data']);tid=data['before']['id'];current=s.task(db,tid,True)
    if not current or current['updated_at']!=data['after']['updated_at']:raise ValueError('任务已有后续修改，无法撤销')
    child=None
    if data['before']['status']!='完成' and data['after']['status']=='完成':
        emitted=db.execute('SELECT successor FROM recurrence WHERE source=?',(tid,)).fetchone()
        if emitted:
            child=s.task(db,emitted[0],True) if emitted[0] else None
            if not child or child['updated_at']!=child['created_at'] or db.execute('SELECT 1 FROM task_dependencies WHERE prerequisite=?',(child['id'],)).fetchone():raise ValueError('下一次重复任务已有修改，不能自动撤销，请手动处理')
    for change in data.get('ordering',[]):
        neighbor=s.task(db,change['after']['id'],True)
        if not neighbor or neighbor['updated_at']!=change['after']['updated_at']:raise ValueError('任务排序已有后续修改，无法撤销')
    with db:
        for change in data.get('ordering',[]):
            previous=change['before'];db.execute('UPDATE tasks SET status=?,sort_order=?,updated_at=? WHERE id=?',(previous['status'],previous['sort_order'],s.now(),previous['id']))
        before=data['before'];validate_completion(db,tid,before['status'])
        if child:
            db.execute('DELETE FROM recurrence WHERE source=?',(tid,))
            db.execute('DELETE FROM tasks WHERE id=?',(child['id'],))
        db.execute('UPDATE tasks SET status=?,sort_order=?,deleted_at=?,archived_at=?,updated_at=? WHERE id=?',tuple(before[k] for k in ('status','sort_order','deleted_at','archived_at'))+(s.now(),tid))
        db.execute('DELETE FROM undo_actions WHERE id=?',(key,));s._event(db,tid,current['title'],'撤销操作')

def directory_backup(files,destination):
    import base64,zipfile,hashlib,json,sqlite3,tempfile
    from pathlib import Path
    seen=set();total=0
    contents={}
    for file in files:
        name=file['name']
        # Older migration clients may include the source backup manifest;
        # validate imported data files and generate a fresh destination manifest.
        if name=='manifest.json':continue
        if name in seen or (name!='tasks.sqlite3' and not re.fullmatch(r'attachments/[a-f0-9]{32}(?:\.[a-zA-Z0-9]{1,11})?',name)):raise ValueError('旧数据文件路径无效或重复')
        seen.add(name);data=base64.b64decode(file['data'],validate=True);total+=len(data)
        if total>100*1024*1024:raise ValueError('目录导入超过 100 MB，请从旧版导出 ZIP 完整备份后导入')
        contents[name]=data
    if 'tasks.sqlite3' not in contents:raise ValueError('旧数据目录缺少数据库')
    with tempfile.TemporaryDirectory() as tmp:
        dbpath=Path(tmp)/'tasks.sqlite3';dbpath.write_bytes(contents['tasks.sqlite3'])
        check=sqlite3.connect(dbpath);check.row_factory=sqlite3.Row
        try:
            if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('旧版数据库损坏')
            if any('attachments/'+r[0] not in contents for r in check.execute('SELECT stored_name FROM attachments')):raise ValueError('旧数据目录缺少附件')
        finally:check.close()
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
        manifest={'format':'taskboard-backup','format_version':2,'files':{}}
        for name,data in contents.items():
            z.writestr(name,data);manifest['files'][name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
    return preview_backup(destination)
