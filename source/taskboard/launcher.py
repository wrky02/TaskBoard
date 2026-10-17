"""Standalone launcher and service lifecycle. No Qt or embedded browser."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser
import storage as store
import features
from server import TaskServer


def message(title, text, confirm=False):
    if os.name == 'nt':
        return ctypes.windll.user32.MessageBoxW(None, text, title, 0x24 if confirm else 0x10) == 6
    print(title + ': ' + text, file=sys.stderr)
    return False


def open_browser(url):
    # Edge app mode uses the installed system browser in its own window, with
    # standard taskbar/window controls, without bundling any browser engine.
    if os.name == 'nt':
        for base in [os.environ.get('PROGRAMFILES(X86)'), os.environ.get('PROGRAMFILES'), os.environ.get('LOCALAPPDATA')]:
            if not base: continue
            edge = Path(base) / 'Microsoft/Edge/Application/msedge.exe'
            if edge.is_file():
                subprocess.Popen([str(edge), '--app=' + url, '--start-maximized'])
                return
    webbrowser.open(url)


from instance_lock import InstanceLock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if args.data_root: os.environ['TASKBOARD_DATA_ROOT'] = str(args.data_root.resolve())
    install_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    target = install_dir / 'Data'
    root = store.resolve_root() if os.environ.get('TASKBOARD_DATA_ROOT') or not store.writable(target) else target
    root.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(root)
    info_file = root / '.instance.json'
    if not lock.acquire():
        for _ in range(30):
            try:
                info = json.loads(info_file.read_text())
                port = int(info['port'])
                origin = f'http://127.0.0.1:{port}'
                with urllib.request.urlopen(origin + '/api/status', timeout=1) as response:
                    if response.status == 200:
                        if not args.no_browser:
                            req=urllib.request.Request(origin+'/api/window',data=b'{}',headers={'Origin':origin,'X-TaskBoard':info['capability'],'Content-Type':'application/json'})
                            with urllib.request.urlopen(req,timeout=5):pass
                        return 0
            except (OSError, ValueError, KeyError): time.sleep(0.1)
        message('任务看板系统', '另一个实例正在启动或无响应，请稍后重试。')
        return 1
    server = None
    try:
        location = store.prepare_data_root(target)
        if location['status'] == 'fallback' and not message('数据保存位置', '安装目录不可写，是否改用以下位置？\n' + location['path'], True): return 1
        # Startup migrations complete before the browser is opened.
        db = store.connect()
        try:
            try:
                features.auto_backup(db)
                with db:db.execute("DELETE FROM settings WHERE key='backup_error'")
            except (OSError,ValueError,__import__('zipfile').BadZipFile) as exc:
                with db:db.execute("INSERT OR REPLACE INTO settings VALUES ('backup_error',?)",(str(exc),))
        finally:db.close()
        server = TaskServer(('127.0.0.1', args.port))
        info_file.write_text(json.dumps({'port': server.server_port, 'capability': server.capability}))
        if os.name != 'nt': info_file.chmod(0o600)
        if not args.no_browser:
            if os.name=='nt':
                from desktop import WindowsDesktop
                server.desktop=WindowsDesktop(server,root);server.desktop.start()
            else:open_browser(server.launch_url)
        server.serve_forever(poll_interval=0.2)
        return 0
    except (OSError, ValueError, RuntimeError, __import__('sqlite3').Error) as exc:
        message('无法启动任务看板', str(exc))
        return 1
    except KeyboardInterrupt: return 0
    finally:
        if server:
            if getattr(server,'desktop',None):server.desktop.stop()
            server.server_close()
        info_file.unlink(missing_ok=True)
        lock.close()


if __name__ == '__main__': sys.exit(main())
