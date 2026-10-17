"""Drive installed WPF controls through Windows UI Automation.
Also monitor the full process tree for browser processes and TCP listeners.
"""
import io
import base64
import json
from datetime import date,timedelta
import win32clipboard
from PIL import Image
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import psutil
from pywinauto import Desktop

exe=Path(sys.argv[1]).resolve();root=Path(sys.argv[2]).resolve();root.mkdir(parents=True,exist_ok=True)
artifacts=Path('artifacts');artifacts.mkdir(exist_ok=True)
process=None;stop=threading.Event();violations=[];seen=set()

def until(fn,timeout=30):
    deadline=time.monotonic()+timeout;last=None
    while time.monotonic()<deadline:
        try:
            result=fn()
            if result:return result
        except Exception as exc:last=exc
        time.sleep(.25)
    raise AssertionError(f'Timed out: {last}')

def monitor():
    while not stop.wait(.15):
        try:
            parent=psutil.Process(process.pid)
            for p in [parent]+parent.children(recursive=True):
                name=p.name().lower();seen.add(name)
                if any(x in name for x in ('msedge','chrome','firefox','webview','qtwebengine','electron')):violations.append(name)
                try:
                    if any(c.status==psutil.CONN_LISTEN for c in p.net_connections(kind='tcp')):violations.append(name+' opened TCP listener')
                except psutil.AccessDenied:violations.append('cannot inspect network '+name)
        except psutil.NoSuchProcess:pass

def db_scalar(sql):
    with sqlite3.connect(root/'tasks.sqlite3') as db:return db.execute(sql).fetchone()[0]

def mainwindow():return Desktop(backend='uia').window(process=process.pid,title_re='任务看板系统.*',control_type='Window')
def control(win,id):
    result=win.child_window(auto_id=id);result.wait('visible enabled',timeout=30);return result.wrapper_object()
def click(win,id):control(win,id).invoke()
def text(win,id,value):control(win,id).set_edit_text(value)
def dialog(title):
    # WPF owned windows appear beneath their owner in the UIA tree on
    # some Windows builds. Win32 HWND enumeration still finds the actual dialog.
    native=Desktop(backend='win32').window(process=process.pid,title=title)
    native.wait('visible',timeout=30)
    return Desktop(backend='uia').window(handle=native.handle)

try:
    process=subprocess.Popen([str(exe),'--data-root',str(root)])
    threading.Thread(target=monitor,daemon=True).start()
    win=mainwindow();win.wait('visible',timeout=45)
    control(win,'loginPassword').type_keys('native-test-password',with_spaces=True)
    control(win,'loginConfirm').type_keys('native-test-password',with_spaces=True)
    click(win,'login');control(win,'quickTitle')
    text(win,'quickTitle','Native quick task');control(win,'quickTitle').type_keys('{ENTER}')
    until(lambda:db_scalar('SELECT COUNT(*) FROM tasks')==1)
    click(win,'tags');tags=dialog('标签管理');text(tags,'tagName','NativeTag');control(tags,'tagName').type_keys('{ENTER}')
    until(lambda:db_scalar("SELECT COUNT(*) FROM tags WHERE name='NativeTag'")==1);tags.close()
    click(win,'newTask');edit=dialog('新建任务');text(edit,'editorTitle','Recovered native draft');text(edit,'editorNote','Native text without browser')
    edit.child_window(title='明天',control_type='Button').invoke()
    stream=io.BytesIO();Image.new('RGB',(12,12),'purple').save(stream,format='BMP')
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard();win32clipboard.SetClipboardData(win32clipboard.CF_DIB,stream.getvalue()[14:])
    finally:win32clipboard.CloseClipboard()
    edit.child_window(title='粘贴截图',control_type='Button').invoke()
    click(edit,'closeEditor');until(lambda:db_scalar('SELECT COUNT(*) FROM drafts')==1)
    click(win,'drafts');drafts=dialog('草稿恢复');drafts.child_window(title='恢复编辑',control_type='Button').invoke()
    edit=dialog('新建任务');assert control(edit,'editorTitle').window_text()=='Recovered native draft'
    control(edit,'editorPriority').select('紧急');text(edit,'checkInput','Verify native controls');click(edit,'addCheck');click(edit,'saveTask')
    until(lambda:db_scalar('SELECT COUNT(*) FROM tasks')==2);assert db_scalar('SELECT COUNT(*) FROM drafts')==0
    with sqlite3.connect(root/'tasks.sqlite3') as db:
        row=db.execute("SELECT data FROM task_options WHERE task_id=(SELECT id FROM tasks WHERE title='Recovered native draft')").fetchone();assert json.loads(row[0])['priority']==0
        assert db.execute("SELECT due_date FROM tasks WHERE title='Recovered native draft'").fetchone()[0]==(date.today()+timedelta(days=1)).isoformat()
        assert db.execute('SELECT COUNT(*) FROM checklist_items').fetchone()[0]==1
        attachment=db.execute('SELECT stored_name FROM attachments').fetchone()[0]
        assert (root/'attachments'/attachment).read_bytes().startswith(b'\x89PNG')
    # Exercise lock/re-unlock using native password controls.
    click(win,'lock');control(win,'loginPassword').type_keys('native-test-password',with_spaces=True);click(win,'login');control(win,'quickTitle')
    # Real installed icon and WPF class, not Edge app mode.
    assert 'HwndWrapper' in win.wrapper_object().class_name(),win.wrapper_object().class_name()
    screenshot=win.wrapper_object().capture_as_image();screenshot.save(artifacts/'native-board.png')
    preview=screenshot.convert('RGB');preview.thumbnail((1000,700));stream=io.BytesIO();preview.save(stream,format='JPEG',quality=55)
    print('::notice title=Native screenshot::'+base64.b64encode(stream.getvalue()).decode())
    click(win,'settings');settings=dialog('设置与备份');control(settings,'closeBehavior').select('最小化到托盘');click(settings,'saveSettings');settings.close()
    win.close();until(lambda:not mainwindow().is_visible())
    second=subprocess.run([str(exe),'--data-root',str(root)],timeout=15);assert second.returncode==0
    win=mainwindow();until(lambda:win.is_visible());assert process.poll() is None
    click(win,'settings');settings=dialog('设置与备份');control(settings,'closeBehavior').select('退出程序');click(settings,'saveSettings');settings.close()
    win.close();process.wait(timeout=20);assert process.returncode==0
    stop.set();assert not violations,violations;assert 'taskboard.engine.exe' in seen,seen
    assert db_scalar('SELECT COUNT(*) FROM tasks')==2
    report=dict(passed=True,processes=sorted(seen),browser_processes=False,tcp_listeners=False,tests=['native password controls','quick Enter','tag Enter','draft recovery','priority','checklist','native date shortcut','clipboard image through draft restart','lock/unlock','installed WPF window','tray close','second instance reopen','exit','preserve data'])
    (artifacts/'native-acceptance.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('::notice title=Native WPF acceptance::PASS: installed WPF controls, task creation, tag Enter, draft recovery, priority, checklist, tray/reopen/exit; no browser process or TCP listener')
except Exception:
    traceback.print_exc()
    try:
        native=Desktop(backend='win32')
        windows=native.windows(process=process.pid)
        for index,w in enumerate(windows):
            print('::notice title=Native visible window::'+repr((w.window_text(),w.class_name(),w.rectangle())))
            if w.is_visible():w.capture_as_image().save(artifacts/f'native-failure-{index}.png')
        with (artifacts/'native-controls.txt').open('w',encoding='utf-8') as output:
            for w in windows:
                target=Desktop(backend='uia').window(handle=w.handle)
                for c in target.descendants():
                    entry=repr((c.element_info.control_type,c.element_info.automation_id,c.window_text()))
                    output.write(entry+'\n')
                    if c.element_info.control_type in ('Window','Text'):print('::notice title=Native control::'+entry[:500])
    except Exception as debug_error:print('Diagnostic failure:',type(debug_error).__name__)
    raise
finally:
    stop.set()
    if process and process.poll() is None:
        try:
            p=psutil.Process(process.pid)
            for child in p.children(recursive=True):child.kill()
            p.kill()
        except psutil.NoSuchProcess:pass
