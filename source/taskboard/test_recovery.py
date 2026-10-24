"""Real process interruption tests, archive verification and immutable history."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import recovery as r
import storage as s


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.environment=patch.dict(os.environ,{'TASKBOARD_DATA_ROOT':str(self.root)})
        self.environment.start();self.db=s.connect()

    def tearDown(self):
        try:r.wait_for_jobs(self.root)
        except Exception:pass
        self.db.close();self.environment.stop();self.temp.cleanup()

    def task(self,title='初始任务'):
        return s.save_task(self.db,title,'完整正文','计划中',None)

    def test_generation_swap_retries_windows_sharing_violation(self):
        source=self.root/'replacement.tmp';destination=self.root/'replacement.json'
        source.write_text('ready',encoding='utf-8')
        transient=PermissionError(13,'file in use')
        transient.winerror=32
        real_replace=os.replace
        calls=[]
        def replace(source,destination):
            calls.append((source,destination))
            if len(calls)==1:raise transient
            real_replace(source,destination)
        with patch.object(r.os,'replace',side_effect=replace),patch.object(r.time,'sleep') as sleep:
            r._replace_with_retry(source,destination)
        self.assertEqual(destination.read_text(encoding='utf-8'),'ready')
        self.assertEqual(len(calls),2)
        sleep.assert_called_once_with(0.025)

    def test_manifest_hashes_legacy_compatibility_and_corruption(self):
        tid=self.task();s.add_file_bytes(self.db,tid,'资料.txt',b'original')
        path=self.root/'backup.zip';s.backup(self.db,path)
        with zipfile.ZipFile(path) as archive:
            manifest=json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['format_version'],2)
            members={name:archive.read(name) for name in archive.namelist()}
            for name,expected in manifest['files'].items():
                self.assertEqual(hashlib.sha256(members[name]).hexdigest(),expected['sha256'])
        legacy=self.root/'legacy.zip'
        with zipfile.ZipFile(legacy,'w') as archive:
            for name,data in members.items():
                if name!='manifest.json':archive.writestr(name,data)
        self.assertTrue(s.validate_backup(legacy))
        corrupted=self.root/'corrupt.zip'
        with zipfile.ZipFile(corrupted,'w') as archive:
            for name,data in members.items():archive.writestr(name,b'changed' if name.startswith('attachments/') else data)
        with self.assertRaisesRegex(ValueError,'校验失败'):s.validate_backup(corrupted)

    def test_history_preserves_removed_attachment_and_full_text(self):
        tid=self.task();fid=s.add_file_bytes(self.db,tid,'原始附件.txt',b'keep historical bytes')
        with self.db:version=r.history_record(self.db,tid,'正式保存')
        s.remove_file(self.db,fid)
        s.save_task(self.db,'修改后','修改后的完整正文','计划中',None,task_id=tid)
        with self.db:r.history_record(self.db,tid,'编辑')
        changes=r.history_diff(self.db,version)['changes']
        self.assertIn('title',[row['field'] for row in changes])
        payload=r.history_restore_payload(self.db,version)
        self.assertEqual(payload['title'],'初始任务')
        self.assertEqual(base64.b64decode(payload['new_files'][0]['data']),b'keep historical bytes')
        self.assertNotIn('id',r.history_restore_payload(self.db,version,as_new=True))
        path=self.root/'with-history.zip';s.backup(self.db,path)
        with zipfile.ZipFile(path) as archive:
            self.assertTrue(any(name.startswith('history-objects/') for name in archive.namelist()))
        self.assertTrue(s.validate_backup(path))

    def test_history_erase_remains_recoverable_as_new(self):
        tid=self.task();s.add_file_bytes(self.db,tid,'history.txt',b'not silently erased')
        s.trash(self.db,tid);s.erase(self.db,tid)
        version=r.history_list(self.db,tid)[0]['id']
        with self.assertRaisesRegex(ValueError,'恢复为新任务'):r.history_restore_payload(self.db,version)
        restored=r.history_restore_payload(self.db,version,as_new=True)
        self.assertEqual(restored['title'],'初始任务')
        self.assertEqual(base64.b64decode(restored['new_files'][0]['data']),b'not silently erased')

    def test_restore_process_crash_at_every_swap_boundary(self):
        phases=('prepared','old-tasks.sqlite3','new-tasks.sqlite3',
                'old-attachments','new-attachments','old-history-objects',
                'new-history-objects','old-staging','new-staging','committed')
        code='import storage as s; s.restore_backup(s.connect(), __import__("sys").argv[1])'
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                with patch.dict(os.environ,{'TASKBOARD_DATA_ROOT':str(root)}):
                    db=s.connect();tid=s.save_task(db,'备份任务','','计划中',None)
                    s.add_file_bytes(db,tid,'原附件.txt',b'backup-file')
                    archive=root/'full.zip';s.backup(db,archive)
                    s.save_task(db,'恢复前任务','','计划中',None,task_id=tid)
                    s.add_file_bytes(db,tid,'新增附件.txt',b'before-restore-file');db.close()
                    environment=dict(os.environ,TASKBOARD_RECOVERY_CRASH_AT=phase)
                    process=subprocess.run([sys.executable,'-c',code,str(archive)],cwd=Path(__file__).parent,env=environment,capture_output=True,timeout=30)
                    self.assertEqual(process.returncode,86,process.stderr.decode())
                    reopened=s.connect()
                    try:
                        expected='备份任务' if phase=='committed' else '恢复前任务'
                        self.assertEqual(s.task(reopened,tid)['title'],expected)
                        self.assertEqual(reopened.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                        for file in s.attachments(reopened,tid):self.assertTrue((s.FILES/file['stored_name']).is_file())
                        self.assertFalse((root/r.JOURNAL).exists())
                    finally:reopened.close()
        # Restore module-level paths for this test's original connection.
        s.ROOT=self.root;s.DB=self.root/'tasks.sqlite3';s.FILES=self.root/'attachments'

    def test_migration_crash_does_not_accept_partial_destination(self):
        self.db.close()
        for phase in ('prepared','new-tasks.sqlite3','new-attachments','committed'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);legacy=root/'TaskBoardAccio';target=root/'install'/'Data'
                with patch.dict(os.environ,{'TASKBOARD_DATA_ROOT':str(legacy),'LOCALAPPDATA':str(root)}):
                    db=s.connect();tid=s.save_task(db,'迁移不能丢','','计划中',None)
                    s.add_file_bytes(db,tid,'资料.txt',b'legacy-file');db.close()
                    environment=dict(os.environ,TASKBOARD_RECOVERY_CRASH_AT=phase);environment.pop('TASKBOARD_DATA_ROOT')
                    code='import storage as s; s.migrate_data_root(__import__("sys").argv[1])'
                    process=subprocess.run([sys.executable,'-c',code,str(target)],cwd=Path(__file__).parent,env=environment,capture_output=True,timeout=30)
                    self.assertEqual(process.returncode,86,process.stderr.decode())
                    result=s.migrate_data_root(target)
                    self.assertIn(result['status'],('migrated','already_migrated'))
                    with patch.dict(os.environ,{'TASKBOARD_DATA_ROOT':str(target)}):
                        reopened=s.connect()
                        try:
                            self.assertEqual(s.task(reopened,tid)['title'],'迁移不能丢')
                            self.assertFalse(s.attachment_health_check(reopened)['missing'])
                        finally:reopened.close()
        self.db=s.connect()

    def test_backup_is_background_and_changes_during_backup_are_not_missed(self):
        self.task();started=threading.Event();release=threading.Event();original=s.backup
        def delayed(db,destination):
            started.set();release.wait(10);return original(db,destination)
        with patch.object(s,'backup',side_effect=delayed):
            result=r.tick(self.db,now=100,force=True)
            self.assertTrue(result['job']['id']);self.assertTrue(started.wait(5))
            self.task('备份期间仍可保存')
            release.set();r.wait_for_jobs(self.root)
        r.tick(self.db,now=101)
        first=r.status(self.db)['job']['id']
        result=r.tick(self.db,now=1001)
        self.assertNotEqual(first,result['job']['id'])
        r.wait_for_jobs(self.root);r.tick(self.db,now=1002)
        self.assertTrue(r.status(self.db)['last']['verified'])

    def test_external_target_failure_is_visible_and_original_unchanged(self):
        self.task();r.configure(self.db,dict(target=str(self.root/'disconnected'/'backups')))
        before=self.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        r.tick(self.db,force=True)
        try:r.wait_for_jobs(self.root)
        except ValueError:pass
        report=r.tick(self.db)
        self.assertEqual(report['job']['stage'],'failed')
        self.assertIn('备份目标不可用',report['job']['error'])
        self.assertEqual(before,self.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])


if __name__=='__main__':unittest.main()
