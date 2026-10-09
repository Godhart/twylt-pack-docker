"""Shared policy regression suite, identical in both packs."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

BASE = Path(__file__).resolve().parents[1]
from twylt import guardrails as policy

@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path/'workspace'; root.mkdir()
    log = tmp_path/'incidents.jsonl'
    monkeypatch.setenv('TWYLT_WORKSPACE_ROOT', str(root))
    monkeypatch.setenv('TWYLT_INCIDENT_LOG', str(log))
    return root, log

def events(log):
    return [json.loads(line) for line in log.read_text().splitlines()]

def test_virtual_root_and_cwd(configured, tmp_path, monkeypatch):
    root, log = configured
    monkeypatch.chdir(tmp_path)
    with policy.Workspace('test') as ws:
        assert ws.resolve('/repo/new.txt') == root/'repo/new.txt'
        assert ws.resolve('repo/new.txt') == root/'repo/new.txt'
        assert ws.resolve('/') == root and ws.resolve('.') == root
        assert ws.resolve(str(tmp_path/'outside')) == root/str(tmp_path/'outside').lstrip('/')
        assert ws.virtual(root/'repo/new.txt') == '/repo/new.txt'
    assert log.read_text() == ''

@pytest.mark.parametrize('value', ['../outside', '/../../outside', 'x/../y', '//server/share', r'C:\outside', 'C:/outside', r'x\..\y', '~/secret', 'x:stream', 'bad\0name'])
def test_bad_paths_logged(configured, value):
    root, log = configured
    with policy.Workspace('fs_read') as ws:
        with pytest.raises(policy.WorkspaceDenied) as caught: ws.resolve(value)
    event = events(log)[0]
    assert event['incident_id'] == caught.value.incident_id
    assert event['tool'] == 'fs_read' and event['action'] == 'deny'
    assert event['timestamp'] and event['pid'] > 0
    assert len(events(log)) == 1

@pytest.mark.parametrize('which', ['parent', 'leaf', 'dangling', 'inside'])
def test_symlinks(configured, tmp_path, which):
    root, log = configured
    other = tmp_path/'other'; other.mkdir(); (other/'secret').write_text('SECRET')
    (root/'real').write_text('inside')
    link = root/'link'
    link.symlink_to(root/'real' if which == 'inside' else other/'missing' if which == 'dangling' else other, target_is_directory=which not in {'inside','dangling'})
    with policy.Workspace('fs_read') as ws:
        with pytest.raises(policy.WorkspaceDenied): ws.resolve('/link/secret' if which == 'parent' else '/link')
    assert events(log)[0]['code'] == 'symlink_forbidden'
    assert 'SECRET' not in log.read_text()

@pytest.mark.skipif(os.name != 'posix', reason='POSIX link and FIFO')
def test_hardlinks_and_special_files(configured, tmp_path):
    root, log = configured
    target=tmp_path/'outside'; target.write_text('secret')
    os.link(target, root/'hard')
    os.mkfifo(root/'pipe')
    with policy.Workspace('test') as ws:
        for name in ['hard','pipe']:
            with pytest.raises(policy.WorkspaceDenied): ws.resolve(name)
    assert [e['code'] for e in events(log)] == ['hardlink_forbidden','special_file_forbidden']

@pytest.mark.parametrize('value', [None, '', 'relative'])
def test_missing_config_fails_closed(tmp_path, monkeypatch, capsys, value):
    monkeypatch.delenv('TWYLT_INCIDENT_LOG', raising=False)
    if value is None: monkeypatch.delenv('TWYLT_WORKSPACE_ROOT', raising=False)
    else: monkeypatch.setenv('TWYLT_WORKSPACE_ROOT', value)
    with pytest.raises(policy.WorkspaceDenied): policy.Workspace('fs_write')
    event=json.loads(capsys.readouterr().err)
    assert event['code'] == 'workspace_not_configured'

@pytest.mark.parametrize('case', ['inside','missing_parent','directory','symlink','relative'])
def test_log_configuration_fail_closed(configured, tmp_path, monkeypatch, case, capsys):
    root, log = configured
    if case == 'inside': destination=root/'log'
    elif case == 'missing_parent': destination=tmp_path/'absent'/'log'
    elif case == 'directory': destination=tmp_path
    elif case == 'relative': destination=Path('relative.log')
    else:
        target=tmp_path/'target';target.write_text('keep')
        destination=tmp_path/'link';destination.symlink_to(target)
    monkeypatch.setenv('TWYLT_INCIDENT_LOG', str(destination))
    with pytest.raises(policy.WorkspaceDenied): policy.Workspace('fs_write')
    event=json.loads(capsys.readouterr().err.splitlines()[-1])
    assert event['action']=='deny' and event['code'] in {'incident_log_inside_workspace','incident_log_unavailable','invalid_incident_log'}
    if case == 'symlink': assert target.read_text()=='keep'

def test_sink_loss_and_json_escaping(configured, capsys):
    root, log = configured
    ws=policy.Workspace('test')
    with pytest.raises(policy.WorkspaceDenied): ws.resolve('../bad\nname')
    assert len(events(log))==1
    log.unlink();log.mkdir()
    with pytest.raises(policy.WorkspaceDenied) as caught: ws.resolve('../outside')
    assert caught.value.code=='incident_log_unavailable'
    event=json.loads(capsys.readouterr().err)
    assert event['original_code']=='path_outside_workspace'

def test_root_mutation_and_tree_preflight(configured, tmp_path):
    root, log = configured
    folder=root/'a';folder.mkdir();(folder/'file').write_text('keep')
    (folder/'link').symlink_to(tmp_path)
    with policy.Workspace('test') as ws:
        with pytest.raises(policy.WorkspaceDenied): ws.protect_root(root)
        with pytest.raises(policy.WorkspaceDenied): ws.tree(folder)
    assert (folder/'file').read_text()=='keep'

def test_parallel_incident_append(configured):
    root, log = configured
    code = "import sys; sys.path.insert(0,sys.argv[1]); from workspace import Workspace, WorkspaceDenied\ntry: Workspace('parallel').resolve('../outside')\nexcept WorkspaceDenied: pass"
    processes=[subprocess.Popen([sys.executable,'-c',code,str(BASE/'tests/legacy')],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(8)]
    for p in processes:
        out, err=p.communicate(timeout=10)
        assert p.returncode==0 and not out and not err
    records=events(log)
    assert len(records)==8 and len({e['incident_id'] for e in records})==8
