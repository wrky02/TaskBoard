import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'Data'
        self.root.mkdir()
        db = sqlite3.connect(self.root / 'tasks.sqlite3')
        db.executescript('''
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE tasks(id INTEGER PRIMARY KEY, title TEXT, body TEXT);
            CREATE TABLE attachments(id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id),stored_name TEXT);
            CREATE TABLE drafts(id TEXT PRIMARY KEY, data TEXT);
            INSERT INTO tasks VALUES(1,'私密标题-private-title','私密正文-private-body');
            INSERT INTO attachments VALUES(1,1,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin');
            INSERT INTO drafts VALUES('draft','未保存草稿-private-draft');
            PRAGMA user_version=12;
        ''')
        db.commit()
        db.close()
        (self.root / 'attachments').mkdir()
        (self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin').write_bytes(b'private-attachment-content')
        (self.root / 'history-objects').mkdir()
        (self.root / 'history-objects/hash').write_bytes(b'private-old-version-content')
        (self.root / 'staging/chunk-upload').mkdir(parents=True)
        (self.root / 'staging/chunk-upload/part-0').write_bytes(b'pending-upload-content')
        (self.root / 'backups').mkdir()
        with zipfile.ZipFile(self.root / 'backups/old.zip', 'w') as archive:
            archive.write(self.root / 'tasks.sqlite3', 'tasks.sqlite3')
            archive.write(self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin', 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin')
        self.password = 'Correct Password 中文 2026'

    def tearDown(self):
        for root in list(vault._sessions):
            if root.is_relative_to(Path(self.temp.name)):
                vault.lock(root)
        self.temp.cleanup()

    def enable(self):
        return vault.enable(self.root, self.password)

    def test_roundtrip_covers_database_drafts_history_files_and_old_backups(self):
        result = self.enable()
        self.assertTrue(result['enabled'])
        self.assertTrue(vault.is_enabled(self.root))
        self.assertFalse((self.root / 'tasks.sqlite3').read_bytes().startswith(b'SQLite format 3'))
        with self.assertRaises(sqlite3.DatabaseError):
            db = sqlite3.connect(self.root / 'tasks.sqlite3')
            try:
                db.execute('SELECT * FROM tasks').fetchall()
            finally:
                db.close()
        db = vault.connection(self.root)
        try:
            self.assertEqual(db.execute('SELECT title FROM tasks').fetchone()['title'], '私密标题-private-title')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 12)
            with db:
                db.execute('UPDATE tasks SET body=?', ('保存后-private-modified-body',))
        finally:
            db.close()
        self.assertEqual(vault.read_bytes(self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'), b'private-attachment-content')
        self.assertEqual(vault.file_size(self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'), len(b'private-attachment-content'))
        self.assertEqual(vault.read_bytes(self.root / 'history-objects/hash'), b'private-old-version-content')
        self.assertEqual(vault.read_bytes(self.root / 'staging/chunk-upload/part-0'), b'pending-upload-content')
        with vault.open_archive(self.root / 'backups/old.zip', root=self.root) as stream:
            with zipfile.ZipFile(stream) as archive:
                self.assertEqual(archive.read('attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'), b'private-attachment-content')
        for path in self.root.rglob('*'):
            if path.is_file():
                content = path.read_bytes()
                for secret in (b'private-title', b'private-body', b'private-attachment-content', b'private-draft', b'private-old-version', b'pending-upload-content'):
                    self.assertNotIn(secret, content, str(path))
        self.assertFalse(list(self.root.glob('.vault-*')))

    def test_locked_wrong_password_and_recovery_unlock(self):
        result = self.enable()
        vault.lock(self.root)
        with self.assertRaises(vault.VaultLocked):
            vault.connection(self.root)
        with self.assertRaises(vault.VaultLocked):
            vault.read_bytes(self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin')
        with self.assertRaises(ValueError):
            vault.unlock(self.root, 'incorrect-password')
        self.assertFalse(vault.is_unlocked(self.root))
        vault.unlock(self.root, recovery_key=result['recovery_key'])
        self.assertEqual(vault.read_bytes(self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'), b'private-attachment-content')

    def test_tampering_and_object_substitution_are_rejected(self):
        self.enable()
        path = self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with self.assertRaises(ValueError):
            vault.read_bytes(path)
        path.write_bytes(data)
        other = self.root / 'attachments/copied.bin'
        other.write_bytes(data)
        with self.assertRaises(ValueError):
            vault.read_bytes(other)
        old_archive = self.root / 'backups/old.zip'
        data = old_archive.read_bytes()
        old_archive.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with self.assertRaises(ValueError):
            with vault.open_archive(old_archive, root=self.root):
                self.fail('unauthenticated contents must never be yielded')

    def test_password_change_and_recovery_keep_old_backup_boundary(self):
        result = self.enable()
        original_db = (self.root / 'tasks.sqlite3').read_bytes()
        original_file = (self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin').read_bytes()
        vault.change_password(self.root, self.password, 'New Password 2026')
        self.assertEqual(original_db, (self.root / 'tasks.sqlite3').read_bytes())
        self.assertEqual(original_file, (self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin').read_bytes())
        vault.lock(self.root)
        with self.assertRaises(ValueError):
            vault.unlock(self.root, self.password)
        vault.unlock(self.root, 'New Password 2026')
        vault.recover_password(self.root, result['recovery_key'], 'Recovered Password 2026')
        vault.lock(self.root)
        vault.unlock(self.root, 'Recovered Password 2026')
        with self.assertRaises(ValueError):
            with vault.open_archive(self.root / 'backups/old.zip', password='Recovered Password 2026'):
                pass
        with vault.open_archive(self.root / 'backups/old.zip', password=self.password) as stream:
            self.assertTrue(zipfile.is_zipfile(stream))
        with vault.open_archive(self.root / 'backups/old.zip', recovery_key=result['recovery_key']) as stream:
            self.assertTrue(zipfile.is_zipfile(stream))

    def test_snapshot_and_relocating_workspace_to_other_computer(self):
        result = self.enable()
        db = vault.connection(self.root)
        destination = Path(self.temp.name) / 'snapshot.sqlite3'
        try:
            vault.snapshot_database(db, destination)
        finally:
            db.close()
        metadata = vault.backup_metadata(self.root)
        vault.lock(self.root)
        check = vault.validate_backup_database(destination, metadata, password=self.password)
        self.assertEqual(check.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
        check.close()
        moved = Path(self.temp.name) / 'another-computer/Data'
        shutil.copytree(self.root, moved)
        vault.unlock(moved, recovery_key=result['recovery_key'])
        self.assertEqual(vault.read_bytes(moved / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'), b'private-attachment-content')

    def test_atomic_object_and_password_write_failures_preserve_old_data(self):
        self.enable()
        path = self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'
        before = path.read_bytes()
        with mock.patch.object(vault.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                vault.write_bytes(path, b'new private attachment')
            with self.assertRaises(OSError):
                vault.change_password(self.root, self.password, 'Another Password 2026')
        self.assertEqual(path.read_bytes(), before)
        vault.lock(self.root)
        vault.unlock(self.root, self.password)
        self.assertFalse(list(self.root.rglob('*.tmp')))

    def test_recovery_key_rotation_only_invalidates_current_metadata(self):
        result = self.enable()
        new_key = vault.new_recovery_key(self.root, self.password)
        vault.lock(self.root)
        with self.assertRaises(ValueError):
            vault.unlock(self.root, recovery_key=result['recovery_key'])
        vault.unlock(self.root, recovery_key=new_key)
        with vault.open_archive(self.root / 'backups/old.zip', recovery_key=result['recovery_key']) as stream:
            self.assertTrue(zipfile.is_zipfile(stream))

    def _crash_migration(self, marker):
        script = '''
import os,sys
from pathlib import Path
import vault
root=Path(sys.argv[1]); marker=sys.argv[2]
replace=vault.os.replace
def crash(source,dest):
    replace(source,dest)
    if Path(source).parent.name.startswith('.vault-stage-') and Path(source).name==marker:
        os._exit(77)
vault.os.replace=crash
vault.enable(root,sys.argv[3])
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.root), marker, self.password],
                                cwd=Path(__file__).parent, timeout=30)
        self.assertEqual(result.returncode, 77)
        self.assertTrue((self.root / vault.JOURNAL).is_file())

    def test_process_crash_during_swap_rolls_back_complete_plain_generation(self):
        self._crash_migration('tasks.sqlite3')
        self.assertEqual(vault.recover_migration(self.root), 'rolled_back')
        self.assertFalse(vault.is_enabled(self.root))
        db = sqlite3.connect(self.root / 'tasks.sqlite3')
        self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
        db.close()
        self.assertEqual((self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin').read_bytes(), b'private-attachment-content')
        self.assertTrue(zipfile.is_zipfile(self.root / 'backups/old.zip'))
        self.assertFalse(list(self.root.glob('.vault-*')))

    def test_process_crash_after_commit_finishes_plaintext_cleanup(self):
        self._crash_migration('vault.json')
        self.assertEqual(vault.recover_migration(self.root), 'committed')
        vault.unlock(self.root, self.password)
        self.assertEqual(vault.read_bytes(self.root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'), b'private-attachment-content')
        self.assertFalse(list(self.root.glob('.vault-*')))

    def test_database_and_metadata_tampering_never_register_session(self):
        self.enable()
        vault.lock(self.root)
        path = self.root / 'tasks.sqlite3'
        content = path.read_bytes()
        path.write_bytes(content[:100] + bytes([content[100] ^ 1]) + content[101:])
        with self.assertRaises(Exception):
            vault.unlock(self.root, self.password)
        self.assertFalse(vault.is_unlocked(self.root))
        path.write_bytes(content)
        metadata = json.loads((self.root / vault.META).read_text())
        metadata['id'] = '0' * 32
        (self.root / vault.META).write_text(json.dumps(metadata))
        with self.assertRaises(ValueError):
            vault.unlock(self.root, self.password)
        self.assertFalse(vault.is_unlocked(self.root))

    def test_symlink_cannot_create_plaintext_outside_workspace(self):
        self.enable()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        link = self.root / 'attachments/escape'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks require developer mode on Windows')
        with self.assertRaises(ValueError):
            vault.write_bytes(link / 'content', b'secret')
        self.assertFalse((outside / 'content').exists())

    def test_plain_backup_preview_is_memory_only_and_restore_keeps_encryption(self):
        plain_archive = Path(self.temp.name) / 'export-before-encryption.zip'
        shutil.copy2(self.root / 'backups/old.zip', plain_archive)
        self.enable()
        with zipfile.ZipFile(plain_archive) as archive:
            with vault.validate_backup_bytes(archive.read('tasks.sqlite3'), root=self.root) as db:
                self.assertEqual(db.execute('PRAGMA database_list').fetchone()[2], '')
                self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
        stage = self.root / 'restore-test'
        token = vault.stage_backup_archive(plain_archive, stage, self.root)
        self.assertTrue(vault.is_enabled(stage))
        self.assertFalse((stage / 'tasks.sqlite3').read_bytes().startswith(b'SQLite format 3'))
        content = (stage / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin').read_bytes()
        self.assertNotIn(b'private-attachment-content', content)
        self.assertEqual(token.instance.unseal(content, 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'), b'private-attachment-content')
        prepared = vault.prepare_restore(self.root, stage)
        self.assertEqual(prepared.instance.metadata['id'], token.instance.metadata['id'])

    def test_cross_workspace_cipher_restore_activates_only_after_commit(self):
        first = self.enable()
        source_backup = Path(self.temp.name) / 'cipher.zip'
        with zipfile.ZipFile(source_backup, 'w') as archive:
            for name in ('tasks.sqlite3', 'vault.json', 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'):
                archive.write(self.root / name, name)
        new_root = Path(self.temp.name) / 'new-computer'
        new_root.mkdir()
        stage = new_root / 'restore-new'
        token = vault.stage_backup_archive(source_backup, stage, new_root, recovery_key=first['recovery_key'])
        self.assertFalse(vault.is_unlocked(new_root))
        self.assertTrue(vault.is_unlocked(self.root))
        prepared = vault.prepare_restore(new_root, stage, recovery_key=first['recovery_key'])
        for path in stage.iterdir():
            os.replace(path, new_root / path.name)
        vault.activate_restored(new_root, prepared)
        self.assertEqual(vault.read_bytes(new_root / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'), b'private-attachment-content')
        self.assertEqual(token.instance.metadata['id'], first['workspace_id'])

    def test_old_archive_restored_on_new_computer_remains_encrypted(self):
        enabled = self.enable()
        new_root = Path(self.temp.name) / 'new-computer'
        new_root.mkdir()
        stage = new_root / 'restore-old'
        token = vault.stage_backup_archive(self.root / 'backups/old.zip', stage, new_root, recovery_key=enabled['recovery_key'])
        self.assertIsNotNone(token.instance)
        self.assertTrue(vault.is_enabled(stage))
        self.assertFalse((stage / 'tasks.sqlite3').read_bytes().startswith(b'SQLite format 3'))

    def test_existing_plain_recovery_generations_are_encrypted_with_logical_paths(self):
        generation = self.root / ('previous-generation-' + 'a' * 32)
        generation.mkdir()
        shutil.copy2(self.root / 'tasks.sqlite3', generation / 'tasks.sqlite3')
        shutil.copytree(self.root / 'attachments', generation / 'attachments')
        self.enable()
        self.assertTrue(vault.is_enabled(generation))
        self.assertFalse((generation / 'tasks.sqlite3').read_bytes().startswith(b'SQLite format 3'))
        archived = vault.unlock(generation, self.password)
        self.assertEqual(vault.read_bytes(generation / 'attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bin'), b'private-attachment-content')
        self.assertEqual(archived.metadata['id'], vault.current(self.root).metadata['id'])

    def test_authenticated_metadata_prevents_backup_wrapper_corruption(self):
        self.enable()
        metadata = vault.backup_metadata(self.root)
        metadata['salt'] = vault._b64(b'x' * 16)
        with self.assertRaisesRegex(ValueError, '校验'):
            vault.validate_backup_database(self.root / 'tasks.sqlite3', metadata, root=self.root)


if __name__ == '__main__':
    unittest.main()
