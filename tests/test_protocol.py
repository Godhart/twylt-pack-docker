import pytest
import json
import os
from pathlib import Path
import subprocess
import sys
BASE=Path(__file__).resolve().parents[1]
SCRIPT=BASE/'tools/docker_run/run.py'

@pytest.mark.parametrize('name',['docker_run','docker_images'])
def test_discovery_without_workspace(name):
    env=dict(os.environ);env.pop('TWYLT_WORKSPACE_ROOT',None);env.pop('INPUT_DESCRIBE',None)
    p=subprocess.run([sys.executable,str(BASE/'tools'/name/'run.py'),'{"describe":"json_spec"}'],env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    spec=json.loads(p.stdout)
    assert spec['name']==name and spec['version']=='0.3.0'
    assert not spec['inputSchema']['additionalProperties']
    assert 'docker==7.1.0' in spec['requirements']['content']


def test_no_root_and_transport(tmp_path):
    env=dict(os.environ);env.pop('TWYLT_WORKSPACE_ROOT',None);env.pop('TWYLT_INCIDENT_LOG',None);env.pop('INPUT_DESCRIBE',None)
    p=subprocess.run([sys.executable,str(SCRIPT),'{"image":"alpine"}'],env=env,capture_output=True,text=True)
    assert p.returncode==5 and json.loads(p.stderr.splitlines()[0])['code']=='workspace_not_configured'
    root=tmp_path/'root';root.mkdir();env['TWYLT_WORKSPACE_ROOT']=str(root)
    (tmp_path/'output.json').write_text('keep')
    p=subprocess.run([sys.executable,str(SCRIPT)],cwd=tmp_path,stdin=subprocess.DEVNULL,env=env,capture_output=True,text=True)
    assert p.returncode==4 and (tmp_path/'output.json').read_text()=='keep'


def test_sdk_create_payload():
    # Validate actual installed SDK serialization, without contacting a daemon.
    from docker.models.containers import _create_container_args
    from docker.types import Mount,LogConfig
    args=_create_container_args({'version':'1.45','image':'sha256:abc','command':['echo','hello'],
        'name':'test','nano_cpus':500000000,'mem_limit':134217728,'memswap_limit':134217728,
        'mounts':[Mount('/work','/host/work',type='bind',read_only=True,propagation='rprivate')],
        'network_mode':'none','cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],
        'healthcheck':{'test':['NONE']},'detach':True,'auto_remove':False,
        'log_config':LogConfig(type='json-file',config={'max-size':'10m','max-file':'1'})})
    host=args['host_config']
    assert host['NanoCpus']==500000000 and host['Memory']==134217728 and host['MemorySwap']==134217728
    assert host['NetworkMode']=='none' and host['Mounts'][0]['Type']=='bind'
