from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared'))
import json
import re
import select
import subprocess
import sys
import time
import uuid
from pathlib import PurePosixPath
from typing import Literal
import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.types import Mount, LogConfig
from requests.exceptions import RequestException, ReadTimeout
from pydantic import Field, model_validator
from twylt import Tool, Requirements
from twylt.guardrails import Workspace, WorkspaceDenied, workspace, check_network
from docker_common.common import API_TIMEOUT, Model, Settings
OWNER_KEY = 'io.twylt.app-runner'
OWNER = 'v1'
TOKEN_KEY = 'io.twylt.run-token'
DEADLINE_KEY = 'io.twylt.deadline'
LIMIT_KEY = 'io.twylt.max-containers'
SLOT_PREFIX = 'twylt-app-slot-'
OUTPUT_LIMIT = 1048576
CLEANUP_GRACE = 15

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
            if values is not None and any(('\x00' in s for s in values)):
                raise ValueError('NUL in argv')
            if values and (not values[0]):
                raise ValueError('Executable/first argument must not be empty')
        for key, value in self.environment.items():
            if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', key) or '\x00' in value:
                raise ValueError('Invalid environment key or NUL in value')
        return self

class Output(Model):
    ok: bool
    status: Literal['succeeded', 'failed', 'timed_out', 'busy', 'error']
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

def bind_mounts(data):
    ws = workspace()
    mounts = []
    targets = []
    for volume in data.volumes:
        path = ws.resolve(volume.source)
        if not path.exists():
            raise ValueError('Bind source must exist: ' + volume.source)
        ws.tree(path)
        target = volume.target
        p = PurePosixPath(target)
        if not target.startswith('/') or target.startswith('//') or '\x00' in target or ('\\' in target) or ('..' in p.parts) or (str(p) != target):
            ws.deny('docker_mount_target_forbidden')
        if p == PurePosixPath('/') or any((p == PurePosixPath(x) or PurePosixPath(x) in p.parents for x in ['/proc', '/sys', '/dev'])):
            ws.deny('docker_mount_target_forbidden')
        if any((p == q or p in q.parents or q in p.parents for q in targets)):
            ws.deny('docker_mount_overlap')
        targets.append(p)
        mounts.append(Mount(target=target, source=str(path), type='bind', read_only=volume.read_only, propagation='rprivate'))
    return mounts

def incident(code):
    try:
        workspace().deny(code)
    except WorkspaceDenied as exc:
        return exc.incident_id

def remove_container(container):
    try:
        container.remove(force=True, v=True)
        return True
    except NotFound:
        return True
    except (APIError, RequestException):
        return False

def reap_expired(client):
    now = time.time()
    containers = client.containers.list(all=True, filters={'label': OWNER_KEY + '=' + OWNER}, ignore_removed=True)
    for c in containers:
        labels = c.attrs.get('Config', {}).get('Labels') or {}
        if not c.name.startswith(SLOT_PREFIX):
            continue
        try:
            deadline = float(labels[DEADLINE_KEY])
        except (KeyError, ValueError):
            continue
        if now > deadline + CLEANUP_GRACE and (not remove_container(c)):
            incident('docker_expired_cleanup_failed')
    return containers
PULL_CODE = "\nimport json,sys,docker\np=json.load(sys.stdin)\nc=docker.DockerClient(base_url=p['host'],version=p['version'],timeout=10)\ntry:\n    for item in c.api.pull(p['name'],stream=True,decode=True):\n        if item.get('error'): raise RuntimeError('pull failed')\nfinally: c.close()\n"

def pull_image(settings, name, version):
    check_network('docker_run')
    payload = json.dumps({'host': settings.host, 'version': version, 'name': name})
    try:
        done = subprocess.run([sys.executable, '-c', PULL_CODE], input=payload, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=settings.max_timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError('Image pull setup budget exceeded') from None
    if done.returncode:
        raise RuntimeError('Image pull failed')

def get_image(client, settings, name):
    try:
        return client.images.get(name)
    except ImageNotFound:
        if not settings.allow_pull:
            workspace().deny('docker_pull_forbidden')
    pull_image(settings, name, client.api._version)
    return client.images.get(name)

def check_image_volumes(image, mounts):
    declared = image.attrs.get('Config', {}).get('Volumes') or {}
    targets = {m['Target'] for m in mounts}
    if not set(declared).issubset(targets):
        workspace().deny('docker_image_volume_requires_bind')

def reserve(client, settings, image, data, mounts, seconds, token):
    reap_expired(client)
    existing = client.containers.list(all=True, filters={'label': OWNER_KEY + '=' + OWNER}, ignore_removed=True)
    for c in existing:
        labels = c.attrs.get('Config', {}).get('Labels') or {}
        if labels.get(LIMIT_KEY) != str(settings.maximum):
            workspace().deny('docker_capacity_configuration_mismatch')
    image_config = image.attrs.get('Config', {})
    cmd = data.command if data.command is not None else image_config.get('Cmd')
    ep = data.entrypoint if data.entrypoint is not None else image_config.get('Entrypoint')
    if ep == []:
        ep = ['']
    kwargs = dict(image=image.id, command=cmd, entrypoint=ep, environment=data.environment, mounts=mounts, network_mode='none' if data.network == 'isolated' else 'bridge', nano_cpus=settings.nano_cpus, mem_limit=settings.memory, memswap_limit=settings.memory, pids_limit=256, cap_drop=['ALL'], security_opt=['no-new-privileges:true'], privileged=False, detach=True, stdin_open=False, tty=False, auto_remove=False, restart_policy={'Name': 'no'}, healthcheck={'test': ['NONE']}, log_config=LogConfig(type='json-file', config={'max-size': '10m'} if settings.engine == 'podman' else {'max-size': '10m', 'max-file': '1'}))
    for slot in range(settings.maximum):
        deadline = time.time() + seconds
        labels = {OWNER_KEY: OWNER, TOKEN_KEY: token, DEADLINE_KEY: str(deadline), LIMIT_KEY: str(settings.maximum)}
        try:
            c = client.containers.create(name=SLOT_PREFIX + str(slot), labels=labels, **kwargs)
            return (c, deadline)
        except APIError as exc:
            if exc.status_code != 409:
                raise
    return (None, None)
WATCHDOG_BODY = "\nfrom twylt.guardrails import Workspace\nimport json,os,sys,time\nimport docker\nfrom docker.errors import NotFound\npayload=json.load(sys.stdin)\nclient=docker.DockerClient(base_url=payload['host'],version=payload['api_version'],timeout=10)\nprint('ready',flush=True)\nsys.stdout.close()\nwhile time.monotonic()<payload['monotonic_deadline']:\n    time.sleep(min(1,max(0,payload['monotonic_deadline']-time.monotonic())))\nreported=False\nwhile True:\n    try:\n        c=client.containers.get(payload['container_id'])\n        labels=c.attrs.get('Config',{}).get('Labels') or {}\n        if labels.get('io.twylt.run-token')!=payload['token'] or labels.get('io.twylt.app-runner')!='v1': break\n        if c.attrs.get('State',{}).get('Running'):\n            try: c.kill()\n            except NotFound: break\n        if time.monotonic()<payload['monotonic_deadline']+15:\n            time.sleep(min(1,max(0,payload['monotonic_deadline']+15-time.monotonic())))\n            continue\n        c.remove(force=True,v=True)\n        break\n    except NotFound:\n        break\n    except Exception:\n        if not reported:\n            try: Workspace('docker_run.watchdog').deny('docker_watchdog_cleanup_failed')\n            except Exception: pass\n            reported=True\n        time.sleep(5)\nclient.close()\n"

class Watchdog:

    def __init__(self, settings, client, container, deadline, token):
        payload = {'host': settings.host, 'api_version': client.api._version, 'container_id': container.id, 'deadline': deadline, 'monotonic_deadline': time.monotonic() + max(0, deadline - time.time()), 'token': token}
        self.process = subprocess.Popen([sys.executable, '-c', WATCHDOG_BODY], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, text=True)
        try:
            self.process.stdin.write(json.dumps(payload))
            self.process.stdin.close()
            ready, _, _ = select.select([self.process.stdout], [], [], 10)
            if not ready or self.process.stdout.readline().strip() != 'ready':
                raise RuntimeError('Watchdog did not acknowledge startup')
        except BaseException:
            self.cancel()
            raise
        finally:
            self.process.stdout.close()

    def cancel(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

def capture(container, stdout):
    stream = container.logs(stdout=stdout, stderr=not stdout, stream=True, follow=False)
    data = bytearray()
    truncated = False
    try:
        for chunk in stream:
            room = OUTPUT_LIMIT - len(data)
            data.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
                break
    finally:
        if hasattr(stream, 'close'):
            stream.close()
    return (bytes(data).decode('utf-8', errors='replace'), truncated)

def execute(data, client_factory=None, watchdog_factory=None):
    settings = Settings()
    ws = workspace()
    if data.network == 'enabled':
        check_network('docker_run')
    if settings.disable_network and data.network == 'enabled':
        ws.deny('docker_network_forbidden')
    name = settings.image_name(data.image)
    seconds = data.timeout if data.timeout is not None else min(60, settings.max_timeout)
    if seconds > settings.max_timeout:
        ws.deny('docker_timeout_exceeds_maximum')
    mounts = bind_mounts(data)
    output = Output(ok=False, status='error', image=name)
    client = None
    container = None
    watchdog = None
    started = time.monotonic()
    token = uuid.uuid4().hex
    factory = client_factory or (lambda: docker.DockerClient(base_url=settings.host, timeout=API_TIMEOUT, version=settings.api_version))
    guardian = watchdog_factory or Watchdog
    stage = 'connect'
    try:
        client = factory()
        info = client.info()
        if info.get('OSType') != 'linux':
            ws.deny('docker_local_linux_required')
        if settings.engine == 'podman' and info.get('Rootless') is True and (str(info.get('CgroupVersion')) != '2'):
            ws.deny('docker_resource_limits_unavailable')
        if not all((info.get(key) is True for key in ['MemoryLimit', 'SwapLimit', 'CpuCfsQuota'])):
            ws.deny('docker_resource_limits_unavailable')
        stage = 'image'
        image = get_image(client, settings, name)
        check_image_volumes(image, mounts)
        output.image_id = image.id
        mounts = bind_mounts(data)
        stage = 'reserve'
        container, deadline = reserve(client, settings, image, data, mounts, seconds, token)
        if container is None:
            output.status = 'busy'
            output.error = 'Container capacity exhausted; retry later.'
            return output
        run_deadline = time.monotonic() + max(0, deadline - time.time())
        output.container_id = container.id
        container.reload()
        if any((m.get('Type') != 'bind' for m in container.attrs.get('Mounts', []))):
            ws.deny('docker_non_bind_mount_forbidden')
        stage = 'watchdog'
        watchdog = guardian(settings, client, container, deadline, token)
        if time.monotonic() >= run_deadline:
            raise TimeoutError('Startup exceeded execution budget')
        stage = 'start'
        container.start()
        stage = 'wait'
        while True:
            remaining = run_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Execution budget exceeded')
            try:
                answer = container.wait(timeout=min(remaining, API_TIMEOUT))
                output.container_exit_code = int(answer['StatusCode'])
                break
            except (ReadTimeout,):
                continue
        output.timed_out = time.monotonic() >= run_deadline
        output.exit_code = 124 if output.timed_out else output.container_exit_code
        output.status = 'timed_out' if output.timed_out else 'succeeded' if output.exit_code == 0 else 'failed'
        container.reload()
        output.oom_killed = bool(container.attrs.get('State', {}).get('OOMKilled'))
        stage = 'logs'
        output.stdout, output.stdout_truncated = capture(container, True)
        output.stderr, output.stderr_truncated = capture(container, False)
        output.ok = output.status == 'succeeded'
    except WorkspaceDenied:
        raise
    except TimeoutError:
        output.status = 'timed_out'
        output.timed_out = True
        output.exit_code = 124
        output.error = 'Timeout during ' + stage
        if container is not None:
            try:
                container.kill()
                container.reload()
                output.container_exit_code = container.attrs.get('State', {}).get('ExitCode')
                output.oom_killed = bool(container.attrs.get('State', {}).get('OOMKilled'))
                output.stdout, output.stdout_truncated = capture(container, True)
                output.stderr, output.stderr_truncated = capture(container, False)
            except (APIError, RequestException):
                pass
    except (APIError, RequestException, RuntimeError, OSError, ValueError) as exc:
        output.status = 'error'
        output.ok = False
        output.error = 'Docker operation failed during ' + stage + ' (' + type(exc).__name__ + ')'
    finally:
        if container is not None:
            output.cleanup_ok = remove_container(container)
            if output.cleanup_ok:
                if watchdog is not None:
                    watchdog.cancel()
            else:
                output.ok = False
                output.status = 'error'
                output.error = 'Container cleanup failed; watchdog/reaper will retry. Incident ' + incident('docker_cleanup_failed')
        if client is not None:
            client.close()
        output.duration_seconds = round(time.monotonic() - started, 3)
    return output

class DockerRunTool(Tool[Input, Output]):
    input_model = Input
    output_model = Output
    name = 'docker_run'
    version = '0.4.0'
    description = 'Run an application in a disposable resource-limited Linux Docker/Podman container, with scoped host bind mounts and daemon-wide concurrency slots.'
    requirements = Requirements(tool='pip', format='requirements.txt', content='twylt>=1.1.1,<2\npydantic>=2,<3\ndocker==7.1.0\n')
    few_shots = [{'input': {'image': 'alpine:3.20', 'command': ['echo', 'hello'], 'network': 'isolated', 'timeout': 10}, 'output': {'ok': True, 'status': 'succeeded', 'image': 'docker.io/library/alpine:3.20', 'stdout': 'hello\n', 'cleanup_ok': True, 'exit_code': 0, 'container_exit_code': 0}}]
    input_schema_name = 'docker_run.input'
    input_schema_version = '1.0.0'
    output_schema_name = 'docker_run.output'
    output_schema_version = '1.0.0'

    def biz(self, data):
        with Workspace(self.name):
            return execute(data)
TOOL = DockerRunTool
if __name__ == '__main__':
    DockerRunTool.run()
