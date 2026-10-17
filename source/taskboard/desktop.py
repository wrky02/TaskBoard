"""Windows shell integration around an external Edge window (no embedded engine)."""
import ctypes as c
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
import tempfile
import storage

class WindowsDesktop:
    def __init__(self,server,root):
        import psutil
        from PIL import Image
        import pystray
        self.psutil=psutil;self.server=server;self.root=root;self.stopping=False
        self.processes=[];self.handles=[];self.was_visible=False;self.launching=False
        self.guard=threading.Lock();self.last_seen=time.monotonic()
        self.edge=next((Path(base)/'Microsoft/Edge/Application/msedge.exe' for base in (os.environ.get('PROGRAMFILES(X86)'),os.environ.get('PROGRAMFILES'),os.environ.get('LOCALAPPDATA')) if base and (Path(base)/'Microsoft/Edge/Application/msedge.exe').is_file()),None)
        if not self.edge:raise RuntimeError('Windows 托盘模式需要 Microsoft Edge，请先安装 Edge。')
        profile=Path(os.environ.get('LOCALAPPDATA',str(root.parent)))/'TaskBoard'/'Browser'/hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
        profile.mkdir(parents=True,exist_ok=True)
        self.profile_temp=tempfile.TemporaryDirectory(prefix='session-',dir=profile,ignore_cleanup_errors=True)
        self.profile=Path(self.profile_temp.name)
        self.user=c.WinDLL('user32',use_last_error=True)
        self.user.GetWindowThreadProcessId.argtypes=[w.HWND,c.POINTER(w.DWORD)]
        self.user.IsWindowVisible.argtypes=[w.HWND]
        self.user.ShowWindow.argtypes=[w.HWND,c.c_int]
        self.user.SetForegroundWindow.argtypes=[w.HWND]
        self.user.SendMessageTimeoutW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM,w.UINT,w.UINT,c.POINTER(c.c_size_t)]
        self.callback=c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
        self.user.EnumWindows.argtypes=[self.callback,w.LPARAM]
        self.user.LoadImageW.argtypes=[w.HINSTANCE,w.LPCWSTR,w.UINT,c.c_int,c.c_int,w.UINT];self.user.LoadImageW.restype=w.HANDLE
        self.user.DestroyIcon.argtypes=[w.HICON]
        self.iconfile=Path(__file__).resolve().parent/'taskboard.ico'
        self.icons=[self.user.LoadImageW(None,str(self.iconfile),1,size,size,16) for size in (16,32)]
        self.tray=pystray.Icon('TaskBoard',Image.open(self.iconfile),'任务看板系统',pystray.Menu(pystray.MenuItem('打开工作区',lambda *_:self.open(),default=True),pystray.MenuItem('退出程序',lambda *_:self.quit())))
    def start(self):
        threading.Thread(target=self.tray.run,daemon=True).start()
        threading.Thread(target=self.monitor,daemon=True).start();self.open()
    def windows(self):
        processes=[]
        for process in self.processes:
            try:
                if process.is_running():processes.extend([process,*process.children(recursive=True)])
            except self.psutil.Error:pass
        self.processes=list({(p.pid,p.create_time()):p for p in processes if p.is_running()}.values())
        pids={p.pid for p in self.processes};handles=[]
        @self.callback
        def visit(hwnd,_):
            pid=w.DWORD();self.user.GetWindowThreadProcessId(hwnd,c.byref(pid))
            if pid.value in pids and self.user.IsWindowVisible(hwnd):handles.append(hwnd)
            return True
        self.user.EnumWindows(visit,0);return handles
    def open(self):
        if self.stopping:return
        with self.guard:
            handles=self.windows()
            if handles:
                for hwnd in handles:self.user.ShowWindow(hwnd,9);self.user.SetForegroundWindow(hwnd)
                return
            if self.launching:return
            self.launching=True;self.last_seen=time.monotonic()
            process=subprocess.Popen([str(self.edge),'--app='+self.server.launch_url,'--start-maximized','--user-data-dir='+str(self.profile),'--no-first-run','--no-default-browser-check','--disable-background-mode'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            self.processes.append(self.psutil.Process(process.pid))
    def monitor(self):
        while not self.stopping:
            try:
                with self.guard:handles=self.windows()
                if handles:
                    self.was_visible=True;self.launching=False;self.last_seen=time.monotonic()
                    for hwnd in handles:
                        for kind,icon in enumerate(self.icons):
                            if icon:
                                result=c.c_size_t();self.user.SendMessageTimeoutW(hwnd,0x80,kind,icon,2,500,c.byref(result))
                elif self.was_visible and time.monotonic()-self.last_seen>1.5:
                    self.was_visible=False;self.launching=False;self.on_closed()
                elif self.launching and time.monotonic()-self.last_seen>30:
                    self.launching=False
                    self.tray.notify('窗口启动失败，可从托盘重试。','任务看板系统')
            except (OSError,self.psutil.Error):pass
            time.sleep(.5)
    def on_closed(self):
        if self.stopping:return
        db=storage.connect()
        try:behavior=storage.get_setting(db,'close_behavior','ask')
        finally:db.close()
        if behavior=='ask':
            answer=self.user.MessageBoxW(None,'窗口已关闭。\n\n是：退出程序\n否：保留在系统托盘\n取消：重新打开工作区','任务看板系统',0x23)
            if self.stopping:return
            if answer==2:self.open();return
            behavior='exit' if answer==6 else 'tray'
            remember=self.user.MessageBoxW(None,'记住这个选择？可在“数据与设置”中修改。','关闭偏好',0x24)==6
            if remember:
                db=storage.connect()
                try:
                    with db:db.execute("INSERT OR REPLACE INTO settings VALUES ('close_behavior',?)",(behavior,))
                finally:db.close()
        if behavior=='exit':self.quit()
        else:self.tray.notify('已保留在系统托盘，双击图标重新打开。应用内提醒需打开并登录工作区。','任务看板系统')
    def quit(self):
        if self.stopping:return
        self.stopping=True
        threading.Thread(target=self.server.shutdown,daemon=True).start()
    def stop(self):
        self.stopping=True
        self.tray.stop()
        # Only processes belonging to this dedicated app profile are stopped.
        for process in reversed(self.processes):
            try:
                if process.is_running():process.terminate()
            except self.psutil.Error:pass
        _,alive=self.psutil.wait_procs(self.processes,timeout=3)
        for process in alive:
            try:process.kill()
            except self.psutil.Error:pass
        self.psutil.wait_procs(alive,timeout=3)
        self.profile_temp.cleanup()
        for icon in self.icons:
            if icon:self.user.DestroyIcon(icon)
