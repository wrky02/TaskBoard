"""Check the actual Edge app window's small and large icons in an isolated profile."""
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
user = c.WinDLL('user32', use_last_error=True)
gdi = c.WinDLL('gdi32', use_last_error=True)
CALLBACK = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user.EnumWindows.argtypes = [CALLBACK, w.LPARAM]
user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
user.IsWindowVisible.argtypes = [w.HWND]
user.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, c.POINTER(c.c_size_t)]
user.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, c.c_int, c.c_int, w.UINT]
user.LoadImageW.restype = w.HANDLE
user.DestroyIcon.argtypes = [w.HICON]
user.DrawIconEx.argtypes = [w.HDC, c.c_int, c.c_int, w.HICON, c.c_int, c.c_int, w.UINT, w.HBRUSH, w.UINT]
gdi.CreateCompatibleDC.argtypes = [w.HDC]
gdi.CreateCompatibleDC.restype = w.HDC
gdi.CreateDIBSection.argtypes = [w.HDC, c.c_void_p, w.UINT, c.POINTER(c.c_void_p), w.HANDLE, w.DWORD]
gdi.CreateDIBSection.restype = w.HBITMAP
gdi.SelectObject.argtypes = [w.HDC, w.HANDLE]
gdi.SelectObject.restype = w.HANDLE
gdi.DeleteObject.argtypes = [w.HANDLE]
gdi.DeleteDC.argtypes = [w.HDC]

class BitmapInfo(c.Structure):
    _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG),
                ('planes', w.WORD), ('bits', w.WORD), ('compression', w.DWORD),
                ('image_size', w.DWORD), ('xppm', w.LONG), ('yppm', w.LONG),
                ('used', w.DWORD), ('important', w.DWORD)]

def pixels(icon, size):
    dc = gdi.CreateCompatibleDC(None)
    info = BitmapInfo(c.sizeof(BitmapInfo), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    address = c.c_void_p()
    bitmap = gdi.CreateDIBSection(dc, c.byref(info), 0, c.byref(address), None, 0)
    if not bitmap: raise c.WinError(c.get_last_error())
    old = gdi.SelectObject(dc, bitmap)
    try:
        c.memset(address, 0, size * size * 4)
        if not user.DrawIconEx(dc, 0, 0, icon, size, size, 0, None, 3):
            raise c.WinError(c.get_last_error())
        gdi.GdiFlush()
        return c.string_at(address, size * size * 4)
    finally:
        gdi.SelectObject(dc, old); gdi.DeleteObject(bitmap); gdi.DeleteDC(dc)

def window_icons():
    found = []
    @CALLBACK
    def visit(hwnd, _):
        title = c.create_unicode_buffer(512)
        user.GetWindowTextW(hwnd, title, len(title))
        if user.IsWindowVisible(hwnd) and '任务看板系统' in title.value:
            icons = []
            for kind in (0, 1):
                result = c.c_size_t()
                user.SendMessageTimeoutW(hwnd, 0x7f, kind, 0, 2, 1000, c.byref(result))
                icons.append(result.value)
            found.append(icons)
        return True
    user.EnumWindows(visit, 0)
    return found

edge = next((Path(base) / 'Microsoft/Edge/Application/msedge.exe'
             for base in (os.environ.get('PROGRAMFILES(X86)'), os.environ.get('PROGRAMFILES'), os.environ.get('LOCALAPPDATA'))
             if base and (Path(base) / 'Microsoft/Edge/Application/msedge.exe').exists()), None)
if not edge: raise RuntimeError('Microsoft Edge is unavailable for native icon verification')
with tempfile.TemporaryDirectory(prefix='taskboard-native-icons-', ignore_cleanup_errors=True) as tmp:
    root = Path(tmp)
    service = subprocess.Popen([sys.executable, str(ROOT/'source/taskboard/launcher.py'), '--no-browser', '--data-root', str(root/'data')])
    browser = None
    references = []
    try:
        for _ in range(100):
            info_path = root/'data/.instance.json'
            if info_path.exists(): break
            time.sleep(.1)
        info = json.loads(info_path.read_text())
        # Never print the startup capability or browser command line.
        url = f"http://127.0.0.1:{info['port']}/#{info['capability']}"
        browser = subprocess.Popen([str(edge), '--app='+url, '--user-data-dir='+str(root/'edge'), '--no-first-run', '--no-default-browser-check'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for size in (16, 32):
            handle = user.LoadImageW(None, str(ROOT/'source/taskboard/taskboard.ico'), 1, size, size, 16)
            if not handle: raise c.WinError(c.get_last_error())
            references.append(handle)
        expected = [pixels(handle, size) for handle, size in zip(references, (16, 32))]
        differences = []
        for _ in range(120):
            for handles in window_icons():
                if not all(handles): continue
                actual = [pixels(handle, size) for handle, size in zip(handles, (16, 32))]
                differences = [round(sum(abs(a-b) for a,b in zip(x,y))/len(x), 2) for x,y in zip(expected, actual)]
                if max(differences) <= 12:
                    print(f'::notice title=Native window icons::PASS: small and large Edge app icons match TaskBoard ICO; mean pixel differences {differences}')
                    break
            else:
                time.sleep(.5); continue
            break
        else:
            raise AssertionError(f'Native app icons did not match TaskBoard ICO; differences={differences}')
    finally:
        for handle in references: user.DestroyIcon(handle)
        if browser and browser.poll() is None:
            subprocess.run(['taskkill', '/PID', str(browser.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            browser.wait(timeout=10)
        service.terminate(); service.wait(timeout=10)
