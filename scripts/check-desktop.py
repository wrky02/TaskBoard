"""Windows acceptance: app window -> tray -> reopen -> close and exit."""
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from http.cookiejar import CookieJar
from urllib.request import build_opener,HTTPCookieProcessor,Request
import psutil
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'source/taskboard'))
import storage
user=c.WinDLL('user32',use_last_error=True)
callback=c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
user.EnumWindows.argtypes=[callback,w.LPARAM]
user.GetWindowThreadProcessId.argtypes=[w.HWND,c.POINTER(w.DWORD)]
user.IsWindowVisible.argtypes=[w.HWND]
user.PostMessageW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM]

def windows(process):
    try:pids={p.pid for p in psutil.Process(process.pid).children(recursive=True)}
    except psutil.Error:return []
    handles=[]
    @callback
    def visit(hwnd,_):
        pid=w.DWORD();user.GetWindowThreadProcessId(hwnd,c.byref(pid))
        if pid.value in pids and user.IsWindowVisible(hwnd):handles.append(hwnd)
        return True
    user.EnumWindows(visit,0);return handles

def wait_for(predicate,label,timeout=40):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        value=predicate()
        if value:return value
        time.sleep(.2)
    raise AssertionError(label)

with tempfile.TemporaryDirectory(prefix='TaskBoard-desktop-test-',ignore_cleanup_errors=True) as tmp:
    root=Path(tmp);os.environ['TASKBOARD_DATA_ROOT']=str(root/'Data')
    db=storage.connect()
    with db:db.execute("INSERT OR REPLACE INTO settings VALUES ('close_behavior','tray')")
    db.close()
    process=subprocess.Popen([sys.executable,str(ROOT/'source/taskboard/launcher.py'),'--data-root',str(root/'Data')])
    client=build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        wait_for(lambda:(root/'Data/.instance.json').exists(),'No launch metadata')
        info=json.loads((root/'Data/.instance.json').read_text());origin=f"http://127.0.0.1:{info['port']}"
        def post(path,data):
            request=Request(origin+path,data=json.dumps(data).encode(),headers={'Origin':origin,'X-TaskBoard':info['capability'],'Content-Type':'application/json'})
            with client.open(request,timeout=10) as response:return json.load(response)
        post('/api/login',{'password':'desktop-test-123','confirm':'desktop-test-123'})
        handles=wait_for(lambda:windows(process),'External Edge window did not open')
        time.sleep(2)
        for hwnd in handles:user.PostMessageW(hwnd,0x10,0,0)
        wait_for(lambda:not windows(process),'Window did not close')
        time.sleep(3);assert process.poll() is None,'Tray mode exited the service'
        post('/api/window',{})
        handles=wait_for(lambda:windows(process),'Tray reopen failed')
        post('/api/settings',{'system_name':'任务看板系统','user_name':'','reminder_mode':'off','close_behavior':'exit'})
        time.sleep(2)
        for hwnd in handles:user.PostMessageW(hwnd,0x10,0,0)
        process.wait(timeout=30);assert process.returncode==0
        print('::notice title=Desktop acceptance::PASS: independent Edge window, close-to-tray, authenticated reopen and close-to-exit')
    finally:
        if process.poll() is None:
            try:post('/api/shutdown',{});process.wait(timeout=15)
            except Exception:
                for child in psutil.Process(process.pid).children(recursive=True):
                    try:child.kill()
                    except psutil.Error:pass
                process.kill();process.wait(timeout=10)
