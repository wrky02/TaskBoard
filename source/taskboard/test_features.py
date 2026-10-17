"""Behavioral coverage for 2.1 transactional workflows."""
import json
from datetime import date, datetime, timedelta
import unittest
import features as f
import storage as s
import test_server

class FeatureTests(unittest.TestCase):
    setUp=test_server.HTTPTests.setUp
    tearDown=test_server.HTTPTests.tearDown
    request=test_server.HTTPTests.request
    login=test_server.HTTPTests.login
    save=test_server.HTTPTests.save
    def test_recurrence_calendar_rules(self):
        self.assertEqual(f.next_due('2028-01-31','monthly','due',31,date(2028,1,31)),'2028-02-29')
        self.assertEqual(f.next_due('2028-02-29','monthly','due',31,date(2028,2,29)),'2028-03-31')
        self.assertEqual(f.next_due('2026-01-01','daily','due',1,date(2026,1,30)),'2026-01-31')
        self.assertEqual(f.next_due('2026-01-01','weekly','completion',1,date(2026,1,30)),'2026-02-06')
        with self.assertRaises(ValueError):f.next_due('9999-12-31','daily','due',31,date(9999,12,31))
    def test_recurrence_idempotent_and_undo(self):
        self.login();state=self.save(due=date.today().isoformat(),options={'repeat':'daily'})
        tid=state['tasks'][0]['id']
        code,result,_=self.request('/api/task/action',{'id':tid,'action':'move','status':'完成'})
        self.assertEqual(code,200);self.assertEqual(len(result['tasks']),2)
        child=next(t for t in result['tasks'] if t['id']!=tid)
        self.assertFalse(child['checklist'][0]['done']);self.assertFalse(child['files'])
        code,result,_=self.request('/api/undo',{'key':result['undo']});self.assertEqual(code,200);self.assertEqual(len(result['tasks']),1)
        for status in ('完成','计划中','完成'):self.request('/api/task/action',{'id':tid,'action':'move','status':status})
        self.assertEqual(len(self.request('/api/state')[1]['tasks']),2)
    def test_dependencies_completion_cycles_and_delete(self):
        self.login();a=self.save(title='前置')['tasks'][0];state=self.save(title='后置',dependencies=[a['id']]);b=next(t for t in state['tasks'] if t['title']=='后置')
        self.assertEqual(self.request('/api/task/action',{'id':b['id'],'action':'move','status':'完成'})[0],400)
        self.assertEqual(self.request('/api/task/save',{'id':b['id'],'updated_at':b['updated_at'],'title':b['title'],'status':'完成'})[0],400)
        bulk=self.request('/api/bulk',{'action':'status','ids':[b['id']],'status':'完成'})[1];self.assertEqual(len(bulk['result']['failed']),1)
        self.assertEqual(self.request('/api/task/save',{'id':a['id'],'updated_at':a['updated_at'],'title':a['title'],'dependencies':[b['id']]})[0],400)
        self.request('/api/task/action',{'id':a['id'],'action':'trash'})
        self.assertEqual(self.request('/api/task/action',{'id':a['id'],'action':'erase'})[0],400)
        self.request('/api/task/action',{'id':a['id'],'action':'restore'});self.request('/api/task/action',{'id':a['id'],'action':'move','status':'完成'})
        self.assertEqual(self.request('/api/task/action',{'id':b['id'],'action':'move','status':'完成'})[0],200)
    def test_reminder_snooze_ack_and_reschedule(self):
        self.login();state=self.save(due=date.today().isoformat(),options={'reminder':'today','reminder_time':'00:00'})
        tid=state['tasks'][0]['id'];r=self.request('/api/reminders')[1]['reminders'][0]
        self.request('/api/reminder',{'id':tid,'key':r['key'],'action':'snooze'});self.assertFalse(self.request('/api/reminders')[1]['reminders'])
        db=s.connect()
        try:self.assertTrue(f.reminders(db,datetime.now()+timedelta(minutes=16)))
        finally:db.close()
        self.request('/api/reminder',{'id':tid,'key':r['key'],'action':'ack'})
        task=self.request('/api/state')[1]['tasks'][0];self.save(id=tid,updated_at=task['updated_at'],due=(date.today()-timedelta(days=1)).isoformat())
        self.assertTrue(self.request('/api/reminders')[1]['reminders'])
        self.assertEqual(self.request('/api/reminder',{'id':tid,'key':r['key'],'action':'ack'})[0],400)
    def test_priority_today_views_tags_batch(self):
        self.login();task=self.save(due='2028-01-01',options={'priority':0,'today':date.today().isoformat()})['tasks'][0]
        self.assertEqual(task['due_date'],'2028-01-01');self.assertEqual(task['options']['priority'],0)
        self.request('/api/saved-view',{'name':'紧急','filters':{'priority':'0'}})
        for name in ('原标签','目标'):self.request('/api/tag',{'action':'create','name':name})
        a,b=[t['id'] for t in self.request('/api/state')[1]['tags']]
        self.request('/api/bulk',{'action':'edit','ids':[task['id']],'changes':{'tags':[a],'priority':1,'due':'2029-02-03'}})
        self.request('/api/tag',{'action':'color','id':b,'color':'blue'})
        state=self.request('/api/tag',{'action':'merge','id':a,'target':b})[1]
        self.assertEqual(state['tasks'][0]['tags'][0]['id'],b);self.assertEqual(state['tags'][0]['color'],'blue');self.assertEqual(state['tags'][0]['count'],1)
        self.assertEqual(state['tasks'][0]['options']['priority'],1);self.assertEqual(state['views'][0]['name'],'紧急')
    def test_drafts_privacy_backup_preview_and_undo_conflict(self):
        self.assertEqual(self.request('/api/drafts')[0],401);self.login()
        self.request('/api/draft',{'key':'test-draft','draft':{'title':'私有草稿','note':'私有正文'}})
        self.assertNotIn('data',self.request('/api/drafts')[1]['drafts'][0]);self.assertEqual(self.request('/api/draft?key=test-draft')[1]['draft']['note'],'私有正文')
        report=self.request('/api/diagnostics')[1];self.assertNotIn('私有',json.dumps(report,ensure_ascii=False));self.assertNotIn(str(self.root),json.dumps(report))
        tid=self.save()['tasks'][0]['id'];result=self.request('/api/task/action',{'id':tid,'action':'trash'})[1];self.assertEqual(self.request('/api/undo',{'key':result['undo']})[0],200)
        result=self.request('/api/task/action',{'id':tid,'action':'move','status':'进行中'})[1];self.request('/api/task/action',{'id':tid,'action':'move','status':'完成'})
        self.assertEqual(self.request('/api/undo',{'key':result['undo']})[0],400)
        raw=self.request('/api/backup')[1];self.assertEqual(self.request('/api/restore-preview',raw=raw)[1]['tasks'],1)
        self.request('/api/draft',{'key':'test-draft','action':'delete'});self.request('/api/restore',raw=raw);self.assertTrue(self.request('/api/drafts')[1]['drafts'])
    def test_migration_backup_and_retention(self):
        self.login();self.save();db=s.connect()
        try:
            with db:
                for table in ('task_dependencies','recurrence','task_options','saved_views','tag_colors','drafts','undo_actions'):db.execute('DROP TABLE '+table)
        finally:db.close()
        db=s.connect()
        try:
            self.assertEqual(len(list(self.root.glob('before-v2.1-*.zip'))),1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],1)
            folder=self.root/'backups';folder.mkdir()
            for day in range(1,10):(folder/f'auto-2020-01-{day:02}.zip').write_bytes(b'old')
            f.auto_backup(db);self.assertEqual(len(list(folder.glob('auto-*.zip'))),7)
            current=folder/('auto-'+date.today().isoformat()+'.zip');s.validate_backup(current)
            before=current.stat().st_mtime_ns;f.auto_backup(db);self.assertEqual(before,current.stat().st_mtime_ns)
        finally:db.close()
    def test_directory_import_and_reordered_undo(self):
        import base64,io,zipfile
        self.login()
        for title in ('A','B','C'):self.save(title=title)
        before=[t['id'] for t in self.request('/api/state')[1]['tasks']]
        result=self.request('/api/task/action',{'id':before[0],'action':'move','status':'计划中','index':2})[1]
        restored=self.request('/api/undo',{'key':result['undo']})[1]
        self.assertEqual([t['id'] for t in restored['tasks']],before)
        raw=self.request('/api/backup')[1]
        with zipfile.ZipFile(io.BytesIO(raw)) as z:files=[{'name':name,'data':base64.b64encode(z.read(name)).decode()} for name in z.namelist()]
        self.assertEqual(self.request('/api/migration-preview',{'files':files})[1]['tasks'],3)
        self.save(title='导入后应被替换')
        self.assertEqual(len(self.request('/api/migration-import',{'files':files})[1]['state']['tasks']),3)
        self.assertEqual(self.request('/api/migration-preview',{'files':[{'name':'../escape','data':'YQ=='}]})[0],400)
if __name__=='__main__':unittest.main()
