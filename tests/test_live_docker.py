"""Opt-in real Docker/Podman tests. Require a dedicated daemon and a pre-pulled image."""
import os
from pathlib import Path
import sys
import pytest
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'src'))
import runtime as r
from workspace import Workspace
pytestmark=pytest.mark.skipif(os.environ.get('TWYLT_DOCKER_LIVE_TESTS')!='1',reason='Set TWYLT_DOCKER_LIVE_TESTS=1 on a dedicated local Docker/Podman service')

@pytest.fixture
def live(tmp_path,monkeypatch):
    root=tmp_path/'workspace';root.mkdir()
    monkeypatch.setenv('TWYLT_WORKSPACE_ROOT',str(root));monkeypatch.setenv('TWYLT_INCIDENT_LOG',str(tmp_path/'audit.jsonl'))
    monkeypatch.setenv('TWYLT_DOCKER_ALLOW_PULL','false');monkeypatch.setenv('TWYLT_DOCKER_REGISTRY','docker.io')
    monkeypatch.setenv('TWYLT_DOCKER_MAX_CONTAINERS','2');monkeypatch.setenv('TWYLT_DOCKER_MAX_TIMEOUT','30')
    with Workspace('docker_run'):
        yield root


def test_live_exit_and_cleanup(live):
    out=r.execute(r.Input(image='alpine:3.20',command=['sh','-c','echo hello; echo diagnostic >&2; exit 7'],timeout=20))
    assert out.status=='failed' and out.exit_code==7 and out.cleanup_ok
    assert 'hello' in out.stdout and 'diagnostic' in out.stderr


def test_live_timeout(live):
    out=r.execute(r.Input(image='alpine:3.20',command=['sleep','10'],timeout=2))
    assert out.timed_out and out.exit_code==124 and out.cleanup_ok


def test_live_bind_write(live):
    (live/'work').mkdir()
    out=r.execute(r.Input(image='alpine:3.20',command=['sh','-c','echo data > /work/result'],volumes=[r.BindVolume(source='/work',target='/work',read_only=False)],timeout=20))
    assert out.ok and (live/'work/result').read_text().strip()=='data'


def test_live_tagged_images(live):
    result=r.list_images(r.ImagesInput(limit=1000))
    assert result.ok and result.images
    assert all(item.tags and '<none>:<none>' not in item.tags for item in result.images)


def test_live_isolated_network_and_resources(live):
    # Read effective cgroup v2 configuration from the application namespace.
    settings=r.Settings()
    script='cat /sys/fs/cgroup/memory.max; cat /sys/fs/cgroup/memory.swap.max; cat /sys/fs/cgroup/cpu.max; ls /sys/class/net'
    out=r.execute(r.Input(image='alpine:3.20',command=['sh','-c',script],timeout=20))
    assert out.ok, out.error
    lines=out.stdout.splitlines()
    assert int(lines[0])==settings.memory and lines[1]=='0'
    quota,period=map(int,lines[2].split())
    assert abs(quota/period-settings.nano_cpus/1e9)<0.0001
    assert lines[3:]==['lo']
