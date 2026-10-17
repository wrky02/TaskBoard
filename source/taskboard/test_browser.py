"""System Chromium regression. This does not validate Windows IME rendering."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import date
from playwright.sync_api import sync_playwright, expect

HERE = Path(__file__).resolve().parent
ARTIFACTS = Path(os.environ.get('TASKBOARD_ARTIFACTS', str(HERE.parent.parent / 'artifacts')))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='taskboard-browser-') as tmp:
    root = Path(tmp)
    process = subprocess.Popen([sys.executable,str(HERE/'launcher.py'),'--no-browser','--data-root',str(root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            info_path = root/'.instance.json'
            if info_path.exists(): break
            if process.poll() is not None: raise AssertionError(process.stderr.read().decode())
            time.sleep(.1)
        info=json.loads(info_path.read_text());origin=f"http://127.0.0.1:{info['port']}";url=origin+'/#'+info['capability']
        with sync_playwright() as p:
            executable=os.environ.get('TASKBOARD_CHROMIUM')
            if not executable and Path('/usr/bin/chromium').exists(): executable='/usr/bin/chromium'
            browser=p.chromium.launch(executable_path=executable,headless=True)
            context=browser.new_context(viewport={'width':1440,'height':1024},accept_downloads=True)
            page=context.new_page();errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
            page.goto(url);expect(page.locator('#loginHeading')).to_have_text('创建你的工作区')
            page.screenshot(path=str(ARTIFACTS/'login.png'))
            page.fill('#password','password123');page.fill('#confirmPassword','password123');page.locator('#loginForm button').click()
            expect(page.locator('#workspace')).to_be_visible();expect(page.locator('#lanes .lane')).to_have_count(3)
            page.click('#newTask');expect(page.locator('#taskTitle')).to_be_focused()
            page.evaluate("window.originalTitle=document.querySelector('#taskTitle');window.originalLanes=document.querySelector('#lanes');window.changes=0;window.obs=new MutationObserver(()=>window.changes++);obs.observe(originalLanes,{subtree:true,childList:true,attributes:true})")
            page.locator('#taskTitle').press_sequentially('中文标题输入测试 ABC')
            assert page.evaluate("originalTitle===document.querySelector('#taskTitle') && originalLanes===document.querySelector('#lanes') && changes===0")
            page.fill('#taskNote','验证备注、附件与检查清单一并保存。')
            page.select_option('#taskStatus','进行中');page.fill('#taskDue',date.today().isoformat())
            page.fill('#checkInput','核对需求');page.click('#addCheck');page.locator('[data-check]').check()
            page.fill('#milestoneInput','完成评审');page.click('#addMilestone');page.locator('[data-milestone]').check()
            png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1cAAAAASUVORK5CYII=')
            page.set_input_files('#fileInput',{'name':'截图.png','mimeType':'image/png','buffer':png})
            page.screenshot(path=str(ARTIFACTS/'task-drawer.png'))
            page.click('#closeDrawer');expect(page.locator('#dialog')).to_be_visible();page.click('#dialogCancel')
            expect(page.locator('#taskTitle')).to_have_value('中文标题输入测试 ABC')
            page.click('#saveTask');expect(page.locator('#drawer')).to_be_hidden();expect(page.locator('.card')).to_have_count(1)
            page.locator('.card').click();expect(page.locator('[data-check]')).to_be_checked();expect(page.locator('[data-milestone]')).to_be_checked()
            page.locator('[data-preview]').click();expect(page.locator('#imageDialog')).to_be_visible();assert page.locator('#previewImage').evaluate('(img)=>img.complete && img.naturalWidth>0');page.click('#closeImage')
            page.set_input_files('#fileInput',{'name':'取消的附件.txt','mimeType':'text/plain','buffer':b'not saved'})
            page.fill('[data-file-label]','V2-unsaved');page.click('[data-remove-file]');page.click('#cancelTask');page.click('#dialogOk');expect(page.locator('#drawer')).to_be_hidden()
            page.locator('.card').click();expect(page.locator('.file-row')).to_have_count(1);expect(page.locator('[data-file-label]')).to_have_value('初稿')
            page.fill('[data-file-label]','V2');page.fill('[data-file-note]','评审稿');page.click('#saveTask');expect(page.locator('#drawer')).to_be_hidden()
            page.locator('.card').click();page.click('#duplicateTask');expect(page.locator('#drawer')).to_be_hidden();expect(page.locator('.card')).to_have_count(2)
            copy=page.locator('.card').filter(has_text='（副本）');copy.click();expect(page.locator('[data-check]')).not_to_be_checked();expect(page.locator('.file-row')).to_have_count(0);page.click('#cancelTask')
            copy.drag_to(page.locator('.lane[data-status="完成"] .lane-items'));expect(page.locator('.lane[data-status="完成"] .card')).to_have_count(1)
            page.click('[data-page="tags"]');page.click('#newTag');page.fill('#dlg-name','产品设计');page.click('#dialogOk');expect(page.locator('.list-row .tag')).to_have_text('产品设计')
            page.click('[data-page="board"]');page.click('#bulkToggle');page.locator('[data-select]').first.check();page.select_option('#bulkTag',label='产品设计');page.click('#bulkAddTag');expect(page.locator('.card .tag')).to_have_count(1)
            page.click('#bulkToggle');page.click('[data-page="calendar"]');expect(page.locator('.calendar-task')).to_have_count(2)
            page.click('[data-page="board"]');page.locator('.lane[data-status="完成"] .card').click();page.click('#archiveTask');expect(page.locator('.card')).to_have_count(1)
            page.click('[data-page="archive"]');expect(page.locator('[data-action="unarchive"]')).to_have_count(1);page.click('[data-action="unarchive"]');expect(page.locator('[data-action="unarchive"]')).to_have_count(0)
            page.click('[data-page="board"]');page.locator('.card').first.click();page.click('#trashTask');page.click('#dialogOk');expect(page.locator('#drawer')).to_be_hidden()
            page.click('[data-page="trash"]');expect(page.locator('[data-action="restore"]')).to_have_count(1);page.click('[data-action="restore"]');expect(page.locator('[data-action="restore"]')).to_have_count(0)
            page.click('[data-page="tools"]');page.fill('#systemName','我的产品工作室');page.fill('#userName','设计师');page.click('#settingsForm button');expect(page.locator('#brandName')).to_have_text('我的产品工作室')
            page.click('#healthBtn');expect(page.locator('#healthReport')).to_contain_text('缺失 0 个')
            for selector,ext in [('#csvBtn','csv'),('#xlsxBtn','xlsx'),('#backupBtn','zip')]:
                with page.expect_download() as downloaded:page.click(selector)
                download=downloaded.value;download.save_as(str(root/('download.'+ext)))
                assert (root/('download.'+ext)).stat().st_size>0
            page.set_input_files('#restoreInput',root/'download.zip');page.click('#dialogOk');expect(page.locator('#toast')).to_contain_text('恢复完成')
            page.click('#lockBtn');expect(page.locator('#login')).to_be_visible();page.fill('#password','password123');page.locator('#loginForm button').click();expect(page.locator('#workspace')).to_be_visible()
            page.click('[data-page="tools"]');page.click('#passwordBtn');page.fill('#dlg-old','password123');page.fill('#dlg-new','newpassword123');page.fill('#dlg-confirm','newpassword123');page.click('#dialogOk');expect(page.locator('#toast')).to_have_text('密码已修改')
            # Seed additional realistic examples through the same protected API
            # for the design artifact, using the isolated test data only.
            for title,status,note in [('新产品需求梳理','计划中','收集使用场景与交付边界，形成第一版需求清单。'),('结构设计方案评审','进行中','核对安装孔位、材料与尺寸公差。'),('首轮样件交付','完成','记录试装反馈，整理交付文件。'),('下周工作安排','计划中','把重要的事安排到日历，留出专注时间。')]:
                response=context.request.post(origin+'/api/task/save',headers={'Origin':origin,'X-TaskBoard':info['capability']},data={'title':title,'status':status,'note':note,'tags':[]})
                assert response.ok,response.text()
            page.reload();expect(page.locator('#workspace')).to_be_visible();page.click('[data-page="board"]');expect(page.locator('.card')).to_have_count(6)
            page.screenshot(path=str(ARTIFACTS/'board.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(ARTIFACTS/'mobile.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            assert not errors,errors
            # Re-launching the same data directory must reuse the instance.
            second=subprocess.run([sys.executable,str(HERE/'launcher.py'),'--no-browser','--data-root',str(root)],capture_output=True,timeout=10)
            assert second.returncode==0,second.stderr.decode();assert json.loads(info_path.read_text())['port']==info['port']
            page.set_viewport_size({'width':1440,'height':1024})
            page.click('#exitBtn');page.click('#dialogOk');expect(page.locator('#loginHeading')).to_have_text('工作区已关闭')
            browser.close()
        process.wait(timeout=10);assert process.returncode==0
        print('PASS browser: first login, stable title DOM, checklist/milestones, staged attachments, preview, duplicate, drag, tags/bulk, calendar, archive/trash, settings/export/backup/restore, lock/password, responsive layout, single instance and shutdown')
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=10)
