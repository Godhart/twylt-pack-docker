import json,os,shutil,subprocess,sys
from pathlib import Path
import pytest
from toolpack_builder.builder import BuildConfig,scan,build_from_report
BASE=Path(__file__).resolve().parents[1]

def test_copied_pack_builder_nested_transport(tmp_path,monkeypatch):
    pack=tmp_path/'copied'
    shutil.copytree(BASE/'tools',pack/'tools',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(BASE/'shared',pack/'shared',ignore=shutil.ignore_patterns('__pycache__'))
    config=BuildConfig(root=pack/'tools',glob='*/tool.py',python=sys.executable)
    report=scan(config)
    assert len(report.valid)==2,report.failed
    payload=build_from_report(config,report).payload
    tools={t['name']:t for t in payload['category']['tools']}
    workspace=tmp_path/'workspace';workspace.mkdir()
    runs=tmp_path/'transport';cwd=runs/'request'/'nested';cwd.mkdir(parents=True)
    env={**os.environ,'TWYLT_GUARDRAILS':'1','TWYLT_WORKSPACE_ROOT':str(workspace),
         'TWYLT_ALLOWED_CWD':str(runs),'TWYLT_DISABLE_NETWORK':'0','TWYLT_INCIDENT_LOG':''}
    pass
    (cwd/'input.json').write_text(json.dumps({}))
    code='import docker\nfrom types import SimpleNamespace\ndocker.DockerClient=lambda **kw: SimpleNamespace(api=SimpleNamespace(images=lambda **kw: []),close=lambda: None)\n'+tools['docker_images']['code']
    r=subprocess.run([sys.executable,'-c',code],cwd=cwd,env=env,stdin=subprocess.DEVNULL,
                     capture_output=True,text=True,timeout=20)
    assert r.returncode==0,r.stderr
    result=json.loads((cwd/'output.json').read_text())
    assert result['ok'] and result['images']==[]
    assert not (workspace/'output.json').exists()


def test_common_network_guard_before_daemon(tmp_path,monkeypatch):
    from test_runtime import r,Workspace,WorkspaceDenied
    monkeypatch.setenv('TWYLT_DISABLE_NETWORK','1')
    monkeypatch.setenv('TWYLT_WORKSPACE_ROOT',str(tmp_path))
    monkeypatch.setenv('TWYLT_DOCKER_DISABLE_NETWORK','false')
    def forbidden():pytest.fail('daemon must not be contacted')
    with Workspace('docker_run'),pytest.raises(WorkspaceDenied) as error:
        r.execute(r.Input(image='alpine',network='enabled'),client_factory=forbidden)
    assert error.value.code=='network_disabled'

def test_common_network_guard_before_pull(tmp_path,monkeypatch):
    from test_runtime import r,Workspace,WorkspaceDenied
    monkeypatch.setenv('TWYLT_DISABLE_NETWORK','1')
    monkeypatch.setenv('TWYLT_WORKSPACE_ROOT',str(tmp_path))
    def forbidden(*args,**kwargs):pytest.fail('pull subprocess must not start')
    monkeypatch.setattr(r.subprocess,'run',forbidden)
    with Workspace('docker_run'),pytest.raises(WorkspaceDenied) as error:
        r.pull_image(None,'docker.io/library/alpine','1.0')
    assert error.value.code=='network_disabled'
