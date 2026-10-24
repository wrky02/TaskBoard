"""Exercise the actual private pipe process, shared rules and recovery."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class NativeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.counter=0;self.start()
    def start(self):
        self.process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('native_worker.py')),'--data-root',str(self.root/'Data')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    def stop(self):
        self.process.stdin.close();self.process.wait(timeout=15);error=self.process.stderr.read();self.process.stdout.close();self.process.stderr.close();self.assertEqual(self.process.returncode,0,error)
    def tearDown(self):
        if self.process.poll() is None:self.stop()
        self.tmp.cleanup()
    def call(self,method,**params):
        self.counter+=1;self.process.stdin.write(json.dumps(dict(id=self.counter,method=method,params=params))+'\n');self.process.stdin.flush()
        reply=json.loads(self.process.stdout.readline());self.assertEqual(reply.get('id'),self.counter)
        if 'error' in reply:raise ValueError(reply['error'])
        return reply['result']
    def login(self):return self.call('login',password='native-test-password',confirm='native-test-password')
    def test_auth_atomic_save_files_draft_recovery_and_lock(self):
        self.assertTrue(self.call('status')['first'])
        with self.assertRaisesRegex(ValueError,'解锁'):self.call('state')
        self.login();payload=dict(title='中文任务与原生管道',note='输入法说明',status='计划中',checklist=[dict(label='复核',done=False)],new_files=[dict(name='附件.txt',data=base64.b64encode(b'attachment').decode())])
        self.call('draft-save',key='native-new',draft=payload);self.stop();self.start();self.login()
        recovered=self.call('draft-get',key='native-new');self.assertEqual(recovered,payload)
        saved=self.call('save',**recovered,draft_key='native-new');task=saved['state']['tasks'][0]
        self.assertEqual(Path(task['files'][0]['path']).read_bytes(),b'attachment');self.assertNotIn('url',task['files'][0]);self.assertEqual(self.call('drafts')['drafts'],[])
        self.assertEqual(saved['state']['version'],'4.0.0');self.assertEqual(saved['state']['info']['version'],'4.0.0')
        self.call('lock')
        with self.assertRaisesRegex(ValueError,'解锁'):self.call('drafts')
    def test_dependency_recurrence_bulk_undo(self):
        self.login();first=self.call('save',title='前置',status='计划中')['id'];second=self.call('save',title='后置',status='计划中',due='2026-10-17',dependencies=[first],options=dict(repeat='weekly',priority=0))['id']
        with self.assertRaisesRegex(ValueError,'依赖'):self.call('action',id=second,action='move',status='完成')
        self.call('action',id=first,action='move',status='完成');done=self.call('action',id=second,action='move',status='完成')
        self.assertEqual(len(done['state']['tasks']),3);self.call('undo',key=done['undo']);self.assertEqual(len(self.call('state')['tasks']),2)
        result=self.call('bulk',ids=[first,second],changes=dict(priority=1,due='2027-01-01'));self.assertEqual(result['result']['failed'],[])
    def test_backup_preview_restore_and_privacy(self):
        self.login();self.call('save',title='Private title',note='Private note',status='计划中')
        backup=self.root/'backup.zip';self.call('export',format='zip',path=str(backup));info=self.call('preview',path=str(backup));self.assertEqual(info['tasks'],1)
        self.call('save',title='Extra',status='计划中');result=self.call('restore');self.assertEqual(len(result['state']['tasks']),1);self.assertTrue(Path(result['safety']).is_file())
        dest=self.root/'diagnostics.json';report=self.call('diagnostics',path=str(dest));self.assertFalse(report['browser']);self.assertNotIn('Private',dest.read_text());self.assertNotIn(str(self.root),dest.read_text())
    def test_encrypted_workspace_password_and_recovery_unlock(self):
        self.assertTrue(self.call('status')['first'])
        self.login()
        self.call('save',title='加密后仍可读取',status='计划中')
        enabled=self.call('vault-enable',password='native-test-password')
        self.assertTrue(enabled['enabled']);key=enabled['recovery_key']
        self.assertTrue(enabled['state']['vault']['enabled'])
        self.call('lock');self.stop();self.start()
        self.assertTrue(self.call('status')['encrypted'])
        with self.assertRaisesRegex(ValueError,'密码或恢复密钥错误'):
            self.call('login',password='wrong-password')
        state=self.call('vault-recover',recovery_key=key,new_password='recovered-password',confirm='recovered-password')
        self.assertEqual(state['tasks'][0]['title'],'加密后仍可读取')
        self.call('lock');self.stop();self.start()
        state=self.call('login',password='recovered-password')
        self.assertEqual(state['tasks'][0]['title'],'加密后仍可读取')

    def test_rejects_second_worker_and_releases_lock_on_eof(self):
        self.login();other=subprocess.run([sys.executable,str(Path(__file__).with_name('native_worker.py')),'--data-root',str(self.root/'Data')],input='',capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(other.returncode,1);self.assertIn('另一个',json.loads(other.stdout)['error'])
        self.stop();self.start();self.login()

if __name__=='__main__':unittest.main()
