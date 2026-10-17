"""Run the frozen service on a clean directory and exercise login/save/shutdown."""
import json
import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor, Request
with tempfile.TemporaryDirectory(prefix='taskboard-package-') as tmp:
    root = Path(sys.argv[2]).resolve() if len(sys.argv)>2 else Path(tmp)
    root.mkdir(parents=True,exist_ok=True)
    p=subprocess.Popen([str(Path(sys.argv[1]).resolve()),'--no-browser','--data-root',str(root)])
    try:
        info=root/'.instance.json'
        for _ in range(300):
            if info.exists():break
            if p.poll() is not None:raise RuntimeError('Packaged process exited before readiness')
            time.sleep(.1)
        data=json.loads(info.read_text());origin=f"http://127.0.0.1:{data['port']}"
        client=build_opener(HTTPCookieProcessor(CookieJar()))
        def post(path,item):
            with client.open(Request(origin+path,data=json.dumps(item).encode(),headers={'Content-Type':'application/json','Origin':origin,'X-TaskBoard':data['capability']})) as r:return json.load(r)
        with client.open(origin+'/') as r:assert b'TASKBOARD' in r.read()
        with client.open(origin+'/favicon.ico') as r:
            assert r.headers.get_content_type() == 'image/x-icon'
            assert r.read()[:4] == b'\x00\x00\x01\x00'
        with client.open(origin+'/date-picker.js') as r:assert b'renderDatePicker' in r.read()
        post('/api/login',{'password':'smokepass123','confirm':'smokepass123'})
        state=post('/api/task/save',{'title':'打包验收任务','status':'计划中','new_files':[{'name':'smoke.txt','data':base64.b64encode(b'smoke attachment').decode()}]})
        assert state['tasks'][0]['title']=='打包验收任务'
        assert len(state['tasks'][0]['files'])==1
        post('/api/shutdown',{});p.wait(timeout=10);assert p.returncode==0
        print('PASS frozen launcher/static assets/login/save/shutdown')
    finally:
        if p.poll() is None:p.terminate();p.wait(timeout=10)
