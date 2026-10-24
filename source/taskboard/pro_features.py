"""Local personal-workspace domain services. No network or UI dependencies.

All task hooks participate in the caller's SQLite transaction. Public ``handle``
mutations are atomic. Stable UUIDs survive content export/import; integer IDs are
local display addresses. Advanced recurrence is explicitly opt-in and never
changes the legacy complete-then-repeat interpretation.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import json
import sqlite3
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import storage as s
import features as f

KINDS = {'todo': '计划中', 'doing': '进行中', 'done': '完成', 'cancelled': '计划中'}
STATUS_KINDS = {'计划中': 'todo', '进行中': 'doing', '完成': 'done'}
META_FIELDS = ('project_id', 'parent_id', 'state_id', 'cancelled', 'inbox',
               'plan_date', 'focus', 'estimate_minutes', 'time_blocks')
MAX_IMPORT_BYTES = 100 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _day(value, label='日期', optional=True):
    if not value and optional:
        return ''
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ValueError(label + '应为 YYYY-MM-DD') from None


def _instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('时间须为包含时区偏移的 ISO 时间，例如 2026-10-19T09:00:00+08:00') from None


def _int(value, low=0, high=2_147_483_647, label='数值'):
    if isinstance(value, bool):
        raise ValueError(label + '无效')
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(label + '无效') from None
    if result < low or result > high:
        raise ValueError(label + '超出支持范围')
    return result


def _zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Standard-library-only packaged builds still support the default zone.
        if name == 'Asia/Shanghai':
            return timezone(timedelta(hours=8))
        if name in ('UTC', 'Etc/UTC'):
            return timezone.utc
        raise ValueError('此系统缺少所选时区数据，请安装时区数据后重试') from None


def migrate(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS pro_projects(
        id INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        goal TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT 'purple',
        start_date TEXT NOT NULL DEFAULT '', target_date TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS pro_states(
        id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES pro_projects(id),
        name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('todo','doing','done','cancelled')),
        sort_order INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS pro_tasks(
        task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        uid TEXT UNIQUE NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        project_id INTEGER REFERENCES pro_projects(id),
        parent_id INTEGER REFERENCES tasks(id), state_id INTEGER REFERENCES pro_states(id),
        cancelled INTEGER NOT NULL DEFAULT 0, inbox INTEGER NOT NULL DEFAULT 0,
        plan_date TEXT NOT NULL DEFAULT '', focus INTEGER NOT NULL DEFAULT 0,
        estimate_minutes INTEGER NOT NULL DEFAULT 0, time_blocks TEXT NOT NULL DEFAULT '[]',
        last_status TEXT NOT NULL DEFAULT '计划中', completed_at TEXT,
        CHECK(parent_id IS NULL OR parent_id<>task_id));
      CREATE INDEX IF NOT EXISTS idx_pro_tasks_project ON pro_tasks(project_id, parent_id);
      CREATE INDEX IF NOT EXISTS idx_pro_tasks_parent ON pro_tasks(parent_id);
      CREATE INDEX IF NOT EXISTS idx_pro_tasks_plan ON pro_tasks(plan_date,focus);
      CREATE TABLE IF NOT EXISTS pro_links(
        a INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
        b INTEGER REFERENCES tasks(id) ON DELETE CASCADE, PRIMARY KEY(a,b), CHECK(a<b));
      CREATE TABLE IF NOT EXISTS pro_views(
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS pro_events(
        id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        task_uid TEXT NOT NULL, event TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
      CREATE INDEX IF NOT EXISTS idx_pro_events_at ON pro_events(at,event);
      CREATE TABLE IF NOT EXISTS pro_series(
        id INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL,
        seed_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        rule TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL, updated TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS pro_instances(
        series_id INTEGER REFERENCES pro_series(id) ON DELETE CASCADE,
        occurrence TEXT NOT NULL, task_id INTEGER UNIQUE REFERENCES tasks(id) ON DELETE SET NULL,
        skipped INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(series_id,occurrence));
      CREATE TABLE IF NOT EXISTS pro_reminders(
        id INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        revision INTEGER NOT NULL DEFAULT 1, snooze_until TEXT, delivered_at TEXT);
      CREATE INDEX IF NOT EXISTS idx_pro_reminders_due ON pro_reminders(status,at);
    ''')
    with db:
        if not db.execute('SELECT 1 FROM pro_projects').fetchone():
            pid = db.execute('INSERT INTO pro_projects(uid,name) VALUES (?,?)',
                             (uuid.uuid4().hex, '原工作看板')).lastrowid
            _default_states(db, pid)
        pid = _default_project(db)
        states = {r['kind']: r['id'] for r in db.execute('SELECT * FROM pro_states WHERE project_id=? ORDER BY sort_order,id', (pid,))}
        for task in db.execute('SELECT id,status FROM tasks WHERE id NOT IN (SELECT task_id FROM pro_tasks)').fetchall():
            db.execute('INSERT INTO pro_tasks(task_id,uid,project_id,state_id,last_status) VALUES(?,?,?,?,?)',
                       (task['id'], uuid.uuid4().hex, pid, states.get(STATUS_KINDS.get(task['status'], 'todo')), task['status']))


def _default_states(db, pid):
    for n, (name, kind) in enumerate((('计划中', 'todo'), ('进行中', 'doing'), ('完成', 'done'), ('已取消', 'cancelled'))):
        db.execute('INSERT INTO pro_states(project_id,name,kind,sort_order) VALUES(?,?,?,?)', (pid, name, kind, n))


def _default_project(db):
    row = db.execute('SELECT id FROM pro_projects ORDER BY id LIMIT 1').fetchone()
    return row[0] if row else None


def _state_for(db, project, status, cancelled=False):
    if project is None:
        return None
    kind = 'cancelled' if cancelled else STATUS_KINDS[status]
    row = db.execute('SELECT id FROM pro_states WHERE project_id=? AND kind=? ORDER BY sort_order,id LIMIT 1', (project, kind)).fetchone()
    if not row:
        raise ValueError('目标项目缺少对应状态：' + kind)
    return row[0]


def _meta(row):
    result = dict(row)
    result['time_blocks'] = json.loads(result.get('time_blocks') or '[]')
    for key in ('cancelled', 'inbox', 'focus'):
        result[key] = bool(result.get(key))
    return result


def get_meta(db, tid):
    row = db.execute('SELECT * FROM pro_tasks WHERE task_id=?', (tid,)).fetchone()
    if row:
        return _meta(row)
    task = db.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()
    status = task['status'] if task else '计划中'
    pid = _default_project(db)
    return dict(task_id=tid, uid='', revision=0, project_id=pid, parent_id=None,
                state_id=_state_for(db, pid, status), cancelled=False, inbox=False,
                plan_date='', focus=False, estimate_minutes=0, time_blocks=[],
                last_status=status, completed_at=None)


def _task(db, tid):
    result = db.execute('SELECT * FROM tasks WHERE id=?', (int(tid),)).fetchone()
    if not result:
        raise ValueError('任务不存在')
    return result


def _event(db, tid, event, data=None):
    meta = get_meta(db, tid)
    db.execute('INSERT INTO pro_events(task_id,task_uid,event,at,data) VALUES(?,?,?,?,?)',
               (tid, meta['uid'], event, _now(), _json(data or {})))


def _validate_graph(db, tid, parent, deps):
    # A parent's completion waits for its children. Child->parent dependencies
    # would therefore create a real completion deadlock, not just a hierarchy.
    graph = {}
    for row in db.execute('SELECT task_id,prerequisite FROM task_dependencies'):
        if row['task_id'] != tid:
            graph.setdefault(row['task_id'], []).append(row['prerequisite'])
    for row in db.execute('SELECT task_id,parent_id FROM pro_tasks WHERE parent_id IS NOT NULL'):
        if row['task_id'] != tid:
            graph.setdefault(row['parent_id'], []).append(row['task_id'])
    graph.setdefault(tid, []).extend(deps)
    if parent:
        graph.setdefault(parent, []).append(tid)
    visiting, done = set(), set()
    def visit(node):
        if node in visiting:
            raise ValueError('父子关系与硬依赖会形成完成等待循环')
        if node in done:
            return
        visiting.add(node)
        for other in graph.get(node, ()):
            visit(other)
        visiting.remove(node)
        done.add(node)
    for node in graph:
        visit(node)


def validate_transition(db, tid, status):
    if status != '完成':
        return
    children = db.execute('SELECT t.title FROM pro_tasks p JOIN tasks t ON t.id=p.task_id WHERE p.parent_id=? AND t.deleted_at IS NULL AND p.cancelled=0 AND t.status<>?', (tid, '完成')).fetchall()
    if children:
        raise ValueError('请先完成或取消子任务：' + '、'.join(r[0] for r in children[:5]))
    cancelled = db.execute('SELECT t.title FROM task_dependencies d JOIN tasks t ON t.id=d.prerequisite LEFT JOIN pro_tasks p ON p.task_id=t.id WHERE d.task_id=? AND COALESCE(p.cancelled,0)=1', (tid,)).fetchall()
    if cancelled:
        raise ValueError('已取消的上游仍不满足依赖，请解除或替换：' + '、'.join(r[0] for r in cancelled[:5]))


def validate_save(db, item):
    tid = int(item.get('id') or 0)
    old = get_meta(db, tid)
    supplied = item.get('pro') or {}
    if not isinstance(supplied, dict):
        raise ValueError('专业任务字段无效')
    data = {**old, **{k: supplied[k] for k in META_FIELDS if k in supplied}}
    project = data.get('project_id')
    project = int(project) if project not in (None, '', 0) else None
    parent = int(data.get('parent_id') or 0) or None
    state = int(data.get('state_id') or 0) or None
    if project is not None:
        p = db.execute('SELECT * FROM pro_projects WHERE id=?', (project,)).fetchone()
        if not p or p['archived']:
            raise ValueError('项目不存在或已归档')
    if parent:
        ptask = _task(db, parent)
        pm = get_meta(db, parent)
        if parent == tid or ptask['deleted_at'] or ptask['archived_at'] or pm['parent_id']:
            raise ValueError('父任务必须是同项目中未归档的顶层任务，最多两级')
        if pm['project_id'] != project:
            raise ValueError('父任务与子任务必须属于同一项目')
        if tid and db.execute('SELECT 1 FROM pro_tasks WHERE parent_id=?', (tid,)).fetchone():
            raise ValueError('有子任务的任务不能再设置父任务')
    if tid and old['project_id'] != project:
        if db.execute('SELECT 1 FROM pro_tasks WHERE parent_id=?', (tid,)).fetchone():
            raise ValueError('请使用项目迁移操作移动整个任务及其子任务')
    status = item.get('status', '计划中')
    cancelled = bool(data.get('cancelled'))
    # An explicitly changed custom state wins. A legacy status update should not
    # be accidentally overwritten by the unchanged custom state in a snapshot.
    if project != old.get('project_id'):
        # A new task inherits the workspace default state as a convenience.
        # When moving projects, keep an explicitly selected state only if it
        # belongs to the destination; otherwise choose its matching state.
        row = db.execute('SELECT * FROM pro_states WHERE id=?', (state,)).fetchone() if state else None
        if row and row['project_id'] == project:
            status = KINDS[row['kind']]
            cancelled = row['kind'] == 'cancelled'
        else:
            if status != old.get('last_status'):
                cancelled = False
            state = _state_for(db, project, status, cancelled)
    elif state and (state != old.get('state_id') or status == old.get('last_status')):
        row = db.execute('SELECT * FROM pro_states WHERE id=?', (state,)).fetchone()
        if not row or row['project_id'] != project:
            raise ValueError('任务状态不属于所选项目')
        status = KINDS[row['kind']]
        cancelled = row['kind'] == 'cancelled'
    else:
        if status != old.get('last_status'):
            cancelled = False
        state = _state_for(db, project, status, cancelled)
    if tid:
        item['_pro_previous_due'] = _task(db,tid)['due_date']
    data.update(project_id=project, parent_id=parent, state_id=state, cancelled=cancelled,
                inbox=bool(data['inbox']), focus=bool(data['focus']))
    if data['inbox'] and parent:
        raise ValueError('子任务不能同时放在收件箱中')
    data['plan_date'] = _day(data.get('plan_date'), '计划日期')
    data['estimate_minutes'] = _int(data.get('estimate_minutes', 0), 0, 24*60*365, '预计分钟数')
    blocks = data.get('time_blocks') or []
    if not isinstance(blocks, list) or len(blocks) > 100:
        raise ValueError('单任务最多安排 100 个时间块')
    parsed = []
    for block in blocks:
        begin, end = _instant(block.get('start')), _instant(block.get('end'))
        if end <= begin or end - begin > timedelta(days=7):
            raise ValueError('时间块结束须晚于开始，且最长七天')
        parsed.append(dict(start=begin.isoformat(), end=end.isoformat()))
    parsed.sort(key=lambda b: b['start'])
    if any(a['end'] > b['start'] for a, b in zip(parsed, parsed[1:])):
        raise ValueError('同一任务的时间块不能重叠')
    data['time_blocks'] = parsed
    if data['focus'] and not old['focus']:
        count = db.execute("SELECT COUNT(*) FROM pro_tasks p JOIN tasks t ON t.id=p.task_id WHERE p.focus=1 AND p.task_id<>? AND p.plan_date=? AND t.status<>'完成' AND t.deleted_at IS NULL AND p.cancelled=0", (tid, data['plan_date'])).fetchone()[0]
        if count >= 3:
            raise ValueError('同一天最多设置三项重点，请先取消一项')
    deps = item.get('dependencies')
    if deps is None:
        deps = [r[0] for r in db.execute('SELECT prerequisite FROM task_dependencies WHERE task_id=?', (tid,))]
    deps = [int(x['id'] if isinstance(x, dict) else x) for x in deps]
    _validate_graph(db, tid or -1, parent, deps)
    item['status'] = status
    item['pro'] = {**supplied, **{k: data[k] for k in META_FIELDS}}
    if not cancelled:
        validate_transition(db, tid, status)
    if 'related_ids' in supplied:
        ids = list(dict.fromkeys(int(x) for x in supplied['related_ids']))
        if tid in ids or len(ids) > 100:
            raise ValueError('关联任务不能包含自身且最多 100 项')
        for target in ids:
            _task(db, target)
        item['pro']['related_ids'] = ids
    if supplied.get('series'):
        rule = normalize_rule(supplied['series'])
        existing = db.execute('SELECT 1 FROM pro_instances WHERE task_id=?', (tid,)).fetchone()
        if rule['basis'] == 'scheduled' and not existing and not supplied.get('series_opt_in'):
            raise ValueError('固定时间自动产生实例需要明确确认，并先预览未来日期')
        if parent:
            raise ValueError('高级重复任务请先解除父任务关系，避免重复实例进入已完成父任务')
        item['pro']['series'] = rule
        item['options'] = {**(item.get('options') or f.options(db, tid)), 'repeat': 'off'}
    if 'reminders' in supplied:
        _validate_reminders(supplied['reminders'])


def saved(db, tid, item):
    old = get_meta(db, tid)
    data = {**old, **{k: item.get('pro', {}).get(k, old[k]) for k in META_FIELDS}}
    task = _task(db, tid)
    status = task['status']
    # New core status is authoritative if the hook is used after a legacy move.
    if 'pro' not in item:
        data['state_id'] = _state_for(db, data['project_id'], status, data['cancelled'])
    uid = old['uid'] or uuid.uuid4().hex
    previous_status = old.get('last_status', '计划中') if old['uid'] else '计划中'
    completed_at = old.get('completed_at')
    if status == '完成' and previous_status != '完成' and not data['cancelled']:
        completed_at = _now()
    fields = tuple(data[k] if k != 'time_blocks' else _json(data[k]) for k in META_FIELDS)
    db.execute('''INSERT INTO pro_tasks(task_id,uid,revision,project_id,parent_id,state_id,cancelled,inbox,plan_date,focus,estimate_minutes,time_blocks,last_status,completed_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
      revision=excluded.revision,project_id=excluded.project_id,parent_id=excluded.parent_id,
      state_id=excluded.state_id,cancelled=excluded.cancelled,inbox=excluded.inbox,
      plan_date=excluded.plan_date,focus=excluded.focus,estimate_minutes=excluded.estimate_minutes,
      time_blocks=excluded.time_blocks,last_status=excluded.last_status,completed_at=excluded.completed_at''',
      (tid, uid, old['revision'] + 1, *fields, status, completed_at))
    pro = item.get('pro', {})
    if 'related_ids' in pro:
        db.execute('DELETE FROM pro_links WHERE a=? OR b=?', (tid, tid))
        for target in pro['related_ids']:
            db.execute('INSERT INTO pro_links(a,b) VALUES (?,?)', tuple(sorted((tid, target))))
    if 'reminders' in pro:
        save_reminders(db, tid, pro['reminders'])
    if 'series' in pro and pro.get('series_scope', 'future') != 'current':
        if pro['series']:
            save_series(db, tid, pro['series'], pro.get('series_opt_in', False))
        else:
            row = db.execute('SELECT series_id FROM pro_instances WHERE task_id=?', (tid,)).fetchone()
            if row:
                db.execute('UPDATE pro_series SET paused=1,updated=? WHERE id=?', (_now(), row[0]))
                db.execute('DELETE FROM pro_instances WHERE task_id=?', (tid,))
    if status == '完成' and previous_status != '完成' and not data['cancelled']:
        _event(db, tid, 'completed')
        advance_completion(db, tid)
    elif status != '完成' and previous_status == '完成':
        _event(db, tid, 'reopened')
    if data['cancelled'] and not old['cancelled']:
        _event(db, tid, 'cancelled')
    old_due = item.get('_pro_previous_due')
    if old_due and task['due_date'] and task['due_date'] > old_due:
        _event(db, tid, 'postponed', dict(before=old_due, after=task['due_date']))


def transitioned(db, tid, status=None):
    task = _task(db, tid)
    old = get_meta(db, tid)
    pro = {k: old[k] for k in META_FIELDS}
    pro.update(state_id=_state_for(db, old['project_id'], task['status']), cancelled=False)
    saved(db, tid, dict(status=task['status'], pro=pro))


def inherit_legacy(db, source, successor):
    """Called by legacy completion without enabling schedule-based recurrence."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='pro_tasks'").fetchone():
        return
    old = get_meta(db, source)
    pro = {k: old[k] for k in META_FIELDS}
    # Parent may already be complete; independent successor avoids hidden work.
    pro.update(parent_id=None, state_id=_state_for(db, old['project_id'], '计划中'),
               cancelled=False, focus=False, plan_date='', time_blocks=[])
    saved(db, successor, dict(status='计划中', pro=pro))


def enrich_snapshot(db, result):
    projects=[dict(r) for r in db.execute('SELECT * FROM pro_projects ORDER BY sort_order,id')]
    states=[dict(r) for r in db.execute('SELECT * FROM pro_states ORDER BY project_id,sort_order,id')]
    taskmap={t['id']:t for group in ('tasks','archived','trash') for t in result.get(group,[])}
    ids=list(taskmap);marks=','.join('?'*len(ids));partial=len(ids)<1000
    suffix=(' WHERE task_id IN ('+marks+')' if ids else ' WHERE 0') if partial else ''
    params=ids if partial else []
    metas={r['task_id']:_meta(r) for r in db.execute('SELECT * FROM pro_tasks'+suffix,params)}
    projectmap={p['id']:p for p in projects};default=projects[0]['id'] if projects else None
    for tid,task in taskmap.items():
        if tid not in metas:
            sid=next((st['id'] for st in states if st['project_id']==default and st['kind']==STATUS_KINDS.get(task['status'],'todo')),None)
            metas[tid]=dict(task_id=tid,uid='',revision=0,project_id=default,parent_id=None,state_id=sid,cancelled=False,inbox=False,plan_date='',focus=False,estimate_minutes=0,time_blocks=[],last_status=task['status'],completed_at=None)
    links={}
    linkwhere=(' WHERE a IN ('+marks+') OR b IN ('+marks+')' if ids else ' WHERE 0') if partial else ''
    for row in db.execute('SELECT a,b FROM pro_links'+linkwhere,ids+ids if partial else []):
        links.setdefault(row['a'],[]).append(row['b']);links.setdefault(row['b'],[]).append(row['a'])
    reminders={}
    for row in db.execute('SELECT * FROM pro_reminders'+suffix+' ORDER BY at,id',params):
        reminders.setdefault(row['task_id'],[]).append(dict(row))
    series={}
    isuffix=suffix.replace('task_id','i.task_id')
    for row in db.execute('SELECT i.task_id,i.occurrence,s.id,s.uid,s.rule,s.paused FROM pro_instances i JOIN pro_series s ON s.id=i.series_id'+isuffix,params):
        rule=json.loads(row['rule']);rule.update(id=row['id'],uid=row['uid'],paused=bool(row['paused']),occurrence=row['occurrence']);series[row['task_id']]=rule
    children={}
    childwhere=(' WHERE p.parent_id IN ('+marks+')' if ids else ' WHERE 0') if partial else ' WHERE p.parent_id IS NOT NULL'
    for row in db.execute('SELECT p.parent_id,p.task_id,p.cancelled,t.status,t.deleted_at FROM pro_tasks p JOIN tasks t ON t.id=p.task_id'+childwhere,params):
        children.setdefault(row['parent_id'],[]).append(dict(row))
    for tid,task in taskmap.items():
        meta=dict(metas[tid]);childrows=children.get(tid,[]);live=[r for r in childrows if not r['deleted_at']]
        meta.update(related_ids=links.get(tid,[]),reminders=reminders.get(tid,[]),series=series.get(tid),
          children=[r['task_id'] for r in childrows],child_progress=dict(total=len(live),done=sum(r['status']=='完成' and not r['cancelled'] for r in live),cancelled=sum(bool(r['cancelled']) for r in live)),
          project_archived=bool(projectmap.get(meta['project_id'],{}).get('archived')),project_name=projectmap.get(meta['project_id'],{}).get('name','收件箱'))
        task['pro']=meta
    result.update(projects=projects,project_states=states,pro_views=[{**dict(r),'filters':json.loads(r['data'])} for r in db.execute('SELECT * FROM pro_views ORDER BY sort_order,id')])
    return result


def snapshot(db):
    import domain
    result = domain.snapshot(db)
    return result if 'projects' in result else enrich_snapshot(db, result)


def _norm(text):
    return unicodedata.normalize('NFKC', str(text or '')).casefold()


def query(db, item):
    """Filter/count/page in SQLite before loading task details, including Chinese substrings."""
    db.create_function('pro_norm', 1, _norm, deterministic=True)
    scope=item.get('scope','active');conditions=[];params=[]
    if scope=='active':conditions += ['t.deleted_at IS NULL','t.archived_at IS NULL','COALESCE(j.archived,0)=0']
    elif scope=='archived':conditions += ['t.deleted_at IS NULL','(t.archived_at IS NOT NULL OR COALESCE(j.archived,0)=1)']
    elif scope=='trash':conditions += ['t.deleted_at IS NOT NULL']
    elif scope!='all':raise ValueError('搜索范围无效')
    words=_norm(item.get('query','')).split()
    for word in words:
        conditions.append("""(instr(pro_norm(t.title),?)>0 OR instr(pro_norm(t.body),?)>0
          OR instr(pro_norm(j.name),?)>0
          OR EXISTS(SELECT 1 FROM checklist_items c WHERE c.task_id=t.id AND instr(pro_norm(c.label),?)>0)
          OR EXISTS(SELECT 1 FROM task_tags tt JOIN tags g ON g.id=tt.tag_id WHERE tt.task_id=t.id AND instr(pro_norm(g.name),?)>0)
          OR EXISTS(SELECT 1 FROM attachments a WHERE a.task_id=t.id AND instr(pro_norm(a.filename),?)>0))""")
        params.extend([word]*6)
    for field in ('project_id','parent_id'):
        if field in item:
            if item[field] in (None,'',0):conditions.append('p.'+field+' IS NULL')
            else:conditions.append('p.'+field+'=?');params.append(int(item[field]))
    for field in ('inbox','focus'):
        if field in item:conditions.append('COALESCE(p.'+field+',0)=?');params.append(int(bool(item[field])))
    if item.get('priorities'):
        priorities=[_int(x,0,3,'优先级') for x in item['priorities']]
        conditions.append("COALESCE(json_extract(o.data,'$.priority'),2) IN ("+','.join('?'*len(priorities))+')');params.extend(priorities)
    if item.get('states'):
        kinds=[]
        for kind in item['states']:
            kind=STATUS_KINDS.get(kind,kind)
            if kind not in KINDS:raise ValueError('状态筛选无效')
            kinds.append(kind)
        expression="CASE WHEN COALESCE(p.cancelled,0)=1 THEN 'cancelled' WHEN t.status='完成' THEN 'done' WHEN t.status='进行中' THEN 'doing' ELSE 'todo' END"
        conditions.append(expression+' IN ('+','.join('?'*len(kinds))+')');params.extend(kinds)
    tags=list(dict.fromkeys(map(int,item.get('tags',[]))))
    if tags:
        if item.get('tag_mode')=='any':
            conditions.append('EXISTS(SELECT 1 FROM task_tags tt WHERE tt.task_id=t.id AND tt.tag_id IN ('+','.join('?'*len(tags))+'))');params.extend(tags)
        else:
            for tag in tags:conditions.append('EXISTS(SELECT 1 FROM task_tags tt WHERE tt.task_id=t.id AND tt.tag_id=?)');params.append(tag)
    for tag in item.get('exclude_tags',[]):
        conditions.append('NOT EXISTS(SELECT 1 FROM task_tags tt WHERE tt.task_id=t.id AND tt.tag_id=?)');params.append(int(tag))
    if 'blocked' in item:
        blocked="EXISTS(SELECT 1 FROM task_dependencies d JOIN tasks dep ON dep.id=d.prerequisite LEFT JOIN pro_tasks dp ON dp.task_id=dep.id WHERE d.task_id=t.id AND (dep.status<>'完成' OR dep.deleted_at IS NOT NULL OR COALESCE(dp.cancelled,0)=1))"
        conditions.append(('' if item['blocked'] else 'NOT ')+blocked)
    if 'has_files' in item:conditions.append(('' if item['has_files'] else 'NOT ')+'EXISTS(SELECT 1 FROM attachments a WHERE a.task_id=t.id)')
    if item.get('no_due'):conditions.append("(t.due_date IS NULL OR t.due_date='')")
    for column,prefix in (('t.due_date','due'),('p.plan_date','plan')):
        for direction,operator in (('from','>='),('to','<=')):
            if item.get(prefix+'_'+direction):conditions.append(column+operator+'?');params.append(_day(item[prefix+'_'+direction]))
    sort=item.get('sort','manual')
    order={'manual':'t.sort_order,t.id','priority':"COALESCE(json_extract(o.data,'$.priority'),2),t.id",'due':"COALESCE(NULLIF(t.due_date,''),'9999'),t.id",'plan':"COALESCE(NULLIF(p.plan_date,''),'9999'),t.id",'updated':'t.updated_at DESC,t.id DESC'}.get(sort)
    if not order:raise ValueError('排序方式无效')
    offset=_int(item.get('offset',0),0,10_000_000);limit=_int(item.get('limit',200),1,1000)
    base=' FROM tasks t LEFT JOIN pro_tasks p ON p.task_id=t.id LEFT JOIN pro_projects j ON j.id=p.project_id LEFT JOIN task_options o ON o.task_id=t.id'
    where=' WHERE '+' AND '.join(conditions) if conditions else ''
    total=db.execute('SELECT COUNT(*)'+base+where,params).fetchone()[0]
    ids=[r[0] for r in db.execute('SELECT t.id'+base+where+' ORDER BY '+order+' LIMIT ? OFFSET ?',params+[limit,offset])]
    import domain
    state=domain.snapshot(db,ids)
    if 'projects' not in state:enrich_snapshot(db,state)
    taskmap={t['id']:t for group in ('tasks','archived','trash') for t in state.get(group,[])}
    result=[]
    for tid in ids:
        task=taskmap[tid];p=task['pro']
        fields=[('title',task['title']),('note',task.get('note','')),('project',p['project_name'])]
        fields += [('checklist',c['label']) for c in task.get('checklist',[])]+[('tag',g['name']) for g in task.get('tags',[])]+[('attachment',a['filename']) for a in task.get('files',[])]
        task['match_fields']=list(dict.fromkeys(k for k,value in fields if any(w in _norm(value) for w in words)))
        task['scope']='trash' if task.get('deleted_at') else 'archived' if task.get('archived_at') or p['project_archived'] else 'tasks'
        for file in task.get('files',[]):file['path']=str(s.FILES/file['stored_name'])
        result.append(task)
    return dict(tasks=result,total=total,offset=offset,limit=limit)


def impact(db, tid):
    _task(db, tid)
    def rows(sql, args):
        return [dict(r) for r in db.execute(sql,args)]
    return dict(upstream=rows('SELECT t.id,t.title,t.status,t.deleted_at FROM tasks t JOIN task_dependencies d ON t.id=d.prerequisite WHERE d.task_id=?',(tid,)),
                downstream=rows('SELECT t.id,t.title,t.status,t.deleted_at FROM tasks t JOIN task_dependencies d ON t.id=d.task_id WHERE d.prerequisite=?',(tid,)),
                children=rows('SELECT t.id,t.title,t.status,t.deleted_at FROM tasks t JOIN pro_tasks p ON p.task_id=t.id WHERE p.parent_id=?',(tid,)),
                related=rows('SELECT t.id,t.title,t.status FROM tasks t JOIN pro_links l ON t.id=CASE WHEN l.a=? THEN l.b ELSE l.a END WHERE l.a=? OR l.b=?',(tid,tid,tid)))


def review(db, item):
    today = date.today()
    begin = _day(item.get('from') or (today-timedelta(days=today.weekday())).isoformat())
    end = _day(item.get('to') or today.isoformat())
    if end < begin:
        raise ValueError('结束日期不能早于开始日期')
    counts = {r['event']: r['n'] for r in db.execute('SELECT event,COUNT(*) n FROM pro_events WHERE substr(at,1,10)>=? AND substr(at,1,10)<=? GROUP BY event',(begin,end))}
    state = snapshot(db)
    tasks = [t for t in state['tasks'] if not t['pro']['project_archived']]
    active = [t for t in tasks if t['status']!='完成' and not t['pro']['cancelled']]
    completed_ids = {r[0] for r in db.execute("SELECT task_id FROM pro_events WHERE event='completed' AND substr(at,1,10)>=? AND substr(at,1,10)<=?",(begin,end))}
    all_tasks = tasks + state['archived']
    summary = lambda t:dict(id=t['id'],title=t['title'],status=t['status'],due_date=t.get('due_date'),project=t['pro']['project_name'])
    return dict(**{'from':begin,'to':end}, completed_count=counts.get('completed',0), reopened_count=counts.get('reopened',0),
                cancelled_count=counts.get('cancelled',0),postponed_count=counts.get('postponed',0),
                inbox_count=sum(t['pro']['inbox'] for t in active), overdue_count=sum(bool(t.get('due_date')) and t['due_date']<today.isoformat() for t in active),
                completed=[summary(t) for t in all_tasks if t['id'] in completed_ids],
                stale=[summary(t) for t in active if t.get('updated_at','9999')[:10] < (today-timedelta(days=7)).isoformat()],
                upcoming=[summary(t) for t in active if t.get('due_date') and today.isoformat()<=t['due_date']<=(today+timedelta(days=7)).isoformat()],
                focus=[summary(t) for t in active if t['pro']['focus']],
                definition='完成数按已记录的完成事件计算；重新完成计新事件；历史迁移前的完成不推测。日期按 UTC 事件日统计。')


def normalize_rule(rule):
    if not isinstance(rule, dict):
        raise ValueError('重复规则无效')
    frequency = rule.get('frequency', 'daily')
    if frequency not in ('daily','weekly','monthly','monthly_weekday'):
        raise ValueError('重复频率无效')
    basis = rule.get('basis', 'completion')
    if basis not in ('scheduled','completion'):
        raise ValueError('重复基准无效')
    start = _day(rule.get('start'), '重复开始日期', False)
    day = date.fromisoformat(start)
    end = _day(rule.get('end'))
    if end and end < start:
        raise ValueError('重复结束不能早于开始')
    weekdays = sorted(set(_int(x,0,6,'星期') for x in rule.get('weekdays', [day.weekday()])))
    if frequency == 'weekly' and not weekdays:
        raise ValueError('每周重复至少选择一个星期')
    nth = int(rule.get('nth', 1))
    if nth not in (-1,1,2,3,4,5):
        raise ValueError('每月第几个星期应为 1–5 或最后一个')
    tz = str(rule.get('timezone') or 'Asia/Shanghai')
    _zone(tz)
    count = rule.get('count')
    count = _int(count,1,10000,'重复次数') if count not in (None,'',0) else None
    missed = rule.get('missed','skip')
    dependency = rule.get('dependency_mode','fixed')
    if missed not in ('skip','catchup') or dependency not in ('fixed','previous'):
        raise ValueError('重复补发或依赖策略无效')
    return dict(frequency=frequency,interval=_int(rule.get('interval',1),1,365,'重复间隔'),
                weekdays=weekdays,month_day=_int(rule.get('month_day',day.day),1,31,'每月日期'),
                nth=nth,weekday=_int(rule.get('weekday',day.weekday()),0,6,'星期'),start=start,
                basis=basis,end=end,count=count,missed=missed,dependency_mode=dependency,
                timezone=tz,paused=bool(rule.get('paused',False)))


def rule_dates(rule, from_date=None, limit=5, until=None):
    rule = normalize_rule(rule)
    start = date.fromisoformat(rule['start'])
    begin = date.fromisoformat(_day(from_date or rule['start']))
    finish = date.fromisoformat(rule['end']) if rule['end'] else date.max
    if until:
        finish = min(finish, date.fromisoformat(_day(until)))
    if begin > finish:
        return []
    limit = _int(limit,1,1000,'预览数量')
    current, ordinal, result = start, 0, []
    # The bound prevents a hostile/imported rule from hanging the private RPC.
    for _ in range(366*101):
        if current > finish:
            break
        months = (current.year-start.year)*12 + current.month-start.month
        if rule['frequency']=='daily':
            matched = (current-start).days % rule['interval'] == 0
        elif rule['frequency']=='weekly':
            anchor = start - timedelta(days=start.weekday())
            matched = ((current-anchor).days//7) % rule['interval']==0 and current.weekday() in rule['weekdays']
        elif rule['frequency']=='monthly':
            last = calendar.monthrange(current.year,current.month)[1]
            matched = months % rule['interval']==0 and current.day==min(rule['month_day'],last)
        else:
            last = calendar.monthrange(current.year,current.month)[1]
            is_nth = (current.day-1)//7+1 == rule['nth'] if rule['nth']>0 else current.day+7>last
            matched = months % rule['interval']==0 and current.weekday()==rule['weekday'] and is_nth
        if matched:
            ordinal += 1
            if rule['count'] and ordinal > rule['count']:
                break
            if current >= begin:
                result.append(current.isoformat())
                if len(result) >= limit:
                    break
        if current == date.max:
            break
        current += timedelta(days=1)
    return result


def save_series(db, tid, rule, opt_in=False):
    task = _task(db, tid)
    rule = normalize_rule(rule)
    row = db.execute('SELECT s.* FROM pro_series s JOIN pro_instances i ON i.series_id=s.id WHERE i.task_id=?',(tid,)).fetchone()
    if row:
        # Keep completed/historical instances immutable. Rule changes affect
        # ungenerated dates only; deleting history would invalidate review data.
        db.execute('UPDATE pro_series SET rule=?,paused=?,seed_id=?,updated=? WHERE id=?',
                   (_json(rule),int(rule['paused']),tid,_now(),row['id']))
        return row['id']
    if rule['basis']=='scheduled' and not opt_in:
        raise ValueError('定时产生重复实例需要明确确认')
    if get_meta(db,tid)['parent_id']:
        raise ValueError('高级重复任务不能作为子任务')
    when = _now()
    sid = db.execute('INSERT INTO pro_series(uid,seed_id,rule,paused,created,updated) VALUES(?,?,?,?,?,?)',
                     (uuid.uuid4().hex,tid,_json(rule),int(rule['paused']),when,when)).lastrowid
    occurrence = rule['start'] if rule['basis']=='scheduled' else 'initial'
    db.execute('INSERT INTO pro_instances(series_id,occurrence,task_id) VALUES(?,?,?)',(sid,occurrence,tid))
    options = f.options(db,tid); options['repeat']='off'; f.put_options(db,tid,options)
    return sid


def _clone_occurrence(db, series, source, occurrence, due):
    prior = db.execute('SELECT task_id FROM pro_instances WHERE series_id=? AND occurrence=?',(series['id'],occurrence)).fetchone()
    if prior:
        return prior[0]
    task = _task(db, source)
    meta = get_meta(db, source)
    project = db.execute('SELECT archived FROM pro_projects WHERE id=?',(meta['project_id'],)).fetchone() if meta['project_id'] else None
    if project and project[0]:
        return None
    if task['deleted_at']:
        return None
    now = s.now()
    rank = db.execute('SELECT COALESCE(MAX(sort_order),0)+1000 FROM tasks').fetchone()[0]
    tid = db.execute("INSERT INTO tasks(title,body,status,due_date,created_at,updated_at,sort_order) VALUES(?,?,'计划中',?,?,?,?)",
                     (task['title'],task['body'],due,now,now,rank)).lastrowid
    options = f.options(db,source)
    options.update(repeat='off',today='',snooze=0,acknowledged='')
    f.put_options(db,tid,options)
    db.execute('INSERT INTO task_tags SELECT ?,tag_id FROM task_tags WHERE task_id=?',(tid,source))
    db.execute('INSERT INTO checklist_items(task_id,label,sort_order) SELECT ?,label,sort_order FROM checklist_items WHERE task_id=?',(tid,source))
    db.execute('INSERT INTO milestones(task_id,name,sort_order) SELECT ?,name,sort_order FROM milestones WHERE task_id=?',(tid,source))
    rule = json.loads(series['rule'])
    if rule['dependency_mode']=='previous':
        db.execute('INSERT INTO task_dependencies VALUES(?,?)',(tid,source))
    else:
        db.execute('INSERT INTO task_dependencies SELECT ?,prerequisite FROM task_dependencies WHERE task_id=?',(tid,source))
    pro = {k: meta[k] for k in META_FIELDS}
    pro.update(parent_id=None,state_id=_state_for(db,meta['project_id'],'计划中'),cancelled=False,
               focus=False,plan_date='',time_blocks=[])
    saved(db,tid,dict(status='计划中',pro=pro))
    db.execute('INSERT INTO pro_instances(series_id,occurrence,task_id) VALUES(?,?,?)',(series['id'],occurrence,tid))
    _event(db,tid,'recurrence-created',dict(series_uid=series['uid'],occurrence=occurrence))
    s._event(db,tid,task['title'],'生成重复任务','高级规则；不复制附件')
    return tid


def advance_completion(db, tid, completed_at=None):
    row = db.execute('SELECT s.* FROM pro_series s JOIN pro_instances i ON i.series_id=s.id WHERE i.task_id=?',(tid,)).fetchone()
    if not row or row['paused']:
        return None
    rule = json.loads(row['rule'])
    if rule['basis']!='completion':
        return None
    now = completed_at or datetime.now(_zone(rule['timezone']))
    if now.tzinfo is None:
        raise ValueError('完成时间缺少时区')
    base = now.astimezone(_zone(rule['timezone'])).date()
    total = db.execute('SELECT COUNT(*) FROM pro_instances WHERE series_id=?',(row['id'],)).fetchone()[0]
    if rule['count'] and total>=rule['count']:
        return None
    # Completion-based recurrences restart their interval at actual completion;
    # the first date is strictly after completion, never today's original slot.
    next_rule = {**rule,'start':base.isoformat(),'count':None}
    if rule['frequency']=='monthly':
        next_rule['month_day']=base.day
    dates = rule_dates(next_rule,(base+timedelta(days=1)).isoformat(),1)
    if not dates:
        return None
    return _clone_occurrence(db,row,tid,'after:'+get_meta(db,tid)['uid'],dates[0])


def tick(db, at=None, limit=32):
    """Catch up while the app is running. Does not claim exited-app scheduling."""
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError('调度时间必须包含时区')
    emitted=[]
    with db:
        for row in db.execute('SELECT * FROM pro_series WHERE paused=0 AND seed_id IS NOT NULL').fetchall():
            rule=json.loads(row['rule'])
            if rule['basis']!='scheduled':
                continue
            today=at.astimezone(_zone(rule['timezone'])).date().isoformat()
            begin=today if rule['missed']=='skip' else rule['start']
            # Existing instances, including skipped/deleted ones, retain their
            # unique occurrence key. Restarting can never recreate them.
            dates=rule_dates(rule,begin,1000,until=today)
            source=row['seed_id']
            for due in dates:
                if len(emitted)>=limit:
                    return dict(created=emitted,remaining=True)
                if db.execute('SELECT 1 FROM pro_instances WHERE series_id=? AND occurrence=?',(row['id'],due)).fetchone():
                    continue
                if rule['dependency_mode']=='previous':
                    previous=db.execute('SELECT task_id FROM pro_instances WHERE series_id=? AND occurrence<? AND task_id IS NOT NULL ORDER BY occurrence DESC LIMIT 1',(row['id'],due)).fetchone()
                    source=previous[0] if previous else row['seed_id']
                child=_clone_occurrence(db,row,source,due,due)
                if child:
                    emitted.append(child)
    return dict(created=emitted,remaining=False)


def _validate_reminders(items):
    if not isinstance(items,list) or len(items)>20:
        raise ValueError('每项任务最多设置 20 个独立提醒')
    seen=set()
    for item in items:
        at=_instant(item.get('at')).isoformat()
        if at in seen:
            raise ValueError('相同时间的提醒不需要重复设置')
        seen.add(at)
        if item.get('status','pending') not in ('pending','ack','cancelled'):
            raise ValueError('提醒状态无效')


def save_reminders(db, tid, items):
    _task(db,tid);_validate_reminders(items)
    existing={r['id']:dict(r) for r in db.execute('SELECT * FROM pro_reminders WHERE task_id=?',(tid,))}
    keep=set()
    for item in items:
        rid=int(item.get('id') or 0)
        at=_instant(item['at']).isoformat()
        status=item.get('status','pending')
        if rid:
            if rid not in existing:
                raise ValueError('提醒不属于当前任务')
            old=existing[rid]
            if item.get('revision') is not None and int(item['revision'])!=old['revision']:
                raise ValueError('提醒已在其他操作中更新，请刷新')
            if at!=old['at']:
                db.execute("UPDATE pro_reminders SET at=?,status='pending',revision=revision+1,snooze_until=NULL,delivered_at=NULL WHERE id=?",(at,rid))
            elif status!=old['status']:
                db.execute('UPDATE pro_reminders SET status=?,revision=revision+1 WHERE id=?',(status,rid))
        else:
            rid=db.execute('INSERT INTO pro_reminders(uid,task_id,at,status) VALUES(?,?,?,?)',(uuid.uuid4().hex,tid,at,status)).lastrowid
        keep.add(rid)
    for rid in existing.keys()-keep:
        db.execute("UPDATE pro_reminders SET status='cancelled',revision=revision+1 WHERE id=?",(rid,))
    return [dict(r) for r in db.execute("SELECT * FROM pro_reminders WHERE task_id=? AND status<>'cancelled' ORDER BY at",(tid,))]


def reminders(db, at=None, limit=100, privacy=False):
    now=(at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    rows=db.execute('''SELECT r.*,t.title FROM pro_reminders r JOIN tasks t ON t.id=r.task_id
      LEFT JOIN pro_tasks p ON p.task_id=t.id LEFT JOIN pro_projects j ON j.id=p.project_id
      WHERE r.status='pending' AND r.at<=? AND (r.snooze_until IS NULL OR r.snooze_until<=?)
      AND t.status<>'完成' AND t.deleted_at IS NULL AND t.archived_at IS NULL
      AND COALESCE(p.cancelled,0)=0 AND COALESCE(j.archived,0)=0 ORDER BY r.at,r.id LIMIT ?''',(now,now,_int(limit,1,1000))).fetchall()
    result=[]
    for row in rows:
        data=dict(row);data['key']=f"pro:{row['id']}:{row['revision']}"
        if privacy:
            data['title']='你有待处理的任务提醒'
        data['overdue']=row['at']<now;result.append(data)
    return result


def reminder_action(db,item):
    rid=int(item['id']);row=db.execute('SELECT * FROM pro_reminders WHERE id=?',(rid,)).fetchone()
    if not row:
        raise ValueError('提醒不存在')
    if item.get('key') and item['key']!=f"pro:{rid}:{row['revision']}":
        raise ValueError('提醒已更新，请刷新后重试')
    if item.get('revision') is not None and int(item['revision'])!=row['revision']:
        raise ValueError('提醒已更新，请刷新后重试')
    action=item['action']
    if action=='snooze':
        minutes=_int(item.get('minutes',15),1,60*24*30,'稍后提醒分钟数')
        db.execute('UPDATE pro_reminders SET snooze_until=?,revision=revision+1,delivered_at=NULL WHERE id=?',((datetime.now(timezone.utc)+timedelta(minutes=minutes)).isoformat(),rid))
    elif action in ('ack','cancel'):
        db.execute('UPDATE pro_reminders SET status=?,revision=revision+1 WHERE id=?',('ack' if action=='ack' else 'cancelled',rid))
    elif action=='delivered':
        # Notification delivery and acknowledgement are deliberately different.
        db.execute('UPDATE pro_reminders SET delivered_at=? WHERE id=?',(_now(),rid))
    else:
        raise ValueError('提醒操作无效')


def _project_save(db,item):
    name=str(item.get('name','')).strip()
    if not name or len(name)>80:
        raise ValueError('项目名称需为 1–80 字')
    start,target=_day(item.get('start_date')),_day(item.get('target_date'))
    if start and target and target<start:
        raise ValueError('项目目标日期不能早于开始日期')
    values=(name,str(item.get('goal',''))[:2000],str(item.get('color','purple'))[:30],start,target,str(item.get('note',''))[:100_000])
    pid=int(item.get('id') or 0)
    if pid:
        if not db.execute('SELECT 1 FROM pro_projects WHERE id=?',(pid,)).fetchone():
            raise ValueError('项目不存在')
        db.execute('UPDATE pro_projects SET name=?,goal=?,color=?,start_date=?,target_date=?,note=? WHERE id=?',values+(pid,))
    else:
        pid=db.execute('INSERT INTO pro_projects(uid,name,goal,color,start_date,target_date,note) VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,)+values).lastrowid
        _default_states(db,pid)
    return dict(id=pid)


def _state_save(db,item):
    pid=int(item['project_id']);name=str(item.get('name','')).strip();kind=item.get('kind')
    if not name or len(name)>40 or kind not in KINDS:
        raise ValueError('列名称或状态类型无效')
    if not db.execute('SELECT 1 FROM pro_projects WHERE id=? AND archived=0',(pid,)).fetchone():
        raise ValueError('项目不存在或已归档')
    sid=int(item.get('id') or 0);rank=_int(item.get('sort_order',0),0,1_000_000)
    if sid:
        old=db.execute('SELECT * FROM pro_states WHERE id=? AND project_id=?',(sid,pid)).fetchone()
        if not old:
            raise ValueError('列不属于该项目')
        if kind!=old['kind']:
            raise ValueError('已有列的状态类型不能直接改变，请新建列并预览迁移任务')
        db.execute('UPDATE pro_states SET name=?,sort_order=? WHERE id=?',(name,rank,sid))
    else:
        sid=db.execute('INSERT INTO pro_states(project_id,name,kind,sort_order) VALUES(?,?,?,?)',(pid,name,kind,rank)).lastrowid
    return dict(id=sid)


def _move_project(db,item):
    tid=int(item['id']);project=int(item['project_id']);meta=get_meta(db,tid)
    if meta['parent_id'] and not item.get('detach'):
        raise ValueError('单独移动子任务需明确解除父任务关系')
    if not db.execute('SELECT 1 FROM pro_projects WHERE id=? AND archived=0',(project,)).fetchone():
        raise ValueError('目标项目不存在或已归档')
    ids=[tid]+[r[0] for r in db.execute('SELECT task_id FROM pro_tasks WHERE parent_id=?',(tid,))]
    mapping=[]
    for taskid in ids:
        task=_task(db,taskid);m=get_meta(db,taskid)
        mapping.append(dict(id=taskid,title=task['title'],state_id=_state_for(db,project,task['status'],m['cancelled'])))
    if not item.get('confirm'):
        return dict(preview=mapping,project_id=project,requires_confirmation=True)
    for entry in mapping:
        db.execute('UPDATE pro_tasks SET project_id=?,state_id=?,parent_id=CASE WHEN task_id=? THEN NULL ELSE parent_id END,inbox=0,revision=revision+1 WHERE task_id=?',(project,entry['state_id'],tid,entry['id']))
        db.execute('UPDATE tasks SET updated_at=? WHERE id=?',(s.now(),entry['id']))
        _event(db,entry['id'],'project-moved',dict(project_id=project))
    return dict(moved=ids)


def check_to_child(db,item):
    tid=int(item['id']);task=_task(db,tid);meta=get_meta(db,tid)
    if meta['parent_id'] or task['status']=='完成' or task['deleted_at'] or task['archived_at'] or meta['cancelled']:
        raise ValueError('请在未完成的顶层任务中转换检查项')
    checks=list(db.execute('SELECT * FROM checklist_items WHERE task_id=? ORDER BY sort_order,id',(tid,)))
    index=_int(item['index'],0,len(checks)-1,'检查项位置')
    check=checks[index];now=s.now();status='完成' if check['done'] else '计划中'
    child=db.execute('INSERT INTO tasks(title,body,status,due_date,created_at,updated_at,sort_order) VALUES(?,?,?,?,?,?,?)',(check['label'],'',status,task['due_date'],now,now,task['sort_order'])).lastrowid
    pro={k:meta[k] for k in META_FIELDS};pro.update(parent_id=tid,state_id=_state_for(db,meta['project_id'],status),inbox=False,focus=False,time_blocks=[])
    saved(db,child,dict(status=status,pro=pro));db.execute('DELETE FROM checklist_items WHERE id=?',(check['id'],))
    db.execute('UPDATE tasks SET updated_at=? WHERE id=?',(now,tid));_event(db,tid,'check-converted',dict(child_id=child))
    return dict(id=child)


def _child_to_check(db,item):
    tid=int(item['id']);task=_task(db,tid);meta=get_meta(db,tid)
    if not meta['parent_id']:
        raise ValueError('任务没有父任务')
    if not item.get('confirm_loss'):
        return dict(requires_confirmation=True,losses=['独立日期','提醒','说明','附件','依赖','优先级'])
    if db.execute('SELECT 1 FROM task_dependencies WHERE prerequisite=?',(tid,)).fetchone():
        raise ValueError('此子任务仍被其他任务依赖，不能转换')
    rank=db.execute('SELECT COALESCE(MAX(sort_order),0)+1 FROM checklist_items WHERE task_id=?',(meta['parent_id'],)).fetchone()[0]
    db.execute('INSERT INTO checklist_items(task_id,label,done,sort_order) VALUES(?,?,?,?)',(meta['parent_id'],task['title'],int(task['status']=='完成'),rank))
    # Preserve the old object in trash instead of erasing metadata/attachments.
    db.execute('UPDATE tasks SET deleted_at=?,updated_at=? WHERE id=?',(s.now(),s.now(),tid))
    db.execute('UPDATE pro_tasks SET parent_id=NULL WHERE task_id=?',(tid,))
    return dict(id=meta['parent_id'],archived_child=tid)


def export_content(db, item):
    state=snapshot(db)
    wanted=set(map(int,item.get('ids',[]))) if item.get('ids') else None
    tasks=[t for group in ('tasks','archived','trash') for t in state[group] if wanted is None or t['id'] in wanted]
    uidmap={t['id']:t['pro']['uid'] for group in ('tasks','archived','trash') for t in state[group]}
    selected={t['id'] for t in tasks}
    # Export relationship closure explicitly; silently dropping a prerequisite
    # or parent would make a "complete" export misleading.
    if wanted:
        missing=set()
        for task in tasks:
            refs=[d['id'] for d in task.get('dependencies',[])]+([task['pro']['parent_id']] if task['pro']['parent_id'] else [])
            missing.update(x for x in refs if x not in selected)
        if missing:
            raise ValueError('所选任务引用未导出的父任务或依赖，请一并选择：'+','.join(map(str,sorted(missing))))
    projects={p['id']:p for p in state['projects']}
    states={p['id']:p for p in state['project_states']}
    out=[];total=0
    for task in tasks:
        p=task['pro'];data={k:task.get(k) for k in ('title','status','due_date','created_at','updated_at','sort_order','deleted_at','archived_at')}
        data.update(uid=p['uid'],revision=p['revision'],note=task.get('note',''),options=task['options'],
                    tags=[dict(name=t['name'],color=t.get('color','purple')) for t in task.get('tags',[])],
                    checklist=task.get('checklist',[]),milestones=task.get('milestones',[]),
                    dependencies=[uidmap[d['id']] for d in task.get('dependencies',[])],
                    pro={k:p[k] for k in META_FIELDS if k not in ('project_id','parent_id','state_id')},files=[])
        data['pro'].update(project_uid=projects[p['project_id']]['uid'] if p['project_id'] else None,
                           parent_uid=uidmap.get(p['parent_id']),
                           state_key=str(p['state_id']) if p['state_id'] else None,
                           related_uids=[uidmap[x] for x in p.get('related_ids',[]) if x in selected],
                           completed_at=p.get('completed_at'),reminders=[{k:r[k] for k in ('uid','at','status','revision','snooze_until','delivered_at')} for r in p.get('reminders',[])])
        for file in task.get('files',[]):
            path=s.FILES/file['stored_name']
            if not path.is_file():
                raise ValueError('附件缺失，不能声称导出完整：'+file['filename'])
            raw=path.read_bytes();total+=len(raw)
            if total>MAX_IMPORT_BYTES:
                raise ValueError('内容包附件总计超过 100 MB，请使用完整备份或按项目分批导出')
            data['files'].append(dict(name=file['filename'],data=base64.b64encode(raw).decode('ascii'),sha256=hashlib.sha256(raw).hexdigest(),version_label=file.get('version_label','初稿'),version_note=file.get('version_note','')))
        out.append(data)
    series=[]
    for row in db.execute('SELECT * FROM pro_series'):
        instances=[dict(occurrence=i['occurrence'],task_uid=uidmap.get(i['task_id']),skipped=bool(i['skipped'])) for i in db.execute('SELECT * FROM pro_instances WHERE series_id=?',(row['id'],)) if i['task_id'] in selected]
        if instances:
            series.append(dict(uid=row['uid'],seed_uid=uidmap.get(row['seed_id']),rule=json.loads(row['rule']),paused=bool(row['paused']),instances=instances))
    package=dict(format='TaskBoard.Content',version=1,created=_now(),projects=[dict(p) for p in projects.values()],states=[{**dict(st),'key':str(st['id']),'project_uid':projects[st['project_id']]['uid']} for st in states.values()],tasks=out,series=series,
                 privacy='内容导出不包含密码验证信息、密钥、本机凭据或诊断日志')
    path=item.get('path')
    if path:
        target=Path(path)
        if target.resolve()==s.DB.resolve() if hasattr(s,'DB') else False:
            raise ValueError('不能覆盖工作区数据库')
        if target.resolve().is_relative_to(s.FILES.resolve()):
            raise ValueError('请导出到附件数据目录以外的位置')
        temp=target.with_name(target.name+'.pending-'+uuid.uuid4().hex)
        try:
            temp.write_text(json.dumps(package,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        return dict(path=str(target),tasks=len(out),attachments=sum(len(t['files']) for t in out),bytes=total)
    return dict(package=package,tasks=len(out),attachments=sum(len(t['files']) for t in out))


def _read_package(item):
    if item.get('path'):
        path=Path(item['path'])
        if path.stat().st_size>MAX_IMPORT_BYTES*2:
            raise ValueError('内容包太大，请分批导入')
        try:
            raw=path.read_bytes()
            digest=hashlib.sha256(raw).hexdigest()
            if item.get('expected_sha256') and item['expected_sha256']!=digest:
                raise ValueError('内容包在预览后已变化，请重新预览')
            data=json.loads(raw.decode('utf-8-sig'))
            data['_source_sha256']=digest
        except (UnicodeError,json.JSONDecodeError):
            raise ValueError('内容包不是有效的 UTF-8 JSON') from None
    else:
        data=item.get('package')
    if not isinstance(data,dict) or data.get('format')!='TaskBoard.Content' or data.get('version')!=1:
        raise ValueError('不支持的内容包格式或版本')
    if not isinstance(data.get('tasks'),list) or len(data['tasks'])>100000:
        raise ValueError('内容包任务列表无效')
    return data


def import_content(db,item,preview=False):
    package=_read_package(item);mode=item.get('mode','skip')
    if mode not in ('skip','copy','replace'):
        raise ValueError('导入策略应为 skip、copy 或 replace')
    existing={r['uid']:dict(r) for r in db.execute('SELECT task_id,uid,revision FROM pro_tasks')}
    incoming=package['tasks'];uids=set();file_bytes=0;rawfiles={}
    for task in incoming:
        uid=str(task.get('uid',''))
        if not uid or len(uid)>80 or uid in uids:
            raise ValueError('内容包存在缺失或重复的任务稳定 ID')
        uids.add(uid)
        title=str(task.get('title','')).strip()
        if not title or len(title)>240 or task.get('status') not in STATUS_KINDS:
            raise ValueError('导入任务标题或状态无效')
        _day(task.get('due_date'));_day(task.get('pro',{}).get('plan_date'))
        for index,file in enumerate(task.get('files',[])):
            try:
                raw=base64.b64decode(file['data'],validate=True)
            except (ValueError,KeyError):
                raise ValueError('附件编码无效') from None
            file_bytes+=len(raw)
            if not raw or file_bytes>MAX_IMPORT_BYTES or hashlib.sha256(raw).hexdigest()!=file.get('sha256'):
                raise ValueError('附件为空、超过容量限制或内容校验失败')
            rawfiles[(uid,index)]=raw
    available=uids | set(existing)
    for task in incoming:
        p=task.get('pro',{})
        refs=list(task.get('dependencies',[]))+list(p.get('related_uids',[]))+([p['parent_uid']] if p.get('parent_uid') else [])
        if any(x not in available for x in refs):
            raise ValueError('内容包含无法映射的任务关系：'+task['title'])
    info=dict(source_sha256=package.get('_source_sha256'),tasks=len(incoming),attachments=len(rawfiles),bytes=file_bytes,existing=sum(t['uid'] in existing for t in incoming),mode=mode,
              requires_confirmation=True,new=sum(t['uid'] not in existing for t in incoming))
    if preview or not item.get('confirm'):
        return info
    staged=[]
    try:
        # Staged opaque names never use a supplied relative path.
        for key,raw in rawfiles.items():
            ext=Path(next(t for t in incoming if t['uid']==key[0])['files'][key[1]].get('name','')).suffix.lower()
            if len(ext)>12 or not ext[1:].isalnum():ext='.bin'
            path=s.FILES/(uuid.uuid4().hex+ext);path.write_bytes(raw);staged.append(path)
            rawfiles[key]=path
        with db:
            projectmap={r['uid']:r['id'] for r in db.execute('SELECT uid,id FROM pro_projects')}
            for p in package.get('projects',[]):
                if p['uid'] not in projectmap:
                    pid=_project_save(db,p)['id'];db.execute('UPDATE pro_projects SET uid=?,archived=? WHERE id=?',(p['uid'],int(bool(p.get('archived'))),pid));projectmap[p['uid']]=pid
            statemap={}
            for state in package.get('states',[]):
                pid=projectmap.get(state['project_uid'])
                if not pid or state.get('kind') not in KINDS:raise ValueError('导入状态或项目引用无效')
                same=db.execute('SELECT id FROM pro_states WHERE project_id=? AND name=? AND kind=?',(pid,state['name'],state['kind'])).fetchone()
                sid=same[0] if same else db.execute('INSERT INTO pro_states(project_id,name,kind,sort_order) VALUES(?,?,?,?)',(pid,state['name'],state['kind'],state.get('sort_order',0))).lastrowid
                statemap[str(state['key'])]=sid
            mapping={uid:row['task_id'] for uid,row in existing.items()};written=[]
            for task in incoming:
                uid=task['uid'];old=existing.get(uid)
                if old and mode=='skip':continue
                if old and mode=='replace':
                    if int(task.get('revision',0))<old['revision'] and not item.get('allow_older'):
                        raise ValueError('导入版本早于本地：'+task['title']+'；请使用副本或明确允许旧版')
                    tid=old['task_id'];db.execute('UPDATE tasks SET title=?,body=?,status=?,due_date=?,updated_at=?,deleted_at=?,archived_at=? WHERE id=?',(task['title'],task.get('note',''),task['status'],task.get('due_date'),s.now(),task.get('deleted_at'),task.get('archived_at'),tid))
                else:
                    now=s.now();tid=db.execute('INSERT INTO tasks(title,body,status,due_date,created_at,updated_at,sort_order,deleted_at,archived_at) VALUES(?,?,?,?,?,?,?,?,?)',(task['title'],task.get('note',''),task['status'],task.get('due_date'),task.get('created_at') or now,now,task.get('sort_order',0),task.get('deleted_at'),task.get('archived_at'))).lastrowid
                mapping[uid]=tid;written.append((task,tid,uuid.uuid4().hex if old and mode=='copy' else uid))
            for task,tid,uid in written:
                p=task.get('pro',{});project=projectmap.get(p.get('project_uid'))
                parent=mapping.get(p.get('parent_uid'))
                state=statemap.get(str(p.get('state_key')))
                if p.get('project_uid') and project is None:raise ValueError('项目引用无法映射')
                if state and db.execute('SELECT project_id FROM pro_states WHERE id=?',(state,)).fetchone()[0]!=project:raise ValueError('状态不属于项目')
                values={**get_meta(db,tid),**{k:p[k] for k in META_FIELDS if k in p},'project_id':project,'parent_id':parent,'state_id':state,'related_ids':[mapping[x] for x in p.get('related_uids',[])]}
                # Do not run user-save completion automation during a content import.
                db.execute('''INSERT INTO pro_tasks(task_id,uid,revision,project_id,parent_id,state_id,cancelled,inbox,plan_date,focus,estimate_minutes,time_blocks,last_status,completed_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,project_id=excluded.project_id,parent_id=excluded.parent_id,state_id=excluded.state_id,cancelled=excluded.cancelled,inbox=excluded.inbox,plan_date=excluded.plan_date,focus=excluded.focus,estimate_minutes=excluded.estimate_minutes,time_blocks=excluded.time_blocks,last_status=excluded.last_status,completed_at=excluded.completed_at''',
                  (tid,uid,max(1,int(task.get('revision',1))),project,parent,state,int(bool(values['cancelled'])),int(bool(values['inbox'])),values['plan_date'],int(bool(values['focus'])),_int(values['estimate_minutes'],0,525600),_json(values['time_blocks']),task['status'],p.get('completed_at')))
                opts={**f.DEFAULT,**task.get('options',{})};f.put_options(db,tid,opts)
                for table in ('checklist_items','milestones','task_tags','task_dependencies','pro_reminders'):
                    db.execute(f'DELETE FROM {table} WHERE task_id=?',(tid,))
                db.execute('DELETE FROM pro_links WHERE a=? OR b=?',(tid,tid))
                for tag in task.get('tags',[]):
                    name=str(tag['name']).strip()
                    if not name or len(name)>80:raise ValueError('导入标签无效')
                    db.execute('INSERT OR IGNORE INTO tags(name) VALUES(?)',(name,));tagid=db.execute('SELECT id FROM tags WHERE name=?',(name,)).fetchone()[0]
                    db.execute('INSERT OR IGNORE INTO task_tags VALUES(?,?)',(tid,tagid));db.execute('INSERT OR IGNORE INTO tag_colors VALUES(?,?)',(tagid,tag.get('color','purple')))
                for rank,check in enumerate(task.get('checklist',[])):
                    db.execute('INSERT INTO checklist_items(task_id,label,done,sort_order) VALUES(?,?,?,?)',(tid,check['label'],int(bool(check.get('done'))),rank))
                for rank,milestone in enumerate(task.get('milestones',[])):
                    db.execute('INSERT INTO milestones(task_id,name,confirmed,confirmed_at,sort_order) VALUES(?,?,?,?,?)',(tid,milestone['name'],int(bool(milestone.get('confirmed'))),milestone.get('confirmed_at'),rank))
                for dep in task.get('dependencies',[]):
                    if mapping[dep]==tid:raise ValueError('任务不能依赖自身')
                    db.execute('INSERT INTO task_dependencies VALUES(?,?)',(tid,mapping[dep]))
                for related in values['related_ids']:
                    if related==tid:raise ValueError('任务不能关联自身')
                    db.execute('INSERT OR IGNORE INTO pro_links VALUES(?,?)',tuple(sorted((tid,related))))
                for reminder in p.get('reminders',[]):
                    at=_instant(reminder['at']).isoformat();status=reminder.get('status','pending')
                    if status not in ('pending','ack','cancelled'):raise ValueError('导入提醒状态无效')
                    db.execute('INSERT INTO pro_reminders(uid,task_id,at,status,revision,snooze_until,delivered_at) VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,tid,at,status,1,reminder.get('snooze_until'),reminder.get('delivered_at')))
                # Existing attachment content remains on disk for history/recovery.
                db.execute('DELETE FROM attachments WHERE task_id=?',(tid,))
                for index,file in enumerate(task.get('files',[])):
                    path=rawfiles[(task['uid'],index)];name=Path(str(file.get('name','file')).replace('\\','/')).name[:240]
                    db.execute('INSERT INTO attachments(task_id,filename,stored_name,is_image,created_at,version_label,version_note) VALUES(?,?,?,?,?,?,?)',(tid,name,path.name,int(path.suffix.lower() in ('.png','.jpg','.jpeg','.gif','.bmp','.webp')),s.now(),str(file.get('version_label','初稿'))[:20],str(file.get('version_note',''))[:200]))
            for task,tid,_ in written:
                p=get_meta(db,tid)
                if p['parent_id']:
                    parent=get_meta(db,p['parent_id'])
                    if parent['parent_id'] or parent['project_id']!=p['project_id']:raise ValueError('导入父子关系超过两级或跨项目')
                _validate_graph(db,tid,p['parent_id'],[r[0] for r in db.execute('SELECT prerequisite FROM task_dependencies WHERE task_id=?',(tid,))])
            for ser in package.get('series',[]):
                source=mapping.get(ser.get('seed_uid'))
                if source is None:continue
                if mode=='skip' and all(i.get('task_uid') in existing for i in ser.get('instances',[])):continue
                rule=normalize_rule(ser['rule']);old=db.execute('SELECT id FROM pro_series WHERE uid=?',(ser['uid'],)).fetchone()
                if old and mode=='replace':
                    sid=old[0];db.execute('UPDATE pro_series SET seed_id=?,rule=?,paused=?,updated=? WHERE id=?',(source,_json(rule),int(ser.get('paused',False)),_now(),sid));db.execute('DELETE FROM pro_instances WHERE series_id=?',(sid,))
                else:
                    sid=db.execute('INSERT INTO pro_series(uid,seed_id,rule,paused,created,updated) VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex if old else ser['uid'],source,_json(rule),int(ser.get('paused',False)),_now(),_now())).lastrowid
                for inst in ser.get('instances',[]):
                    target=mapping.get(inst.get('task_uid'))
                    if target is not None:
                        db.execute('DELETE FROM pro_instances WHERE task_id=?',(target,))
                        db.execute('INSERT INTO pro_instances VALUES(?,?,?,?)',(sid,inst['occurrence'],target,int(inst.get('skipped',False))))
            for _,tid,_ in written:_event(db,tid,'imported',dict(mode=mode))
        referenced={r[0] for r in db.execute('SELECT stored_name FROM attachments')}
        for path in staged:
            if path.name not in referenced:path.unlink(missing_ok=True)
        return dict(imported=len(written),skipped=len(incoming)-len(written),mapping={uid:tid for uid,tid in mapping.items() if uid in uids})
    except Exception:
        for path in staged:path.unlink(missing_ok=True)
        raise


def handle(db,method,item):
    if method=='pro-query':return query(db,item)
    if method=='pro-review':return review(db,item)
    if method=='pro-impact':return impact(db,int(item['id']))
    if method=='pro-series-preview':return dict(dates=rule_dates(item['rule'],item.get('from'),item.get('limit',5)))
    if method=='pro-reminders':return dict(reminders=reminders(db,privacy=bool(item.get('privacy'))))
    if method=='pro-export':return export_content(db,item)
    if method in ('pro-import','pro-import-preview'):return import_content(db,item,preview=method.endswith('-preview'))
    with db:
        if method=='pro-project-save':return _project_save(db,item)
        if method=='pro-project-move':return _move_project(db,item)
        if method=='pro-project-archive':
            pid=int(item['id']);archived=bool(item.get('archived',True))
            if not db.execute('SELECT 1 FROM pro_projects WHERE id=?',(pid,)).fetchone():raise ValueError('项目不存在')
            db.execute('UPDATE pro_projects SET archived=? WHERE id=?',(int(archived),pid));return dict(id=pid)
        if method=='pro-project-delete':
            pid=int(item['id'])
            if db.execute('SELECT 1 FROM pro_tasks WHERE project_id=?',(pid,)).fetchone():raise ValueError('项目仍有任务，请归档或迁移后再删除')
            if pid==_default_project(db):raise ValueError('默认迁移项目不能删除，请使用归档')
            db.execute('DELETE FROM pro_states WHERE project_id=?',(pid,));db.execute('DELETE FROM pro_projects WHERE id=?',(pid,));return dict(id=pid)
        if method=='pro-state-save':return _state_save(db,item)
        if method=='pro-state-delete':
            sid,target=int(item['id']),int(item['target_id'])
            old=db.execute('SELECT * FROM pro_states WHERE id=?',(sid,)).fetchone();new=db.execute('SELECT * FROM pro_states WHERE id=?',(target,)).fetchone()
            if not old or not new or sid==target or old['project_id']!=new['project_id'] or old['kind']!=new['kind']:raise ValueError('删列迁移目标必须是同项目同类型的其他列')
            db.execute('UPDATE pro_tasks SET state_id=?,revision=revision+1 WHERE state_id=?',(target,sid));db.execute('DELETE FROM pro_states WHERE id=?',(sid,));return dict(id=target)
        if method=='pro-view-save':
            name=str(item.get('name','')).strip();filters=item.get('filters',{})
            if not name or len(name)>80 or not isinstance(filters,dict) or len(_json(filters))>12000:raise ValueError('视图名称或筛选条件无效')
            vid=int(item.get('id') or 0);rank=int(item.get('sort_order',0))
            if vid:
                if not db.execute('SELECT 1 FROM pro_views WHERE id=?',(vid,)).fetchone():raise ValueError('视图不存在')
                db.execute('UPDATE pro_views SET name=?,data=?,sort_order=? WHERE id=?',(name,_json(filters),rank,vid))
            else:vid=db.execute('INSERT INTO pro_views(name,data,sort_order) VALUES(?,?,?)',(name,_json(filters),rank)).lastrowid
            return dict(id=vid)
        if method=='pro-view-delete':db.execute('DELETE FROM pro_views WHERE id=?',(int(item['id']),));return {}
        if method=='pro-view-reorder':
            ids=list(map(int,item['ids']))
            if len(ids)!=len(set(ids)):raise ValueError('视图顺序存在重复项')
            actual={r[0] for r in db.execute('SELECT id FROM pro_views')}
            if set(ids)!=actual:raise ValueError('视图已变化，请刷新后重新排序')
            for n,vid in enumerate(ids):db.execute('UPDATE pro_views SET sort_order=? WHERE id=?',(n,vid))
            return {}
        if method=='pro-series-save':return dict(id=save_series(db,int(item['id']),item['rule'],bool(item.get('opt_in'))))
        if method=='pro-series-action':
            tid=int(item['id']);row=db.execute('SELECT s.* FROM pro_series s JOIN pro_instances i ON i.series_id=s.id WHERE i.task_id=?',(tid,)).fetchone()
            if not row:raise ValueError('此任务不属于高级重复序列')
            action=item['action']
            if action in ('pause','resume'):
                db.execute('UPDATE pro_series SET paused=?,updated=? WHERE id=?',(int(action=='pause'),_now(),row['id']))
            elif action=='skip':
                db.execute('UPDATE pro_instances SET skipped=1 WHERE task_id=?',(tid,))
                meta=get_meta(db,tid);db.execute('UPDATE pro_tasks SET cancelled=1,state_id=?,revision=revision+1 WHERE task_id=?',(_state_for(db,meta['project_id'],'计划中',True),tid))
                _event(db,tid,'cancelled',dict(reason='skip-instance'));advance_completion(db,tid)
            else:raise ValueError('序列操作无效')
            return dict(id=row['id'])
        if method=='pro-reminder-save':return dict(reminders=save_reminders(db,int(item['id']),item['reminders']))
        if method=='pro-reminder-action':reminder_action(db,item);return {}
        if method=='pro-check-to-child':return check_to_child(db,item)
        if method=='pro-child-to-check':return _child_to_check(db,item)
        if method=='pro-tick':return tick(db)
    raise ValueError('未知专业功能命令：'+method)
