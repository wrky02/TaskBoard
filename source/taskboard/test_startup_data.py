"""Startup must never replace failed legacy migration with an empty database."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import storage as store


class StartupDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)})
        self.env.start()
        os.environ.pop('TASKBOARD_DATA_ROOT', None)
        self.install = self.root / 'install' / 'Data'

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_explicit_override_is_not_overwritten(self):
        isolated = self.root / 'isolated'
        os.environ['TASKBOARD_DATA_ROOT'] = str(isolated)
        with patch.object(store, 'migrate_data_root') as migrate:
            self.assertEqual(store.prepare_data_root(self.install)['path'], str(isolated))
            migrate.assert_not_called()
        self.assertFalse(self.install.exists())

    def test_failed_migration_preserves_legacy_and_does_not_select_empty_root(self):
        db = store.connect()
        task = store.save_task(db, '旧任务', '', '计划中', None)
        store.add_file_bytes(db, task, 'missing.png', b'image', True)
        file = next(store.FILES.iterdir())
        file.unlink()
        legacy_db = store.DB
        db.close()
        before = legacy_db.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '数据迁移未完成'):
            store.prepare_data_root(self.install)
        self.assertNotIn('TASKBOARD_DATA_ROOT', os.environ)
        self.assertFalse((self.install / 'tasks.sqlite3').exists())
        self.assertEqual(legacy_db.read_bytes(), before)
        db = store.connect()
        self.assertEqual(store.task(db, task)['title'], '旧任务')
        db.close()

    def test_healthy_migration_retains_tasks_and_is_repeatable(self):
        db = store.connect()
        task = store.save_task(db, '保留任务', '', '计划中', None)
        store.add_checklist_item(db, task, '手动检查项')
        db.close()
        first = store.prepare_data_root(self.install)
        self.assertEqual(first['status'], 'migrated')
        self.assertTrue(Path(first['legacy_kept_at']).is_dir())
        db = store.connect()
        self.assertEqual(store.task(db, task)['title'], '保留任务')
        self.assertEqual(store.checklist_items(db, task)[0]['label'], '手动检查项')
        db.close()
        os.environ.pop('TASKBOARD_DATA_ROOT')
        self.assertEqual(store.prepare_data_root(self.install)['status'], 'already_migrated')

    def test_unwritable_install_reports_fallback_without_selecting_it(self):
        with patch.object(store, 'writable', return_value=False):
            result = store.prepare_data_root(self.install)
        self.assertEqual(result['status'], 'fallback')
        self.assertEqual(result['path'], str(self.root / 'TaskBoardAccio'))
        self.assertNotIn('TASKBOARD_DATA_ROOT', os.environ)

    def test_existing_target_attachments_are_never_overwritten(self):
        db = store.connect()
        store.save_task(db, '旧任务', '', '计划中', None)
        db.close()
        target_files = self.install / 'attachments'
        target_files.mkdir(parents=True)
        original = target_files / 'user-file.txt'
        original.write_bytes(b'keep')
        with self.assertRaisesRegex(RuntimeError, '目标目录已有附件'):
            store.prepare_data_root(self.install)
        self.assertEqual(original.read_bytes(), b'keep')
        self.assertFalse((self.install / 'tasks.sqlite3').exists())

    def test_failed_file_swap_does_not_leave_empty_migration_destination(self):
        db = store.connect()
        tid = store.save_task(db, '不能丢失', '', '计划中', None)
        legacy_db = store.DB
        db.close()
        replace = os.replace
        def fail_attachment_move(source, target):
            if str(source).endswith('attachments'):
                raise OSError('simulated attachment move failure')
            return replace(source, target)
        with patch.object(store.os, 'replace', side_effect=fail_attachment_move):
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                store.prepare_data_root(self.install)
        self.assertTrue(legacy_db.is_file())
        self.assertFalse((self.install / 'tasks.sqlite3').exists())
        db = store.connect()
        self.assertEqual(store.task(db, tid)['title'], '不能丢失')
        db.close()

    def test_single_instance_lock_contention_is_clean_and_recoverable(self):
        from launcher import InstanceLock
        first, second = InstanceLock(self.root), InstanceLock(self.root)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
        finally:
            first.close(); second.close()


if __name__ == '__main__':
    unittest.main()
