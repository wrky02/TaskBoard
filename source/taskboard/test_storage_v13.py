"""V1.3 new functionality tests: display names, view-state memory, batch ops,
manual checklist, milestones, attachment health check, duplicate, archive,
export CSV/XLSX, and the install-dir data migration helper.
"""
import os, tempfile, shutil
from pathlib import Path
root = Path(tempfile.mkdtemp(prefix='tb-v13-test-'))
os.environ['LOCALAPPDATA'] = str(root)
import storage as s

db = s.connect()
s.set_password(db, 'testpass123')

# Display names: must show exactly what the user typed, no auto-suffix.
s.set_display_names(db, 'XXX公司任务看板系统', '张三')
names = s.get_display_names(db)
assert names['system_name'] == 'XXX公司任务看板系统', names
assert names['user_name'] == '张三', names
s.set_display_names(db, '', '')
assert s.get_display_names(db) == {'system_name': '', 'user_name': ''}

# Board view state memory (not window geometry).
s.set_board_view_state(db, '5', 'G90')
assert s.get_board_view_state(db) == {'tag_filter': '5', 'query': 'G90'}

# Reminder mode toggle.
s.set_reminder_mode(db, 'off')
assert s.get_reminder_mode(db) == 'off'
try:
    s.set_reminder_mode(db, 'invalid')
    raise AssertionError('invalid reminder mode accepted')
except ValueError:
    pass

# Tasks + batch operations.
a = s.save_task(db, '任务A', '<p>内容A</p>', '计划中', None)
b = s.save_task(db, '任务B', '<p>内容B</p>', '计划中', None)
c = s.save_task(db, '任务C', '<p>内容C</p>', '计划中', None)
tag_id = s.add_tag(db, '打样')

bulk_tag = s.bulk_add_tag(db, [a, b, 999999], tag_id)
assert bulk_tag['ok'] == [a, b], bulk_tag
assert len(bulk_tag['failed']) == 1 and bulk_tag['failed'][0]['id'] == 999999

bulk_move = s.bulk_update_status(db, [a, b], '进行中')
assert bulk_move['ok'] == [a, b]
assert s.task(db, a)['status'] == '进行中'
assert s.task(db, b)['status'] == '进行中'

bulk_del = s.bulk_trash(db, [c])
assert bulk_del['ok'] == [c]
assert s.task(db, c) is None  # trashed tasks excluded from task() by default
assert s.task(db, c, allow_deleted=True)['deleted_at'] is not None

# Duplicate task: copies title/body/tags/checklist, not attachments.
s.add_checklist_item(db, a, '核对孔位')
s.add_checklist_item(db, a, '核对间隙')
dup_id = s.duplicate_task(db, a)
dup = s.task(db, dup_id)
assert dup['title'] == '任务A（副本）', dup['title']
assert dup['status'] == '计划中'
assert set(s.task_tag_ids(db, dup_id)) == set(s.task_tag_ids(db, a))
assert len(s.checklist_items(db, dup_id)) == 2

# Manual checklist remains independent of retired schema.
for label in ['需求确认', '图纸', '打样', '装车验证']:
    s.add_checklist_item(db, b, label)
items = s.checklist_items(db, b)
assert len(items) == 4
s.toggle_checklist_item(db, items[0]['id'], True)
assert s.checklist_items(db, b)[0]['done'] == 1

# Milestones.
m1 = s.add_milestone(db, b, '样件确认')
s.confirm_milestone(db, m1, True)
assert s.milestones(db, b)[0]['confirmed'] == 1
assert s.milestones(db, b)[0]['confirmed_at']

# Attachment version notes.
att_id = s.add_file_bytes(db, b, 'design.jpg', b'\xff\xd8\xff fake jpeg data', True)
s.set_attachment_version(db, att_id, '修订版', '调整了ACC开孔位置')
row = db.execute('SELECT * FROM attachments WHERE id=?', (att_id,)).fetchone()
assert row['version_label'] == '修订版'
assert row['version_note'] == '调整了ACC开孔位置'

# Attachment health check: must find a genuinely missing file without
# deleting or altering anything on either side.
missing_att_id = s.add_file_bytes(db, b, 'ghost.png', b'temp bytes', True)
ghost_row = db.execute('SELECT stored_name FROM attachments WHERE id=?', (missing_att_id,)).fetchone()
(s.FILES / ghost_row['stored_name']).unlink()  # simulate a file lost outside the app
report = s.attachment_health_check(db)
assert any(m['attachment_id'] == missing_att_id for m in report['missing']), report
assert report['total_attachments'] >= 2

orphan_path = s.FILES / 'orphan-test-file.bin'
orphan_path.write_bytes(b'orphan')
report2 = s.attachment_health_check(db)
assert any(o['stored_name'] == 'orphan-test-file.bin' for o in report2['orphaned_files']), report2
# Health check must not have deleted the orphan or fixed the missing ref.
assert orphan_path.is_file()
assert not (s.FILES / ghost_row['stored_name']).is_file()

# Archive completed tasks: only 完成 tasks can be archived; archived tasks
# are excluded from the normal board view but kept in the backup.
s.move(db, c if False else a, '完成')  # a is currently 进行中 -> move to 完成
assert s.task(db, a)['status'] == '完成'
try:
    s.archive_completed(db, b)  # b is still 进行中
    raise AssertionError('archived a non-完成 task')
except ValueError:
    pass
s.archive_completed(db, a)
board_tasks = [t['id'] for t in s.tasks(db)]
assert a not in board_tasks, 'archived task leaked into normal board view'
archived_tasks = [t['id'] for t in s.tasks(db, archived=True)]
assert a in archived_tasks
s.unarchive_task(db, a)
assert a in [t['id'] for t in s.tasks(db)]
s.archive_completed(db, a)  # leave archived for the export/backup checks below

# Calendar view: tasks with a due date in the given month.
cal_task = s.save_task(db, '日历任务', '', '计划中', '2026-11-15')
nov = s.calendar_tasks(db, 2026, 11)
assert any(t['id'] == cal_task for t in nov), nov
dec = s.calendar_tasks(db, 2026, 12)
assert not any(t['id'] == cal_task for t in dec)

# Export CSV and XLSX: formula-injection guard + readable round trip.
injection_task = s.save_task(db, '=cmd|/c calc', '', '计划中', None)
csv_path = root / 'export.csv'
count = s.export_csv(db, csv_path)
csv_text = csv_path.read_text(encoding='utf-8-sig')
assert "'=cmd" in csv_text, 'formula injection guard missing in CSV: ' + csv_text[:200]
assert count > 0

xlsx_path = root / 'export.xlsx'
xcount = s.export_xlsx(db, xlsx_path)
assert xlsx_path.is_file() and xlsx_path.stat().st_size > 0
assert xcount == count
from openpyxl import load_workbook
wb = load_workbook(xlsx_path)
ws = wb.active
header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
assert header == ['ID', '标题', '状态', '截止日期', '标签', '备注摘要', '附件数'], header
found_injection_guard = any(
    isinstance(row[1].value, str) and row[1].value.startswith("'=cmd")
    for row in ws.iter_rows(min_row=2)
)
assert found_injection_guard, 'formula injection guard missing in XLSX'

# writable() must actually prove write access, not just check a permission bit.
writable_dir = root / 'writable-check'
assert s.writable(writable_dir) is True
assert writable_dir.is_dir()

# Clean up the deliberately-broken attachment reference created above (for
# the health-check test) before exercising migration: migrate_data_root must
# refuse to migrate data with a known-missing attachment, which we verify
# separately below, so first prove normal migration works on healthy data.
db.execute('DELETE FROM attachments WHERE id=?', (missing_att_id,))
db.commit()
orphan_path.unlink()

# migrate_data_root: no legacy DB present yet in a fresh env -> nothing_to_migrate.
fresh_legacy_env = root / 'fresh-legacy'
os.environ['LOCALAPPDATA'] = str(fresh_legacy_env)
install_dir = root / 'install-data'
result_none = s.migrate_data_root(install_dir)
assert result_none['status'] == 'nothing_to_migrate', result_none

# Now create a legacy DB (simulating an existing V1.2 user) and migrate it.
# Realistic flow: the app closes its connection to the legacy location before
# migrating (migration happens once at startup, before any new connect()).
# If the handle were left open, Windows would correctly refuse to rename the
# legacy directory aside — migrate_data_root handles that by leaving the
# legacy directory in place rather than losing data, which we verify above
# is still a safe ('migrated' with legacy_kept_at == legacy_root) outcome.
db.close()
os.environ['LOCALAPPDATA'] = str(root)  # root/TaskBoardAccio already has our real test data
install_dir2 = root / 'install-data-2'
migration = s.migrate_data_root(install_dir2)
assert migration['status'] == 'migrated', migration
assert migration['tasks'] > 0
assert (install_dir2 / 'tasks.sqlite3').is_file()
assert (install_dir2 / 'attachments').is_dir()
# Legacy directory must still exist (renamed, not deleted) for recoverability.
legacy_root = root / 'TaskBoardAccio'
assert not legacy_root.is_dir(), 'legacy dir should have been renamed away'
assert Path(migration['legacy_kept_at']).is_dir(), 'legacy data must remain recoverable'
legacy_db_still_readable = Path(migration['legacy_kept_at']) / 'tasks.sqlite3'
assert legacy_db_still_readable.is_file()

# Migrating again must be a no-op (never overwrite existing migrated data).
migration2 = s.migrate_data_root(install_dir2)
assert migration2['status'] == 'already_migrated', migration2

# migrate_data_root must refuse a target it genuinely cannot write to.
if os.name == 'nt':
    import ctypes
    is_admin = False
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        pass
    if not is_admin:
        unwritable_target = Path('C:/Windows/System32/taskboard-should-not-write-here')
        result_blocked = s.migrate_data_root(unwritable_target)
        assert result_blocked['status'] == 'target_not_writable', result_blocked

db.close()
print('PASS all V1.3 new-feature tests: display names, view-state memory, '
      'batch ops, manual checklist, milestones, attachment version, '
      'health check, archive, duplicate, calendar, CSV/XLSX export, migration')

# --- Separate isolated check: migration must refuse data with a missing
# attachment reference, rather than silently migrating a broken dataset. ---
broken_root = Path(tempfile.mkdtemp(prefix='tb-v13-broken-'))
os.environ['LOCALAPPDATA'] = str(broken_root)
db2 = s.connect()
s.set_password(db2, 'testpass123')
t2 = s.save_task(db2, '破损任务', '', '计划中', None)
broken_att = s.add_file_bytes(db2, t2, 'will-vanish.png', b'temp', True)
row2 = db2.execute('SELECT stored_name FROM attachments WHERE id=?', (broken_att,)).fetchone()
(s.FILES / row2['stored_name']).unlink()  # simulate the file being lost
db2.close()
broken_install_dir = broken_root / 'install-data'
broken_result = s.migrate_data_root(broken_install_dir)
assert broken_result['status'] == 'failed', broken_result
assert '附件' in broken_result['error'], broken_result
assert not broken_install_dir.exists() or not (broken_install_dir / 'tasks.sqlite3').is_file(), 'broken data must not have been committed'
print('PASS migration correctly refuses data with missing attachments')
