"""Drive installed WPF controls through Windows UI Automation.
Also monitor the full process tree for browser processes and TCP listeners.
"""
import io
import base64
import json
from datetime import date,timedelta
import win32clipboard
import win32gui
import win32con
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
process=None;stop=threading.Event();violations=[];seen=set();completed=[]

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

def mainwindow():return Desktop(backend='uia').window(process=process.pid,auto_id='mainWindow',control_type='Window')
def control(win,id):
    result=win.child_window(auto_id=id);result.wait('exists enabled',timeout=30);return result.wrapper_object()
def click(win,id):control(win,id).invoke()
def text(win,id,value):control(win,id).set_edit_text(value)
def close_window(win):
    handle=win.handle
    win32gui.PostMessage(handle,win32con.WM_CLOSE,0,0)
    until(lambda:not win32gui.IsWindow(handle))
def editor():
    # The pane's root is a layout container, which some Windows UIA versions
    # omit from the control view. Its actual editable controls remain children
    # of the main window and have stable automation IDs of their own.
    return mainwindow()
def editor_closed():
    return not mainwindow().child_window(auto_id='editorTitle').exists(timeout=.2)
def message_button(button_id):
    message=Desktop(backend='win32').window(process=process.pid,class_name='#32770')
    message.wait('visible',timeout=15)
    # Win32 IDYES=6 is stable across runner display languages.
    handle=message.handle
    try:button=message.child_window(control_id=button_id).wrapper_object()
    except Exception:
        buttons=message.children(class_name='Button')
        print('::notice title=Native modal buttons::'+repr([(b.control_id(),b.window_text()) for b in buttons]),flush=True)
        if not buttons:raise AssertionError('Native modal contains no actionable buttons')
        button=buttons[0]
    win32gui.SendMessage(button.handle,win32con.BM_CLICK,0,0)
    # A follow-up MessageBox can reuse the same title/class, so wait for this
    # exact HWND to close instead of querying the selector again.
    until(lambda:not win32gui.IsWindow(handle))
def message_choice(yes=True):message_button(6 if yes else 7)
def record(name):
    completed.append(name);print('PASS '+name,flush=True)
def modal_click(win,id):
    errors=[]
    def run():
        try:click(win,id)
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=run,daemon=True);thread.start()
    return thread,errors
def modal_done(result):
    thread,errors=result;thread.join(timeout=10)
    assert not thread.is_alive(),'Modal action did not finish'
    assert not errors,errors
def expand_section(win,title):
    section=until(lambda:next((c for c in win.descendants() if c.window_text()==title and c.element_info.control_type=='Group'),None))
    section.iface_expand_collapse.Expand()
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
    control(win,'loginConfirm').type_keys('{ENTER}');control(win,'quickTitle');record('first-run password Enter')
    text(win,'quickTitle','Native quick task');control(win,'quickTitle').type_keys('{ENTER}')
    until(lambda:db_scalar('SELECT COUNT(*) FROM tasks')==1);record('quick task Enter')
    click(win,'tags');tags=dialog('标签管理');text(tags,'tagName','NativeTag');control(tags,'tagName').type_keys('{ENTER}')
    until(lambda:db_scalar("SELECT COUNT(*) FROM tags WHERE name='NativeTag'")==1);close_window(tags);record('tag Enter')
    click(win,'newTask');edit=editor();text(edit,'editorTitle','Recovered native draft');text(edit,'editorNote','Native text without browser')
    edit.child_window(title='明天',control_type='Button').invoke()
    stream=io.BytesIO();Image.new('RGB',(12,12),'purple').save(stream,format='BMP')
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard();win32clipboard.SetClipboardData(win32clipboard.CF_DIB,stream.getvalue()[14:])
    finally:win32clipboard.CloseClipboard()
    edit.child_window(title='粘贴截图',control_type='Button').invoke()
    click(edit,'closeEditor');until(lambda:db_scalar('SELECT COUNT(*) FROM drafts')==1);until(editor_closed)
    click(win,'drafts');drafts=dialog('草稿恢复');drafts.child_window(title='恢复编辑',control_type='Button').invoke()
    edit=editor();assert control(edit,'editorTitle').window_text()=='Recovered native draft'
    control(edit,'editorPriority').select('紧急');text(edit,'checkInput','Verify native controls');click(edit,'addCheck');click(edit,'saveTask')
    until(lambda:db_scalar('SELECT COUNT(*) FROM tasks')==2);assert db_scalar('SELECT COUNT(*) FROM drafts')==0
    with sqlite3.connect(root/'tasks.sqlite3') as db:
        row=db.execute("SELECT data FROM task_options WHERE task_id=(SELECT id FROM tasks WHERE title='Recovered native draft')").fetchone();assert json.loads(row[0])['priority']==0
        assert db.execute("SELECT due_date FROM tasks WHERE title='Recovered native draft'").fetchone()[0]==(date.today()+timedelta(days=1)).isoformat()
        assert db.execute('SELECT COUNT(*) FROM checklist_items').fetchone()[0]==1
        attachment=db.execute('SELECT stored_name FROM attachments').fetchone()[0]
        assert (root/'attachments'/attachment).read_bytes().startswith(b'\x89PNG')
    until(editor_closed);record('draft, priority, checklist, date and clipboard image recovery')
    # Closing an unchanged editor must not re-enter WPF's Closing event.
    click(win,'task-1');unchanged=editor();click(unchanged,'closeEditor');until(editor_closed);record('unchanged detail pane close')
    # Exercise lock/re-unlock using native password controls.
    click(win,'lock');control(win,'loginPassword').type_keys('native-test-password',with_spaces=True);control(win,'loginPassword').type_keys('{ENTER}');control(win,'quickTitle');record('unlock password Enter')
    # Search must change the displayed result rather than merely accepting text.
    text(win,'search','Recovered native draft');until(lambda:not win.child_window(auto_id='task-1').exists(timeout=.2));control(win,'task-2')
    text(win,'search','');control(win,'task-1');record('search results')
    # The same application name is reflected in the title bar and sidebar.
    click(win,'settings');settings=dialog('设置与备份')
    text(settings,'applicationNameInput','Native Pro QA');text(settings,'workspaceNameInput','QA Workspace');click(settings,'saveSettings');control(settings,'saveSettings');close_window(settings)
    until(lambda:'Native Pro QA' in win.window_text())
    assert control(win,'applicationName').window_text()=='Native Pro QA'
    assert control(win,'workspaceName').window_text()=='QA Workspace';record('custom application and workspace names')
    # Clearing activity retains tasks; cancellation is checked before confirming.
    before_tasks=db_scalar('SELECT COUNT(*) FROM tasks');before_activity=db_scalar('SELECT COUNT(*) FROM activity');assert before_activity>0
    click(win,'activity');logs=dialog('操作记录');pending=modal_click(logs,'clearActivity');message_choice(False);modal_done(pending)
    assert db_scalar('SELECT COUNT(*) FROM activity')==before_activity
    pending=modal_click(logs,'clearActivity');message_choice();modal_done(pending)
    until(lambda:db_scalar('SELECT COUNT(*) FROM activity')<=1)
    assert db_scalar('SELECT COUNT(*) FROM tasks')==before_tasks;close_window(logs);record('clear activity without deleting tasks')
    # Create a project and a custom workflow column using the real controls.
    click(win,'projects');projects=dialog('项目管理');text(projects,'projectName','Native QA Project');control(projects,'projectName').type_keys('{ENTER}')
    until(lambda:db_scalar("SELECT COUNT(*) FROM pro_projects WHERE name='Native QA Project'")==1)
    state_buttons=[button for button in projects.descendants(control_type='Button') if button.element_info.automation_id=='projectStates']
    assert state_buttons;state_buttons[-1].invoke();states=dialog('项目状态列')
    text(states,'stateName','QA Review');control(states,'stateKind').select('进行中');click(states,'createState')
    until(lambda:db_scalar("SELECT COUNT(*) FROM pro_states WHERE name='QA Review'")==1);close_window(states);close_window(projects)
    click(win,'task-1');edit=editor();expand_section(edit,'项目与任务结构');control(edit,'editorProject').select('Native QA Project');control(edit,'editorProjectState').select('QA Review');click(edit,'saveTask');until(editor_closed)
    assert db_scalar("SELECT COUNT(*) FROM pro_tasks p JOIN pro_projects j ON p.project_id=j.id JOIN pro_states s ON p.state_id=s.id WHERE p.task_id=1 AND j.name='Native QA Project' AND s.name='QA Review'")==1
    # Child tasks have a real parent relation, their own title and due date.
    click(win,'task-1');edit=editor();expand_section(edit,'子任务');click(edit,'addSubtask');edit=editor();until(lambda:control(edit,'editorTitle').window_text()=='')
    text(edit,'editorTitle','Native child task');edit.child_window(title='明天',control_type='Button').invoke();click(edit,'saveTask');until(editor_closed)
    until(lambda:db_scalar("SELECT COUNT(*) FROM tasks t JOIN pro_tasks p ON p.task_id=t.id WHERE t.title='Native child task' AND p.parent_id=1")==1)
    assert db_scalar('SELECT COUNT(*) FROM tasks')==3;record('project, custom workflow and independent child task')
    # A historical version restores task text and its attachment without rolling
    # the entire workspace back or destroying the new project and child task.
    click(win,'task-2');edit=editor();text(edit,'editorNote','Temporary edit for version restoration');click(edit,'saveTask');until(editor_closed)
    until(lambda:db_scalar("SELECT body FROM tasks WHERE id=2")=='Temporary edit for version restoration')
    version=db_scalar('SELECT MIN(id) FROM task_history WHERE task_id=2')
    click(win,'task-2');edit=editor();expand_section(edit,'正式版本历史');click(edit,'historyRefresh')
    pending=modal_click(edit,'historyRestore-'+str(version));message_choice();modal_done(pending)
    until(lambda:db_scalar('SELECT body FROM tasks WHERE id=2')=='Native text without browser')
    click(editor(),'closeEditor');until(editor_closed)
    assert db_scalar('SELECT COUNT(*) FROM tasks')==3;record('restore an individual task version')
    # Create a verified recovery point, add a task, preview and restore the point.
    recovery_target=root.parent/'native-qa-backups'
    recovery_target.mkdir(parents=True,exist_ok=True)
    click(win,'recoveryCenter');center=dialog('恢复中心');text(center,'recoveryTarget',str(recovery_target));click(center,'saveRecoveryConfig');control(center,'saveRecoveryConfig')
    click(center,'createRecoveryPoint');until(lambda:list(recovery_target.glob('*.zip')),timeout=60);close_window(center)
    text(win,'quickTitle','Restore rollback candidate');control(win,'quickTitle').type_keys('{ENTER}');until(lambda:db_scalar('SELECT COUNT(*) FROM tasks')==4)
    click(win,'recoveryCenter');center=dialog('恢复中心');control(center,'previewRecovery')
    pending=modal_click(center,'previewRecovery');message_choice();message_button(1);modal_done(pending)
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        restored_count=db_scalar('SELECT COUNT(*) FROM tasks')
        if restored_count==3:break
        time.sleep(.25)
    if restored_count!=3:
        with sqlite3.connect(root/'tasks.sqlite3') as db:titles=[row[0] for row in db.execute('SELECT title FROM tasks ORDER BY id')]
        raise AssertionError(f'Backup restore left {restored_count} tasks; expected 3. Titles: {titles!r}')
    assert db_scalar("SELECT COUNT(*) FROM tasks WHERE title='Restore rollback candidate'")==0
    close_window(center);record('create, preview and restore a verified recovery point')
    # Real installed icon and WPF class, not Edge app mode.
    assert 'HwndWrapper' in Desktop(backend='win32').window(handle=win.handle).class_name()
    screenshot=win.wrapper_object().capture_as_image();screenshot.save(artifacts/'native-board.png')
    click(win,'settings');settings=dialog('设置与备份');control(settings,'closeBehavior').select('最小化到托盘');click(settings,'saveSettings');control(settings,'saveSettings');close_window(settings)
    main_handle=win.handle;win32gui.PostMessage(main_handle,win32con.WM_CLOSE,0,0);until(lambda:not win32gui.IsWindowVisible(main_handle))
    second=subprocess.run([str(exe),'--data-root',str(root)],timeout=15);assert second.returncode==0
    win=mainwindow();until(lambda:win.is_visible());assert process.poll() is None
    click(win,'settings');settings=dialog('设置与备份');control(settings,'closeBehavior').select('退出程序');click(settings,'saveSettings');control(settings,'saveSettings');close_window(settings)
    win32gui.PostMessage(win.handle,win32con.WM_CLOSE,0,0);process.wait(timeout=20);assert process.returncode==0
    record('tray close, second instance activation and exit')
    # Normal bounds and maximized state must survive a complete process restart.
    process=subprocess.Popen([str(exe),'--data-root',str(root)]);win=mainwindow();win.wait('visible',timeout=45)
    control(win,'loginPassword').type_keys('native-test-password{ENTER}',with_spaces=True);control(win,'quickTitle')
    win32gui.ShowWindow(win.handle,win32con.SW_MAXIMIZE);until(lambda:win32gui.IsZoomed(win.handle))
    win32gui.PostMessage(win.handle,win32con.WM_CLOSE,0,0);process.wait(timeout=20)
    process=subprocess.Popen([str(exe),'--data-root',str(root)]);win=mainwindow();win.wait('visible',timeout=45)
    until(lambda:win32gui.IsZoomed(win.handle))
    control(win,'loginPassword').type_keys('native-test-password{ENTER}',with_spaces=True);control(win,'quickTitle')
    assert 'Native Pro QA' in win.window_text();assert control(win,'workspaceName').window_text()=='QA Workspace'
    win32gui.PostMessage(win.handle,win32con.WM_CLOSE,0,0);process.wait(timeout=20);record('maximized window and custom names retained after restart')
    stop.set();assert not violations,violations;assert 'taskboard.engine.exe' in seen,seen
    assert db_scalar('SELECT COUNT(*) FROM tasks')==3
    report=dict(passed=True,processes=sorted(seen),browser_processes=False,tcp_listeners=False,tests=completed)
    (artifacts/'native-acceptance.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('::notice title=Native WPF acceptance::PASS: installed WPF controls, password Enter, custom names, retained maximization, activity clearing, search, task/draft/checklist/image regression; no browser process or TCP listener')
except Exception:
    failure=traceback.format_exc()
    print(failure,flush=True)
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
    print('Native acceptance failure:\n'+failure,flush=True)
    raise
finally:
    stop.set()
    if process and process.poll() is None:
        try:
            p=psutil.Process(process.pid)
            for child in p.children(recursive=True):child.kill()
            p.kill()
        except psutil.NoSuchProcess:pass
