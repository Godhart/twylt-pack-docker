import json
from types import SimpleNamespace
import pytest
from test_runtime import setup, execute, r, Workspace, WorkspaceDenied
from test_images_network import Client


def settings():
    with Workspace('settings'):return r.Settings()

@pytest.mark.parametrize('engine,uid,expected',[
    ('docker',1000,'unix:///var/run/docker.sock'),
    ('podman',0,'unix:///run/podman/podman.sock'),
    ('podman',1000,'unix:///run/user/1000/podman/podman.sock')])
def test_default_socket(setup,monkeypatch,engine,uid,expected):
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE',engine)
    monkeypatch.delenv('XDG_RUNTIME_DIR',raising=False)
    monkeypatch.setattr(r.os,'geteuid',lambda:uid)
    config=settings()
    assert config.host==expected
    assert config.api_version==('1.40' if engine=='podman' else 'auto')


def test_rootless_runtime_dir_and_endpoint_precedence(setup,monkeypatch):
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE','podman')
    monkeypatch.setattr(r.os,'geteuid',lambda:1001)
    monkeypatch.setenv('XDG_RUNTIME_DIR','/custom/runtime')
    monkeypatch.setenv('DOCKER_HOST','unix:///wrong/docker.sock')
    assert settings().host=='unix:///custom/runtime/podman/podman.sock'
    monkeypatch.setenv('CONTAINER_HOST','unix:///custom/podman.sock')
    assert settings().host=='unix:///custom/podman.sock'
    monkeypatch.setenv('TWYLT_CONTAINER_HOST','unix:///explicit.sock')
    assert settings().host=='unix:///explicit.sock'
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE','docker')
    assert settings().host=='unix:///explicit.sock'
    monkeypatch.delenv('TWYLT_CONTAINER_HOST')
    assert settings().host=='unix:///wrong/docker.sock'

@pytest.mark.parametrize('key,value',[
    ('TWYLT_CONTAINER_ENGINE','containerd'),('TWYLT_CONTAINER_ENGINE',''),
    ('TWYLT_CONTAINER_HOST','tcp://localhost:1234'),
    ('CONTAINER_HOST','ssh://user@host'),('TWYLT_CONTAINER_HOST','')])
def test_invalid_backend_config_audited(setup,monkeypatch,key,value):
    root,engine=setup
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE','podman')
    monkeypatch.setenv(key,value)
    with pytest.raises(WorkspaceDenied):settings()
    assert len((root.parent/'audit.jsonl').read_text().splitlines())==1

@pytest.mark.parametrize('backend',['docker','podman'])
def test_backend_lifecycle_and_log_options(setup,monkeypatch,backend):
    root,engine=setup
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE',backend)
    out=execute(engine)
    assert out.ok and out.cleanup_ok and engine.created[0].removed
    kwargs=engine.created[0].kwargs
    assert kwargs['nano_cpus']==1000000000 and kwargs['mem_limit']==512*1024**2
    assert kwargs['network_mode']=='none' and kwargs['mounts']==[]
    assert kwargs['log_config']['Type']=='json-file'
    assert kwargs['log_config']['Config']==({'max-size':'10m'} if backend=='podman' else {'max-size':'10m','max-file':'1'})

@pytest.mark.parametrize('version',['1',None])
def test_podman_rootless_v1_denied(setup,monkeypatch,version):
    root,engine=setup
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE','podman')
    engine.info_data.update(Rootless=True,CgroupVersion=version)
    with pytest.raises(WorkspaceDenied):execute(engine)
    assert engine.created==[]


def test_podman_rootless_v2(setup,monkeypatch):
    root,engine=setup
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE','podman')
    engine.info_data.update(Rootless=True,CgroupVersion='2')
    assert execute(engine).ok


def test_podman_network_policy_before_connect(setup,monkeypatch):
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE','podman')
    monkeypatch.setenv('TWYLT_DOCKER_DISABLE_NETWORK','true')
    with Workspace('docker_run'),pytest.raises(WorkspaceDenied):
        r.execute(r.Input(image='alpine',network='enabled'),client_factory=lambda:pytest.fail('Connected'))


def test_inventory_uses_selected_socket_api(setup,monkeypatch):
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE','podman')
    monkeypatch.setenv('TWYLT_CONTAINER_HOST','unix:///selected.sock')
    client=Client([]);calls=[]
    def factory(**kwargs):calls.append(kwargs);return client
    monkeypatch.setattr(r.docker,'DockerClient',factory)
    with Workspace('docker_images'):out=r.list_images(r.ImagesInput())
    assert out.ok and client.closed
    assert calls==[dict(base_url='unix:///selected.sock',timeout=10,version='1.40')]


def test_pull_inherits_selected_socket(setup,monkeypatch):
    monkeypatch.setenv('TWYLT_CONTAINER_ENGINE','podman')
    monkeypatch.setenv('TWYLT_CONTAINER_HOST','unix:///selected.sock')
    calls=[]
    def run(*args,**kwargs):calls.append(json.loads(kwargs['input']));return SimpleNamespace(returncode=0)
    monkeypatch.setattr(r.subprocess,'run',run)
    r.pull_image(settings(),'docker.io/library/alpine:3.20','1.40')
    assert calls==[dict(host='unix:///selected.sock',version='1.40',name='docker.io/library/alpine:3.20')]
