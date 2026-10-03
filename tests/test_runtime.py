import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
import requests
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'src'))
import runtime as r
from workspace import Workspace,WorkspaceDenied


def api_error(code):
    response=requests.Response();response.status_code=code
    return r.APIError('test error',response=response)

class FakeContainer:
    def __init__(self,engine,name,kwargs):
        self.engine=engine;self.name=name;self.kwargs=kwargs;self.id='id-'+name+'-'+str(engine.serial)
        self.attrs={'Config':{'Labels':kwargs['labels']},'Mounts':[{'Type':'bind'} for _ in kwargs['mounts']],
                    'State':{'Running':False,'ExitCode':0,'OOMKilled':False}}
        self.killed=False;self.started=False;self.removed=False
    def reload(self): pass
    def start(self):
        if self.engine.fail_start:raise api_error(500)
        self.started=True;self.attrs['State']['Running']=True
    def wait(self,timeout):
        if self.engine.timeout:raise TimeoutError('elapsed')
        if self.engine.wait_gate is not None:self.engine.wait_gate.wait(3)
        self.attrs['State'].update(Running=False,ExitCode=self.engine.exitcode,OOMKilled=self.engine.oom)
        return {'StatusCode':self.engine.exitcode}
    def logs(self,stdout,stderr,stream,follow):
        return iter([self.engine.out if stdout else self.engine.err])
    def kill(self):
        self.killed=True;self.attrs['State'].update(Running=False,ExitCode=137)
    def remove(self,force,v):
        if self.engine.fail_remove:raise api_error(500)
        self.removed=True
        with self.engine.lock:self.engine.items.pop(self.name,None)

class FakeEngine:
    def __init__(self):
        self.lock=threading.Lock();self.items={};self.created=[];self.serial=0;self.peak=0
        self.fail_start=False;self.fail_remove=False;self.timeout=False;self.oom=False
        self.wait_gate=None;self.exitcode=0;self.out=b'hello\n';self.err=b'';self.missing=False
        self.image=SimpleNamespace(id='sha256:'+'a'*64,attrs={'Config':{'Volumes':{},'Cmd':['default'],'Entrypoint':None}})
        self.images=SimpleNamespace(get=self.image_get)
        self.containers=SimpleNamespace(create=self.create,list=self.list)
        self.api=SimpleNamespace(_version='1.45')
        self.info_data={'OSType':'linux','MemoryLimit':True,'SwapLimit':True,'CpuCfsQuota':True}
    def image_get(self,name):
        if self.missing:raise r.ImageNotFound('missing')
        return self.image
    def info(self):return self.info_data
    def close(self):pass
    def list(self,all,filters,ignore_removed=True):
        with self.lock:return list(self.items.values())
    def create(self,name,**kwargs):
        with self.lock:
            if name in self.items:raise api_error(409)
            self.serial+=1;c=FakeContainer(self,name,kwargs);self.items[name]=c
            self.created.append(c);self.peak=max(self.peak,len(self.items));return c

class Guardian:
    def __init__(self,*args):self.cancelled=False
    def cancel(self):self.cancelled=True

@pytest.fixture
def setup(tmp_path,monkeypatch):
    root=tmp_path/'workspace';root.mkdir()
    for key in list(os.environ):
        if key.startswith(('TWYLT_DOCKER_','TWYLT_CONTAINER_')) or key in {'DOCKER_HOST','CONTAINER_HOST'}:monkeypatch.delenv(key)
    monkeypatch.setenv('TWYLT_WORKSPACE_ROOT',str(root));monkeypatch.setenv('TWYLT_INCIDENT_LOG',str(tmp_path/'audit.jsonl'))
    return root,FakeEngine()

def execute(engine,**kwargs):
    with Workspace('docker_run'):
        return r.execute(r.Input(image='alpine:3.20',**kwargs),client_factory=lambda:engine,watchdog_factory=Guardian)


def test_success_resources_environment_and_mount(setup):
    root,engine=setup;(root/'data').mkdir()
    out=execute(engine,command=['echo','hello; not host shell'],environment={'A':'B'},volumes=[r.BindVolume(source='/data',target='/work')])
    assert out.ok and out.status=='succeeded' and out.exit_code==0 and out.cleanup_ok
    assert out.stdout=='hello\n' and engine.items=={}
    kw=engine.created[0].kwargs
    assert kw['nano_cpus']==1000000000 and kw['mem_limit']==512*1024**2 and kw['memswap_limit']==kw['mem_limit']
    assert kw['network_mode']=='none' and kw['environment']=={'A':'B'} and kw['command'][1]=='hello; not host shell'
    assert kw['mounts'][0]['Source']==str(root/'data') and kw['mounts'][0]['ReadOnly']
    assert kw['image']==engine.image.id and kw['cap_drop']==['ALL'] and not kw['privileged']
    assert kw['restart_policy']=={'Name':'no'} and kw['pids_limit']==256


def test_entrypoint_network_and_default_cmd(setup):
    _,engine=setup
    execute(engine,entrypoint=['/bin/sh','-c'],network='enabled')
    assert engine.created[0].kwargs['entrypoint']==['/bin/sh','-c']
    assert engine.created[0].kwargs['command']==['default'] and engine.created[0].kwargs['network_mode']=='bridge'
    execute(engine,entrypoint=[])
    assert engine.created[1].kwargs['entrypoint']==['']

@pytest.mark.parametrize('value', ['../bad','https://evil/x','evil.example/x:1','alpine;id','repo@sha256:short'])
def test_image_rejections(setup,value):
    with Workspace('docker_run'):
        with pytest.raises(WorkspaceDenied):r.Settings().image_name(value)


def test_registry_namespace(setup,monkeypatch):
    monkeypatch.setenv('TWYLT_DOCKER_REGISTRY','registry.example:5000/team')
    s=r.Settings()
    assert s.image_name('tool:V1')=='registry.example:5000/team/tool:V1'
    assert s.image_name('registry.example:5000/team/tool:V1')=='registry.example:5000/team/tool:V1'
    with pytest.raises(WorkspaceDenied):s.image_name('registry.example:5000/other/tool')

@pytest.mark.parametrize('key,value', [('TWYLT_DOCKER_CPUS','NaN'),('TWYLT_DOCKER_CPUS','0'),('TWYLT_DOCKER_MEMORY','2m'),('TWYLT_DOCKER_MEMORY','unlimited'),('TWYLT_DOCKER_MAX_CONTAINERS','0'),('TWYLT_DOCKER_MAX_TIMEOUT','0'),('TWYLT_DOCKER_ALLOW_PULL','yes'),('TWYLT_DOCKER_REGISTRY','https://registry.example'),('DOCKER_HOST','tcp://localhost:2375')])
def test_bad_configuration(setup,monkeypatch,key,value):
    monkeypatch.setenv(key,value)
    with pytest.raises(WorkspaceDenied):r.Settings()


def test_timeout_max(setup,monkeypatch):
    _,e=setup;monkeypatch.setenv('TWYLT_DOCKER_MAX_TIMEOUT','3')
    with pytest.raises(WorkspaceDenied):execute(e,timeout=4)
    assert not e.created

@pytest.mark.parametrize('source,target', [('../outside','/work'),('/data','/'),('/data','/proc/a'),('/data','/a/../b')])
def test_volume_rejections(setup,source,target):
    root,e=setup;(root/'data').mkdir()
    with pytest.raises(WorkspaceDenied):execute(e,volumes=[r.BindVolume(source=source,target=target)])
    assert not e.created


def test_symlink_volume_and_overlap(setup,tmp_path):
    root,e=setup;(root/'data').mkdir();(root/'data/link').symlink_to(tmp_path)
    with pytest.raises(WorkspaceDenied):execute(e,volumes=[r.BindVolume(source='/data',target='/work')])
    (root/'data/link').unlink()
    with pytest.raises(WorkspaceDenied):execute(e,volumes=[r.BindVolume(source='/data',target='/work'),r.BindVolume(source='/data',target='/work/nested')])


def test_image_anonymous_volumes(setup):
    root,e=setup;e.image.attrs['Config']['Volumes']={'/data':{}}
    with pytest.raises(WorkspaceDenied):execute(e)
    (root/'data').mkdir()
    assert execute(e,volumes=[r.BindVolume(source='/data',target='/data',read_only=False)]).ok


def test_pull_permissions(setup,monkeypatch):
    _,e=setup;e.missing=True;called=[]
    def pull(*args):called.append(args);e.missing=False
    monkeypatch.setattr(r,'pull_image',pull)
    with pytest.raises(WorkspaceDenied):execute(e)
    assert not called
    monkeypatch.setenv('TWYLT_DOCKER_ALLOW_PULL','true')
    assert execute(e).ok and len(called)==1
    assert execute(e).ok and len(called)==1  # Cache is reused.

@pytest.mark.parametrize('failure', ['start','timeout','exit','oom','cleanup','watchdog'])
def test_failures_and_cleanup(setup,failure):
    _,e=setup
    if failure=='start':e.fail_start=True
    if failure=='timeout':e.timeout=True
    if failure=='exit':e.exitcode=7
    if failure=='oom':e.exitcode=137;e.oom=True
    if failure=='cleanup':e.fail_remove=True
    if failure=='watchdog':
        def no_guardian(*args):raise RuntimeError('no watchdog')
        with Workspace('docker_run'):out=r.execute(r.Input(image='alpine'),lambda:e,no_guardian)
        assert not e.created[0].started
    else:out=execute(e)
    assert not out.ok
    if failure=='cleanup':assert not out.cleanup_ok and len(e.items)==1
    else:assert out.cleanup_ok and not e.items
    if failure=='timeout':assert out.exit_code==124 and out.timed_out and e.created[0].killed
    if failure=='exit':assert out.exit_code==7 and out.status=='failed'
    if failure=='oom':assert out.oom_killed


def test_truncated_logs(setup):
    _,e=setup;e.out=b'a'*(r.OUTPUT_LIMIT+10)
    out=execute(e)
    assert len(out.stdout)==r.OUTPUT_LIMIT and out.stdout_truncated


def test_concurrency_atomic_slots(setup,monkeypatch):
    _,e=setup;monkeypatch.setenv('TWYLT_DOCKER_MAX_CONTAINERS','2')
    e.wait_gate=threading.Event()
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures=[pool.submit(execute,e) for _ in range(12)]
        deadline=time.monotonic()+3
        while len(e.created)<2 and time.monotonic()<deadline:time.sleep(.01)
        time.sleep(.15)
        assert len(e.items)==2 and e.peak==2
        e.wait_gate.set()
        results=[f.result(timeout=5) for f in futures]
    assert any(x.status=='busy' for x in results) and any(x.ok for x in results)
    assert e.peak<=2 and not e.items


def test_stale_slot_reap_and_limit_mismatch(setup,monkeypatch):
    _,e=setup
    with Workspace('docker_run'):
        s=r.Settings();data=r.Input(image='alpine')
        c,deadline=r.reserve(e,s,e.image,data,[],1,'old')
        c.attrs['Config']['Labels'][r.DEADLINE_KEY]=str(time.time()-100)
        assert execute(e).ok and c.removed
        c,deadline=r.reserve(e,s,e.image,data,[],100,'live')
        monkeypatch.setenv('TWYLT_DOCKER_MAX_CONTAINERS','3')
        with pytest.raises(WorkspaceDenied):execute(e)
        assert not c.removed


def test_daemon_limits_required(setup):
    _,e=setup;e.info_data['MemoryLimit']=False
    with pytest.raises(WorkspaceDenied):execute(e)
    assert not e.created


def test_strict_input():
    for data in [{'image':'x','privileged':True},{'image':'x','command':'echo hello'},{'image':'x','command':[]},{'image':'x','environment':{'A':1}},{'image':'x','network':'host'}]:
        with pytest.raises(ValueError):r.Input(**data)
