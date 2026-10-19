"""Capture real Windows controls using synthetic demo data only.
The preview branch must never contain a user data directory or private task data.
"""
from datetime import date, timedelta
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import win32api
import win32con
import win32gui
import psutil
from pywinauto import Desktop

exe = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
out = Path('artifacts/visual-review'); out.mkdir(parents=True, exist_ok=True)
assert not root.exists(), 'Visual fixture requires a fresh isolated data directory'
password = 'synthetic-preview-only'
engine = subprocess.Popen([str(exe.parent/'engine/TaskBoard.Engine.exe'), '--install-root', str(exe.parent), '--data-root', str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8')
seq = 0

def call(method, **params):
    global seq
    seq += 1
    engine.stdin.write(json.dumps(dict(id=seq, method=method, params=params), ensure_ascii=False)+'\n'); engine.stdin.flush()
    result = json.loads(engine.stdout.readline())
    assert not result.get('error'), result
    return result['result']

try:
    call('login', password=password, confirm=password)
    tags=[]
    for name,color in [('运营','purple'),('设计','blue'),('产品','green')]:
        state=call('tag',action='create',name=name)
        tid=next(t['id'] for t in state['tags'] if t['name']==name)
        call('tag',action='color',id=tid,color=color);tags.append(tid)
    fixtures=[
        ('新品上传阿里新店','完成 25 套新品上架，核对主图、规格与商品说明。','计划中',0,0,0),
        ('新品上传独立站','整理英文商品描述，补充尺寸和使用场景。','计划中',1,3,0),
        ('新老店数据整理','整理本周销售数据，记录高转化商品的共同特点。','计划中',2,5,0),
        ('新品图片制作','统一主图风格，完成详情页与移动端裁切。','进行中',1,1,1),
        ('塑料画册更新','更新产品目录与封面，完成印刷前的最后校对。','进行中',2,-2,1),
        ('工作区资料归档','历史文件已按月份整理，项目资料已完成备份。','完成',2,-1,2),
    ]
    for i,(title,note,status,priority,days,tag) in enumerate(fixtures):
        call('save',title=title,note=note,status=status,due=(date.today()+timedelta(days=days)).isoformat(),tags=[tags[tag]],options=dict(priority=priority,today=date.today().isoformat() if i==0 else '',repeat='weekly' if i==2 else 'off',repeat_basis='due',reminder='off'),checklist=[dict(label=label,done=n<(3 if i==0 else 1)) for n,label in enumerate(['确认商品信息','准备图片素材','核对规格与定价','发布前检查','完成发布'])] if i in (0,3) else [])
    call('view',name='高优先级 · 进行中',filters=dict(priority=1,status='进行中'))
    call('settings',close_behavior='exit',system_name='我的工作空间',user_name='我')
finally:
    engine.stdin.close();engine.wait(timeout=20)

# Use the largest supported desktop mode; don't invent a rendered mockup.
try:
    modes=[];index=0
    while True:
        try:
            m=win32api.EnumDisplaySettings(None,index);index+=1
            if m.PelsWidth>=1440 and m.PelsHeight>=900:modes.append(m)
        except Exception:break
    if modes:
        mode=min(modes,key=lambda m:abs(m.PelsWidth-1600)+abs(m.PelsHeight-1000))
        win32api.ChangeDisplaySettings(mode,0)
except Exception:pass

process=subprocess.Popen([str(exe),'--data-root',str(root)])
def until(fn,timeout=30):
    end=time.monotonic()+timeout;last=None
    while time.monotonic()<end:
        try:
            value=fn()
            if value:return value
        except Exception as ex:last=ex
        time.sleep(.25)
    raise AssertionError(str(last))
def control(win,id):
    c=win.child_window(auto_id=id);c.wait('exists enabled',timeout=30);return c.wrapper_object()
def dialog(title):
    w=Desktop(backend='win32').window(process=process.pid,title=title);w.wait('visible',timeout=30)
    return Desktop(backend='uia').window(handle=w.handle)
def capture(win,name):
    time.sleep(1)
    win.wrapper_object().capture_as_image().save(out/(name+'.png'))

try:
    win=Desktop(backend='uia').window(process=process.pid,title_re='任务看板系统.*',control_type='Window');win.wait('visible',timeout=45)
    control(win,'loginPassword').type_keys(password);control(win,'login').invoke();control(win,'quickTitle')
    capture(win,'board')
    control(win,'batchSelect').invoke();capture(win,'batch-selection');control(win,'batchSelect').invoke()
    control(win,'more-1').invoke()
    menu=Desktop(backend='uia').window(control_type='Menu',process=process.pid)
    # UIA menu nesting differs between Windows builds; locate by descendants.
    today=until(lambda:next((x for w in Desktop(backend='uia').windows(process=process.pid) for x in w.descendants(control_type='MenuItem') if x.window_text()=='移出今日计划'),None))
    capture(win,'task-menu');today.invoke();control(win,'task-1')
    control(win,'task-1').invoke();edit=dialog('编辑任务');capture(edit,'task-editor')
    control(edit,'editorDue');edit.child_window(auto_id='PART_Button',control_type='Button').invoke();capture(edit,'date-picker')
    # Close popup without changing selected date.
    edit.type_keys('{ESC}')
    edit_handle=edit.handle;control(edit,'closeEditor').invoke();until(lambda:not win32gui.IsWindow(edit_handle))
    # Verify navigation selection and an empty filtered state.
    control(win,'nav我的今天').invoke();capture(win,'my-day')
    control(win,'nav看板').invoke();control(win,'search').set_edit_text('no-matching-demo-task');capture(win,'empty-search');control(win,'search').set_edit_text('')
    # Narrow window validates wrapping and summary collapse on a smaller screen.
    win32gui.SetWindowPos(win.handle,0,0,0,1024,700,win32con.SWP_NOZORDER)
    capture(win,'compact-board')
    win32gui.PostMessage(win.handle,win32con.WM_CLOSE,0,0);process.wait(timeout=20)
    (out/'README.md').write_text('# Native UI review · 3.0.1\n\nActual Windows WPF captures. All task names and data are synthetic fixtures from scripts/capture-native.py. No user database, credentials, or attachments are published.\n',encoding='utf-8')
    print('::notice title=Native visual review::PASS: real WPF board, editor, calendar popup, context menu, batch mode, empty search and compact layout captured with synthetic data')
except Exception:
    print(traceback.format_exc())
    raise
finally:
    if process.poll() is None:
        try:
            p=psutil.Process(process.pid)
            for child in p.children(recursive=True):child.kill()
            p.kill()
        except psutil.NoSuchProcess:pass
