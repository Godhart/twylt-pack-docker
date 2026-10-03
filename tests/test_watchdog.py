"""Real guardian subprocess tests against a file-backed fake Docker transport."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from types import SimpleNamespace
import pytest
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'src'))
import runtime as r

@pytest.fixture
def fake(tmp_path,monkeypatch):
    package=tmp_path/'fake'/'docker';package.mkdir(parents=True)
    (package/'errors.py').write_text('class NotFound(Exception): pass\n')
    (package/'__init__.py').write_text('''import json,os
from pathlib import Path
from .errors import NotFound
class C:
    def __init__(self):
        self.p=Path(os.environ['FAKE_DOCKER_STATE'])
        if not self.p.exists():raise NotFound()
        self.attrs=json.loads(self.p.read_text())
    def kill(self):
        self.attrs['State']['Running']=False
        self.p.write_text(json.dumps(self.attrs))
    def remove(self,force,v):self.p.unlink(missing_ok=True)
class Containers:
    def get(self,identifier):return C()
class DockerClient:
    def __init__(self,**kwargs):self.containers=Containers()
    def close(self):pass
''')
    root=tmp_path/'root';root.mkdir()
    state=tmp_path/'state.json'
    state.write_text(json.dumps({'Config':{'Labels':{r.OWNER_KEY:r.OWNER,r.TOKEN_KEY:'token'}},'State':{'Running':True}}))
    monkeypatch.setenv('TWYLT_WORKSPACE_ROOT',str(root))
    monkeypatch.setenv('TWYLT_INCIDENT_LOG',str(tmp_path/'audit.jsonl'))
    monkeypatch.setenv('FAKE_DOCKER_STATE',str(state))
    return package.parent,state


def test_watchdog_can_be_cancelled(fake,monkeypatch):
    fakepath,state=fake;monkeypatch.setenv('PYTHONPATH',str(fakepath))
    guard=r.Watchdog(SimpleNamespace(host='unix:///fake'),SimpleNamespace(api=SimpleNamespace(_version='1.45')),SimpleNamespace(id='test'),time.time()+30,'token')
    guard.cancel()
    assert guard.process.poll() is not None and json.loads(state.read_text())['State']['Running']


def test_watchdog_survives_caller_sigkill(fake):
    fakepath,state=fake
    code='''import os,sys,time
sys.path.insert(0,sys.argv[1])
import runtime as r
from types import SimpleNamespace
os.environ['PYTHONPATH']=sys.argv[2]
g=r.Watchdog(SimpleNamespace(host='unix:///fake'),SimpleNamespace(api=SimpleNamespace(_version='1.45')),SimpleNamespace(id='test'),time.time()+0.3,'token')
print(g.process.pid,flush=True)
time.sleep(60)
'''
    caller=subprocess.Popen([sys.executable,'-c',code,str(BASE/'src'),str(fakepath)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    ready,_,_=select.select([caller.stdout],[],[],10)
    assert ready
    guardian_pid=int(caller.stdout.readline())
    caller.kill();caller.communicate(timeout=5)
    try:
        deadline=time.monotonic()+5
        while state.exists() and json.loads(state.read_text())['State']['Running'] and time.monotonic()<deadline:time.sleep(.05)
        assert state.exists() and not json.loads(state.read_text())['State']['Running']
        deadline=time.monotonic()+18
        while state.exists() and time.monotonic()<deadline:time.sleep(.1)
        assert not state.exists()
    finally:
        try:os.kill(guardian_pid,15)
        except ProcessLookupError:pass
