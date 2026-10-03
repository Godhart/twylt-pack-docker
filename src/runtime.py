"""One-shot Docker execution. Canonical source embedded into the TWYLT entrypoint."""
import json
import os
import re
import select
import subprocess
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Literal
import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.types import Mount, LogConfig
from requests.exceptions import RequestException, ReadTimeout
from pydantic import BaseModel, ConfigDict, Field, model_validator
from twylt import Tool, Requirements
from workspace import Workspace, WorkspaceDenied, WorkspaceTransport, workspace
WORKSPACE_SOURCE = Path(__file__).with_name('workspace.py').read_text(encoding='utf-8')

OWNER_KEY = 'io.twylt.app-runner'
OWNER = 'v1'
TOKEN_KEY = 'io.twylt.run-token'
DEADLINE_KEY = 'io.twylt.deadline'
LIMIT_KEY = 'io.twylt.max-containers'
SLOT_PREFIX = 'twylt-app-slot-'
API_TIMEOUT = 10
OUTPUT_LIMIT = 1048576
CLEANUP_GRACE = 15

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class BindVolume(Model):
    source: str = Field(min_length=1, description='Virtual host path within TWYLT_WORKSPACE_ROOT; must already exist.')
    target: str = Field(min_length=1, description='Absolute POSIX path inside the container.')
    read_only: bool = Field(default=True, description='Read-only bind mount by default; false allows persistent host writes.')

class Input(Model):
    image: str = Field(min_length=1, description='Image relative to configured registry prefix, or a fully qualified image under that prefix.')
    command: list[str] | None = Field(default=None, min_length=1, description='Container CMD argv. No implicit shell; null inherits image CMD.')
    entrypoint: list[str] | None = Field(default=None, description='Container ENTRYPOINT argv. null inherits image ENTRYPOINT, [] clears it.')
    environment: dict[str, str] = Field(default_factory=dict, description='Environment variables inside the container only. Host environment is not forwarded.')
    volumes: list[BindVolume] = Field(default_factory=list, max_length=32, description='Host bind mounts only; no named or anonymous Docker volumes.')
    network: Literal['isolated', 'enabled'] = Field(default='isolated', description='isolated maps to none; enabled maps to bridge, never host networking.')
    timeout: int | None = Field(default=None, ge=1, le=86400, description='Execution budget in seconds, including container creation/start. Default min(60, configured maximum).')

    @model_validator(mode='after')
    def arguments(self):
        for values in [self.command, self.entrypoint]:
            if values is not None and any('\x00' in s for s in values): raise ValueError('NUL in argv')
            if values and not values[0]: raise ValueError('Executable/first argument must not be empty')
        for key,value in self.environment.items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key) or '\x00' in value:
                raise ValueError('Invalid environment key or NUL in value')
        return self

class Output(Model):
    ok: bool
    status: Literal['succeeded','failed','timed_out','busy','error']
    image: str
    image_id: str | None = None
    container_id: str | None = None
    exit_code: int | None = None
    container_exit_code: int | None = None
    stdout: str = ''
    stderr: str = ''
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    oom_killed: bool = False
    cleanup_ok: bool = True
    duration_seconds: float = 0.0
    error: str | None = None

class Settings:
    def __init__(self):
        ws=workspace()
        def invalid(): ws.deny('docker_configuration_invalid')
        flag=os.environ.get('TWYLT_DOCKER_ALLOW_PULL','false').lower()
        if flag not in {'true','false','1','0'}: invalid()
        self.allow_pull=flag in {'true','1'}
        flag=os.environ.get('TWYLT_DOCKER_DISABLE_NETWORK','false').lower()
        if flag not in {'true','false','1','0'}: invalid()
        self.disable_network=flag in {'true','1'}
        self.registry=os.environ.get('TWYLT_DOCKER_REGISTRY','docker.io').rstrip('/')
        if not re.fullmatch(r'(?:localhost|[a-z0-9]+(?:[.-][a-z0-9]+)*)(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*',self.registry): invalid()
        try:
            cpus=Decimal(os.environ.get('TWYLT_DOCKER_CPUS','1'))
            if not cpus.is_finite() or not Decimal('0.01')<=cpus<=1024: invalid()
            self.nano_cpus=int(cpus*1000000000)
            memory=os.environ.get('TWYLT_DOCKER_MEMORY','512m').lower()
            match=re.fullmatch(r'([1-9][0-9]*)([bkmg]?)',memory)
            if match is None: invalid()
            self.memory=int(match[1])*{'':1,'b':1,'k':1024,'m':1024**2,'g':1024**3}[match[2]]
            if not 6*1024**2<=self.memory<=1024**4: invalid()
            self.maximum=int(os.environ.get('TWYLT_DOCKER_MAX_CONTAINERS','2'))
            self.max_timeout=int(os.environ.get('TWYLT_DOCKER_MAX_TIMEOUT','300'))
            if not 1<=self.maximum<=4096 or not 1<=self.max_timeout<=86400: invalid()
        except WorkspaceDenied:
            raise
        except (InvalidOperation,ValueError):
            invalid()
        self.engine=os.environ.get('TWYLT_CONTAINER_ENGINE','docker').lower()
        if self.engine not in {'docker','podman'}: invalid()
        self.api_version='1.40' if self.engine=='podman' else 'auto'
        if self.engine=='docker':
            default_host='unix:///var/run/docker.sock'
            host_variable='DOCKER_HOST'
        else:
            host_variable='CONTAINER_HOST'
            if os.geteuid()==0:
                default_host='unix:///run/podman/podman.sock'
            else:
                runtime_dir=os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.geteuid()}')
                if not runtime_dir.startswith('/') or '\x00' in runtime_dir: invalid()
                default_host='unix://'+runtime_dir.rstrip('/')+'/podman/podman.sock'
        self.host=os.environ.get('TWYLT_CONTAINER_HOST',os.environ.get(host_variable,default_host))
        if os.name!='posix' or not self.host.startswith('unix:///') or '\x00' in self.host:
            ws.deny('docker_local_linux_required')

    def image_name(self,value):
        ws=workspace()
        if not value or any(c.isspace() for c in value) or '://' in value or value.count('@')>1:
            ws.deny('docker_image_forbidden')
        name=value
        if '@' in value:
            name,digest=value.rsplit('@',1)
            if not re.fullmatch(r'sha256:[a-f0-9]{64}',digest): ws.deny('docker_image_forbidden')
        # The last colon is a tag only when it follows the final slash.
        repository=name
        if name.rfind(':')>name.rfind('/'):
            repository,tag=name.rsplit(':',1)
            if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}',tag): ws.deny('docker_image_forbidden')
        parts=repository.split('/')
        first=parts[0]
        qualified=len(parts)>1 and ('.' in first or ':' in first or first=='localhost')
        if qualified and not re.fullmatch(r'(?:localhost|[a-z0-9]+(?:[.-][a-z0-9]+)*)(?::[0-9]{1,5})?',first):
            ws.deny('docker_image_forbidden')
        names=parts[1:] if qualified else parts
        if not names or any(not re.fullmatch(r'[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*',part) for part in names):
            ws.deny('docker_image_forbidden')
        if qualified:
            if not value.startswith(self.registry+'/'): ws.deny('docker_registry_forbidden')
            result=value
        else:
            prefix=self.registry
            if prefix=='docker.io' and '/' not in name: prefix+='/library'
            result=prefix+'/'+value
        return result


def bind_mounts(data):
    ws=workspace();mounts=[];targets=[]
    for volume in data.volumes:
        path=ws.resolve(volume.source)
        if not path.exists(): raise ValueError('Bind source must exist: '+volume.source)
        ws.tree(path)
        target=volume.target
        p=PurePosixPath(target)
        if not target.startswith('/') or target.startswith('//') or '\x00' in target or '\\' in target or '..' in p.parts or str(p)!=target:
            ws.deny('docker_mount_target_forbidden')
        if p==PurePosixPath('/') or any(p==PurePosixPath(x) or PurePosixPath(x) in p.parents for x in ['/proc','/sys','/dev']):
            ws.deny('docker_mount_target_forbidden')
        if any(p==q or p in q.parents or q in p.parents for q in targets): ws.deny('docker_mount_overlap')
        targets.append(p)
        mounts.append(Mount(target=target,source=str(path),type='bind',read_only=volume.read_only,propagation='rprivate'))
    return mounts


def incident(code):
    try: workspace().deny(code)
    except WorkspaceDenied as exc: return exc.incident_id


def remove_container(container):
    try:
        container.remove(force=True,v=True)
        return True
    except NotFound: return True
    except (APIError,RequestException): return False


def reap_expired(client):
    now=time.time()
    containers=client.containers.list(all=True,filters={'label':OWNER_KEY+'='+OWNER},ignore_removed=True)
    for c in containers:
        labels=c.attrs.get('Config',{}).get('Labels') or {}
        if not c.name.startswith(SLOT_PREFIX): continue
        try: deadline=float(labels[DEADLINE_KEY])
        except (KeyError,ValueError): continue
        if now>deadline+CLEANUP_GRACE and not remove_container(c): incident('docker_expired_cleanup_failed')
    return containers


PULL_CODE = r"""
import json,sys,docker
p=json.load(sys.stdin)
c=docker.DockerClient(base_url=p['host'],version=p['version'],timeout=10)
try:
    for item in c.api.pull(p['name'],stream=True,decode=True):
        if item.get('error'): raise RuntimeError('pull failed')
finally: c.close()
"""

def pull_image(settings,name,version):
    # Docker SDK pull sets timeout=None internally. Isolate it in a process so a
    # wall-clock setup timeout also covers stalled headers/partial stream frames.
    payload=json.dumps({'host':settings.host,'version':version,'name':name})
    try:
        done=subprocess.run([sys.executable,'-c',PULL_CODE],input=payload,text=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=settings.max_timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError('Image pull setup budget exceeded') from None
    if done.returncode: raise RuntimeError('Image pull failed')

def get_image(client,settings,name):
    try: return client.images.get(name)
    except ImageNotFound:
        if not settings.allow_pull: workspace().deny('docker_pull_forbidden')
    pull_image(settings,name,client.api._version)
    return client.images.get(name)


def check_image_volumes(image,mounts):
    declared=image.attrs.get('Config',{}).get('Volumes') or {}
    targets={m['Target'] for m in mounts}
    if not set(declared).issubset(targets): workspace().deny('docker_image_volume_requires_bind')


def reserve(client,settings,image,data,mounts,seconds,token):
    reap_expired(client)
    # All clients on a daemon must share the same configured maximum. Reject a
    # mismatch with existing leases rather than silently expanding capacity.
    existing=client.containers.list(all=True,filters={'label':OWNER_KEY+'='+OWNER},ignore_removed=True)
    for c in existing:
        labels=c.attrs.get('Config',{}).get('Labels') or {}
        if labels.get(LIMIT_KEY)!=str(settings.maximum): workspace().deny('docker_capacity_configuration_mismatch')
    image_config=image.attrs.get('Config',{})
    cmd=data.command if data.command is not None else image_config.get('Cmd')
    ep=data.entrypoint if data.entrypoint is not None else image_config.get('Entrypoint')
    if ep==[]: ep=['']  # Docker API sentinel to clear image entrypoint.
    kwargs=dict(image=image.id,command=cmd,entrypoint=ep,
        environment=data.environment,mounts=mounts,network_mode='none' if data.network=='isolated' else 'bridge',
        nano_cpus=settings.nano_cpus,mem_limit=settings.memory,memswap_limit=settings.memory,pids_limit=256,
        cap_drop=['ALL'],security_opt=['no-new-privileges:true'],privileged=False,
        detach=True,stdin_open=False,tty=False,auto_remove=False,
        restart_policy={'Name':'no'},healthcheck={'test':['NONE']},
        log_config=LogConfig(type='json-file',config=({'max-size':'10m'} if settings.engine=='podman' else {'max-size':'10m','max-file':'1'})))
    for slot in range(settings.maximum):
        deadline=time.time()+seconds
        labels={OWNER_KEY:OWNER,TOKEN_KEY:token,DEADLINE_KEY:str(deadline),LIMIT_KEY:str(settings.maximum)}
        try:
            c=client.containers.create(name=SLOT_PREFIX+str(slot),labels=labels,**kwargs)
            return c,deadline
        except APIError as exc:
            if exc.status_code!=409: raise
    return None,None


# Standalone guardian is passed via python -c; it survives the caller and does not
# depend on a temporary ToolHub script remaining on disk. WORKSPACE_SOURCE is
# embedded by the generator to preserve protected audit handling in the guardian.
WATCHDOG_BODY = r'''
import json,os,sys,time
import docker
from docker.errors import NotFound
payload=json.load(sys.stdin)
client=docker.DockerClient(base_url=payload['host'],version=payload['api_version'],timeout=10)
print('ready',flush=True)
sys.stdout.close()
while time.monotonic()<payload['monotonic_deadline']:
    time.sleep(min(1,max(0,payload['monotonic_deadline']-time.monotonic())))
reported=False
while True:
    try:
        c=client.containers.get(payload['container_id'])
        labels=c.attrs.get('Config',{}).get('Labels') or {}
        if labels.get('io.twylt.run-token')!=payload['token'] or labels.get('io.twylt.app-runner')!='v1': break
        if c.attrs.get('State',{}).get('Running'):
            try: c.kill()
            except NotFound: break
        if time.monotonic()<payload['monotonic_deadline']+15:
            time.sleep(min(1,max(0,payload['monotonic_deadline']+15-time.monotonic())))
            continue
        c.remove(force=True,v=True)
        break
    except NotFound:
        break
    except Exception:
        if not reported:
            try: Workspace('docker_run.watchdog').deny('docker_watchdog_cleanup_failed')
            except Exception: pass
            reported=True
        time.sleep(5)
client.close()
'''

class Watchdog:
    def __init__(self,settings,client,container,deadline,token):
        payload={'host':settings.host,'api_version':client.api._version,'container_id':container.id,'deadline':deadline,'monotonic_deadline':time.monotonic()+max(0,deadline-time.time()),'token':token}
        self.process=subprocess.Popen([sys.executable,'-c',WORKSPACE_SOURCE+'\n'+WATCHDOG_BODY],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,text=True)
        try:
            self.process.stdin.write(json.dumps(payload));self.process.stdin.close()
            ready,_,_=select.select([self.process.stdout],[],[],10)
            if not ready or self.process.stdout.readline().strip()!='ready':
                raise RuntimeError('Watchdog did not acknowledge startup')
        except BaseException:
            self.cancel()
            raise
        finally:
            self.process.stdout.close()

    def cancel(self):
        if self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=2)


def capture(container,stdout):
    stream=container.logs(stdout=stdout,stderr=not stdout,stream=True,follow=False)
    data=bytearray();truncated=False
    try:
        for chunk in stream:
            room=OUTPUT_LIMIT-len(data)
            data.extend(chunk[:room])
            if len(chunk)>room:
                truncated=True
                break
    finally:
        if hasattr(stream,'close'):stream.close()
    return bytes(data).decode('utf-8',errors='replace'),truncated


def execute(data,client_factory=None,watchdog_factory=None):
    settings=Settings();ws=workspace()
    if settings.disable_network and data.network=='enabled':
        ws.deny('docker_network_forbidden')
    name=settings.image_name(data.image)
    seconds=data.timeout if data.timeout is not None else min(60,settings.max_timeout)
    if seconds>settings.max_timeout: ws.deny('docker_timeout_exceeds_maximum')
    mounts=bind_mounts(data)
    output=Output(ok=False,status='error',image=name)
    client=None;container=None;watchdog=None
    started=time.monotonic();token=uuid.uuid4().hex
    factory=client_factory or (lambda:docker.DockerClient(base_url=settings.host,timeout=API_TIMEOUT,version=settings.api_version))
    guardian=watchdog_factory or Watchdog
    stage='connect'
    try:
        client=factory()
        info=client.info()
        if info.get('OSType')!='linux': ws.deny('docker_local_linux_required')
        if settings.engine=='podman' and info.get('Rootless') is True and str(info.get('CgroupVersion'))!='2':
            ws.deny('docker_resource_limits_unavailable')
        if not all(info.get(key) is True for key in ['MemoryLimit','SwapLimit','CpuCfsQuota']):
            ws.deny('docker_resource_limits_unavailable')
        stage='image'
        image=get_image(client,settings,name)
        check_image_volumes(image,mounts)
        output.image_id=image.id
        # Revalidate sources immediately before passing them to the daemon.
        mounts=bind_mounts(data)
        stage='reserve'
        container,deadline=reserve(client,settings,image,data,mounts,seconds,token)
        if container is None:
            output.status='busy';output.error='Container capacity exhausted; retry later.'
            return output
        run_deadline=time.monotonic()+max(0,deadline-time.time())
        output.container_id=container.id
        # Verify the engine did not create anonymous image-declared volumes.
        container.reload()
        if any(m.get('Type')!='bind' for m in container.attrs.get('Mounts',[])):
            ws.deny('docker_non_bind_mount_forbidden')
        stage='watchdog'
        watchdog=guardian(settings,client,container,deadline,token)
        if time.monotonic()>=run_deadline: raise TimeoutError('Startup exceeded execution budget')
        stage='start'
        container.start()
        stage='wait'
        while True:
            remaining=run_deadline-time.monotonic()
            if remaining<=0: raise TimeoutError('Execution budget exceeded')
            try:
                answer=container.wait(timeout=min(remaining,API_TIMEOUT))
                output.container_exit_code=int(answer['StatusCode'])
                break
            except (ReadTimeout,):
                continue
        output.timed_out=time.monotonic()>=run_deadline
        output.exit_code=124 if output.timed_out else output.container_exit_code
        output.status='timed_out' if output.timed_out else 'succeeded' if output.exit_code==0 else 'failed'
        container.reload()
        output.oom_killed=bool(container.attrs.get('State',{}).get('OOMKilled'))
        stage='logs'
        output.stdout,output.stdout_truncated=capture(container,True)
        output.stderr,output.stderr_truncated=capture(container,False)
        output.ok=output.status=='succeeded'
    except WorkspaceDenied:
        raise
    except TimeoutError:
        output.status='timed_out';output.timed_out=True;output.exit_code=124
        output.error='Timeout during '+stage
        if container is not None:
            try:
                container.kill();container.reload()
                output.container_exit_code=container.attrs.get('State',{}).get('ExitCode')
                output.oom_killed=bool(container.attrs.get('State',{}).get('OOMKilled'))
                output.stdout,output.stdout_truncated=capture(container,True)
                output.stderr,output.stderr_truncated=capture(container,False)
            except (APIError,RequestException): pass
    except (APIError,RequestException,RuntimeError,OSError,ValueError) as exc:
        # Do not include engine error bodies: they can echo secrets or host paths.
        output.status='error';output.ok=False
        output.error='Docker operation failed during '+stage+' ('+type(exc).__name__+')'
    finally:
        if container is not None:
            output.cleanup_ok=remove_container(container)
            if output.cleanup_ok:
                if watchdog is not None: watchdog.cancel()
            else:
                output.ok=False
                output.status='error'
                output.error='Container cleanup failed; watchdog/reaper will retry. Incident '+incident('docker_cleanup_failed')
        if client is not None:client.close()
        output.duration_seconds=round(time.monotonic()-started,3)
    return output


class ImagesInput(Model):
    offset: int = Field(default=0, ge=0, description='Zero-based image offset after sorting by first tag, then image ID.')
    limit: int = Field(default=100, ge=1, le=1000, description='Maximum images in this response, not number of tags.')

class ImageRecord(Model):
    image_id: str
    tags: list[str]
    size_bytes: int = Field(ge=0)
    created: str | None = None

class ImagesOutput(Model):
    ok: bool
    images: list[ImageRecord] = Field(default_factory=list)
    total: int = 0
    offset: int
    limit: int
    next_offset: int | None = None
    error: str | None = None


def list_images(data, client_factory=None):
    """Local tagged images only; never pulls or reserves container capacity."""
    settings=Settings()
    result=ImagesOutput(ok=False,offset=data.offset,limit=data.limit)
    client=None
    factory=client_factory or (lambda:docker.DockerClient(base_url=settings.host,timeout=API_TIMEOUT,version=settings.api_version))
    try:
        client=factory()
        # Low-level list is a single snapshot request, avoiding per-image inspect races.
        snapshot=client.api.images(all=True)
        grouped={}
        for item in snapshot:
            tags={tag for tag in (item.get('RepoTags') or [])
                  if isinstance(tag,str) and '@' not in tag and
                  ':' in tag.rsplit('/',1)[-1] and
                  '<none>' not in tag and tag.rsplit(':',1)[-1]}
            if not tags: continue
            image_id=item['Id']
            if image_id in grouped:
                grouped[image_id].tags=sorted(set(grouped[image_id].tags)|tags)
            else:
                created=item.get('Created')
                if isinstance(created,(int,float)):
                    from datetime import datetime, timezone
                    created=datetime.fromtimestamp(created,timezone.utc).isoformat().replace('+00:00','Z')
                grouped[image_id]=ImageRecord(image_id=image_id,tags=sorted(tags),
                    size_bytes=item['Size'],created=created)
        ordered=sorted(grouped.values(),key=lambda row:(row.tags[0],row.image_id))
        result.total=len(ordered)
        result.images=ordered[data.offset:data.offset+data.limit]
        if data.offset+data.limit<result.total: result.next_offset=data.offset+data.limit
        result.ok=True
    except (APIError,RequestException,RuntimeError,OSError,ValueError,KeyError,TypeError) as exc:
        result.error='Docker image listing failed: '+type(exc).__name__
    finally:
        if client is not None: client.close()
    return result
