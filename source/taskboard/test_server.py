"""Real HTTP regressions against isolated databases and attachment directories."""
import base64
from http.cookiejar import CookieJar
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
import zipfile
import storage as store
from server import TaskServer

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'TASKBOARD_DATA_ROOT': str(self.root)}); self.env.start()
        db = store.connect(); db.close()
        self.server = TaskServer(); self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))
    def tearDown(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close(); self.env.stop(); self.tmp.cleanup()
    def request(self, path, data=None, headers=None, raw=None):
        body = json.dumps(data).encode() if data is not None else raw
        h = {'Origin': self.server.origin, 'X-TaskBoard': self.server.capability, 'Content-Type': 'application/zip' if raw is not None else 'application/json'} if body is not None else {}
        h.update(headers or {})
        try: response = self.client.open(Request(self.server.origin + path, data=body, headers=h))
        except HTTPError as exc: response = exc
        content = response.read()
        result = json.loads(content) if 'application/json' in response.headers.get('Content-Type', '') else content
        return response.status, result, response.headers
    def login(self):
        status, result, headers = self.request('/api/login', {'password':'password123','confirm':'password123'})
        self.assertEqual(status, 200, result); self.assertIn('HttpOnly', headers['Set-Cookie']); return result
    def save(self, **kwargs):
        item = {'title':'中文任务','note':'开发备注','status':'计划中','tags':[], 'checklist':[{'label':'手动检查','done':True}], 'milestones':[{'name':'样件确认','confirmed':True}], 'new_files':[], **kwargs}
        status, result, _ = self.request('/api/task/save', item)
        self.assertEqual(status, 200, result); return result
    def test_auth_origin_host_and_lock(self):
        self.assertEqual(self.request('/api/state')[0], 401)
        self.assertEqual(self.request('/api/file?id=1')[0], 401)
        self.assertEqual(self.request('/api/login', {}, {'Origin':'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/login', {}, {'X-TaskBoard':'wrong'})[0], 403)
        self.assertEqual(self.request('/api/status', headers={'Host':'evil.example'})[0], 403)
        self.login(); self.assertEqual(self.request('/api/state')[0], 200)
        self.assertEqual(self.request('/api/lock', {})[0], 200)
        self.assertEqual(self.request('/api/state')[0], 401)
        self.assertEqual(self.request('/api/backup')[0], 401)
    def test_atomic_draft_ownership_and_duplicate_reset(self):
        self.login(); state = self.save(new_files=[{'name':'草稿.txt','data':base64.b64encode(b'attachment').decode()}])
        t = state['tasks'][0]; fid = t['files'][0]['id']
        self.assertEqual(self.request('/api/file?id='+str(fid))[1], b'attachment')
        invalid = {**t,'due':'','tags':[],'title':'不应保存','new_files':[{'name':'bad.bin','data':'!bad'}]}
        self.assertEqual(self.request('/api/task/save', invalid)[0], 400)
        self.assertEqual(self.request('/api/state')[1]['tasks'][0]['title'], '中文任务')
        self.assertEqual(len(list(store.FILES.iterdir())), 1)
        self.assertEqual(self.request('/api/task/save', {**invalid,'new_files':[],'removed_files':[999]})[0], 400)
        state = self.save(id=t['id'], updated_at=t['updated_at'], file_versions=[{'id':fid,'version_label':'V2','version_note':'改稿'}])
        self.assertEqual(state['tasks'][0]['files'][0]['version_label'], 'V2')
        self.assertEqual(self.request('/api/task/save', {**invalid,'new_files':[]})[0], 400)
        status, state, _ = self.request('/api/task/action', {'id':t['id'],'action':'duplicate'})
        self.assertEqual(status, 200)
        copy = next(x for x in state['tasks'] if x['id'] != t['id'])
        self.assertFalse(copy['files']); self.assertFalse(copy['checklist'][0]['done'])
        self.assertEqual(self.request('/api/save_template', {})[0], 404)
    def test_backup_restore_archive_trash_bulk_and_export(self):
        self.login(); state = self.save(status='完成',new_files=[{'name':'test.txt','data':base64.b64encode(b'bytes').decode()}]); tid=state['tasks'][0]['id']
        status, result, _ = self.request('/api/bulk', {'ids':[tid,999],'action':'status','status':'完成'})
        self.assertEqual(status,200);self.assertEqual(result['result']['ok'],[tid]);self.assertEqual(len(result['result']['failed']),1)
        self.assertEqual(self.request('/api/task/action',{'id':tid,'action':'archive'})[0],200)
        self.assertIn('中文任务',self.request('/api/export/csv')[1].decode('utf-8-sig'))
        backup=self.request('/api/backup')[1]
        with zipfile.ZipFile(io.BytesIO(backup)) as z:self.assertEqual(len(z.namelist()),2)
        self.assertEqual(self.request('/api/task/action',{'id':tid,'action':'trash'})[0],200)
        self.assertEqual(self.request('/api/state')[1]['trash'][0]['id'],tid)
        self.assertEqual(self.request('/api/restore',raw=b'bad zip')[0],400)
        self.assertTrue(self.request('/api/state')[1]['trash'])
        status,result,_=self.request('/api/restore',raw=backup)
        self.assertEqual(status,200,result);self.assertEqual(result['state']['archived'][0]['id'],tid)
        self.assertTrue((self.root/result['safety']).is_file());self.assertEqual(self.request('/api/health')[1]['missing'],[])
    def test_settings_tags_password_and_headers(self):
        self.login();self.assertEqual(self.request('/api/tag',{'action':'create','name':'设计'})[0],200)
        tag=self.request('/api/state')[1]['tags'][0]['id'];state=self.save(tags=[tag],due='2026-11-13')
        self.assertEqual(state['tasks'][0]['tags'][0]['name'],'设计');self.assertEqual(state['tasks'][0]['due_date'],'2026-11-13')
        self.assertEqual(self.request('/api/view',{'query':'中文','tag_filter':str(tag)})[0],200)
        state=self.request('/api/settings',{'system_name':'原样名称','user_name':'张三','reminder_mode':'off'})[1]
        self.assertEqual(state['names']['system_name'],'原样名称');self.assertEqual(state['view_state']['query'],'中文')
        self.assertEqual(self.request('/api/password',{'old':'wrong','new':'newpass123','confirm':'newpass123'})[0],400)
        self.assertEqual(self.request('/api/password',{'old':'password123','new':'newpass123','confirm':'newpass123'})[0],200)
        self.request('/api/lock',{})
        self.assertEqual(self.request('/api/login',{'password':'password123'})[0],401)
        self.assertEqual(self.request('/api/login',{'password':'newpass123'})[0],200)
        status,body,headers=self.request('/');self.assertEqual(status,200);self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertTrue(self.request('/app.js')[2]['Content-Type'].startswith('text/javascript'))
        self.assertTrue(self.request('/style.css')[2]['Content-Type'].startswith('text/css'))

    def test_attachment_remove_and_read_only_health_report(self):
        self.login()
        state = self.save(new_files=[{'name':'remove.txt','data':base64.b64encode(b'content').decode()}])
        task = state['tasks'][0]
        fid = task['files'][0]['id']
        state = self.save(id=task['id'], updated_at=task['updated_at'], removed_files=[fid])
        self.assertFalse(state['tasks'][0]['files'])
        self.assertEqual(self.request('/api/file?id='+str(fid))[0], 404)
        orphan = store.FILES / ('a'*32+'.txt')
        orphan.write_bytes(b'keep')
        result = self.request('/api/health')[1]
        self.assertEqual(len(result['orphaned_files']), 1)
        self.assertTrue(orphan.is_file())

    def test_login_rate_limit(self):
        self.login(); self.request('/api/lock', {})
        for _ in range(5):
            self.assertEqual(self.request('/api/login', {'password':'incorrect'})[0], 401)
        self.assertEqual(self.request('/api/login', {'password':'password123'})[0], 429)
        self.server.retry_at = 0
        self.assertEqual(self.request('/api/login', {'password':'password123'})[0], 200)


class MigrationTests(unittest.TestCase):
    def test_legacy_retirement_preserves_data_and_backups_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TASKBOARD_DATA_ROOT':tmp}):
            db=store.connect();store.set_password(db,'password123');tid=store.save_task(db,'历史任务','<p>旧备注</p>','计划中',None)
            store.add_checklist_item(db,tid,'历史检查项');store.add_file_bytes(db,tid,'旧附件.txt',b'history')
            db.execute('CREATE TABLE templates(id INTEGER PRIMARY KEY,name TEXT,checklist TEXT,created_at TEXT)');db.execute('ALTER TABLE tasks ADD COLUMN template_id INTEGER')
            db.execute('INSERT INTO templates VALUES(1,?,?,?)',('历史','["历史检查项"]',store.now()));db.execute('UPDATE tasks SET template_id=1 WHERE id=?',(tid,));db.commit();db.close()
            db=store.connect();self.assertTrue(store.check_password(db,'password123'));self.assertEqual(store.checklist_items(db,tid)[0]['label'],'历史检查项');self.assertEqual(len(store.attachments(db,tid)),1)
            self.assertNotIn('template_id',[r['name'] for r in db.execute('PRAGMA table_info(tasks)')]);self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='templates'").fetchone());self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok');db.close()
            backups=list(Path(tmp).glob('before-v2-*.zip'));self.assertEqual(len(backups),1);self.assertTrue(store.validate_backup(backups[0]))
            db=store.connect();db.close();self.assertEqual(len(list(Path(tmp).glob('before-v2-*.zip'))),1)

    def test_restore_failure_rolls_back_original_database_and_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'TASKBOARD_DATA_ROOT': tmp}):
            db = store.connect()
            tid = store.save_task(db, '原任务', '', '计划中', None)
            store.add_file_bytes(db, tid, 'original.txt', b'original')
            archive = Path(tmp) / 'backup.zip'
            store.backup(db, archive)
            store.save_task(db, '恢复前新增', '', '计划中', None)
            replace = os.replace
            def fail_staged_swap(source, target):
                if Path(source).parent.name.startswith('restore-') and Path(source).name == 'tasks.sqlite3':
                    raise OSError('simulated restore swap failure')
                return replace(source, target)
            with patch.object(store.os, 'replace', side_effect=fail_staged_swap):
                with self.assertRaisesRegex(OSError, 'simulated'):
                    store.restore_backup(db, archive)
            fresh = store.connect()
            self.assertEqual(len(store.tasks(fresh)), 2)
            self.assertEqual(store.attachment_health_check(fresh)['missing'], [])
            self.assertEqual(fresh.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            fresh.close()
            self.assertTrue(list(Path(tmp).glob('pre-restore-*.zip')))

if __name__=='__main__':unittest.main()
