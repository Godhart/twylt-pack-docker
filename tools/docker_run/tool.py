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
"""Shared workspace policy 1.0.0. Embedded verbatim in standalone tools.
Explicit path confinement, not an OS sandbox. Environment is trusted configuration.
"""
import contextvars
import datetime as _datetime
import json as _json
import os as _os
from pathlib import Path as _Path
import stat as _stat
import sys as _sys
import uuid as _uuid

_POLICY = contextvars.ContextVar('twylt_workspace', default=None)

class WorkspaceDenied(ValueError):
    """A policy denial, already recorded in the incident sink."""
    def __init__(self, code, incident_id):
        self.code = code
        self.incident_id = incident_id
        super().__init__(f'{code}: access denied (incident {incident_id})')

class Workspace:
    def __init__(self, tool='unknown'):
        self.tool = tool
        self.log_path = None
        self.root = None
        value = _os.environ.get('TWYLT_WORKSPACE_ROOT', '')
        if not value or not _Path(value).is_absolute():
            self.deny('workspace_not_configured')
        candidate = _Path(_os.path.abspath(value))
        # The root and all of its ancestors must be real directories.
        try:
            for part in [*reversed(candidate.parents), candidate]:
                st = part.lstat()
                if self._link(st) or not _stat.S_ISDIR(st.st_mode):
                    self.deny('invalid_workspace_root')
            if candidate.parent == candidate:
                self.deny('filesystem_root_forbidden')
        except OSError:
            self.deny('invalid_workspace_root')
        self.root = candidate
        configured_log = _os.environ.get('TWYLT_INCIDENT_LOG', '')
        if configured_log:
            p = _Path(configured_log)
            if not p.is_absolute(): self.deny('invalid_incident_log')
            p = _Path(_os.path.abspath(p))
            if self.contains(p): self.deny('incident_log_inside_workspace')
            try:
                for parent in [*reversed(p.parent.parents), p.parent]:
                    st = parent.lstat()
                    if self._link(st) or not _stat.S_ISDIR(st.st_mode):
                        self.deny('invalid_incident_log')
                self.log_path = p
                fd = self._open_log()
                _os.close(fd)
            except WorkspaceDenied:
                raise
            except (OSError, ValueError):
                self.log_path = None
                self.deny('incident_log_unavailable')

    @staticmethod
    def _link(st):
        # Includes Windows junctions/reparse points even on Python without is_junction().
        return _stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)

    def contains(self, path):
        return self.root is not None and (path == self.root or self.root in path.parents)

    def _open_log(self):
        p = self.log_path
        if p.exists() or p.is_symlink():
            st = p.lstat()
            if self._link(st) or not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise ValueError('unsafe audit sink')
        flags = _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT | getattr(_os, 'O_NOFOLLOW', 0) | getattr(_os, 'O_CLOEXEC', 0) | getattr(_os, 'O_NONBLOCK', 0)
        fd = _os.open(p, flags, 0o600)
        try:
            st = _os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise ValueError('unsafe audit sink')
        except BaseException:
            _os.close(fd)
            raise
        return fd

    def deny(self, code, requested=None):
        identifier = str(_uuid.uuid4())
        event = {'event': 'workspace_incident', 'schema_version': '1.0',
                 'timestamp': _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
                 'incident_id': identifier, 'tool': self.tool, 'pid': _os.getpid(),
                 'action': 'deny', 'code': code}
        if requested is not None:
            # Only explicit path values, never file contents or remote URLs/credentials.
            event['requested_path'] = str(requested)[:1024]
        payload = (_json.dumps(event, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')
        try:
            if self.log_path is None:
                _sys.stderr.write(payload.decode('utf-8')); _sys.stderr.flush()
            else:
                fd = self._open_log()
                try:
                    if _os.write(fd, payload) != len(payload): raise OSError('short audit write')
                    _os.fsync(fd)
                finally: _os.close(fd)
        except (OSError, ValueError):
            event['original_code'] = code
            event['code'] = code = 'incident_log_unavailable'
            _sys.stderr.write(_json.dumps(event, ensure_ascii=True) + '\n'); _sys.stderr.flush()
        raise WorkspaceDenied(code, identifier)

    def inspect(self, path, allow_leaf_link=False):
        """Check physical path components without following any link."""
        path = _Path(path)
        if not self.contains(path): self.deny('path_outside_workspace')
        current = self.root
        parts = path.relative_to(self.root).parts
        for i, part in enumerate(parts):
            current = current / part
            try: st = current.lstat()
            except FileNotFoundError: break
            if self._link(st):
                if allow_leaf_link and i == len(parts)-1: return path
                self.deny('symlink_forbidden', self.virtual_unchecked(path))
            if _stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                self.deny('hardlink_forbidden', self.virtual_unchecked(path))
            if not (_stat.S_ISREG(st.st_mode) or _stat.S_ISDIR(st.st_mode)):
                self.deny('special_file_forbidden', self.virtual_unchecked(path))
            if st.st_dev != self.root.stat().st_dev:
                self.deny('mount_boundary_forbidden', self.virtual_unchecked(path))
        return path

    def resolve(self, value, base=None):
        """POSIX virtual / is root; relative paths are root-based unless base is explicit."""
        if not isinstance(value, str) or not value or '\x00' in value:
            self.deny('invalid_path')
        if '\\' in value or ':' in value or value.startswith('//') or value.startswith('~'):
            self.deny('invalid_path_syntax', value)
        parts = value.split('/')
        if '..' in parts: self.deny('path_outside_workspace', value)
        if _os.name == 'nt':
            reserved = {'CON','PRN','AUX','NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}
            if any(p.endswith((' ', '.')) or p.split('.')[0].upper() in reserved for p in parts if p not in {'', '.'}):
                self.deny('invalid_path_syntax', value)
        start = self.root if value.startswith('/') or base is None else base
        path = start.joinpath(*(p for p in parts if p not in {'', '.'}))
        return self.inspect(path)

    def virtual_unchecked(self, path):
        relative = _Path(path).relative_to(self.root).as_posix()
        return '/' if relative == '.' else '/' + relative

    def virtual(self, path):
        if not self.contains(_Path(path)): self.deny('path_outside_workspace')
        return self.virtual_unchecked(path)

    def protect_root(self, path):
        if path == self.root: self.deny('workspace_root_mutation_forbidden', '/')

    def tree(self, path):
        """Preflight every descendant before a recursive mutation."""
        self.inspect(path)
        if not path.is_dir(): return
        stack = [path]
        while stack:
            parent = stack.pop()
            with _os.scandir(parent) as children:
                for child in children:
                    p = self.inspect(_Path(child.path))
                    if p.is_dir(): stack.append(p)

    def redact(self, value):
        if self.root is None: return str(value)
        return str(value).replace(str(self.root) + _os.sep, '/').replace(str(self.root), '/')

    def __enter__(self):
        self._token = _POLICY.set(self)
        return self

    def __exit__(self, typ, exc, tb):
        _POLICY.reset(self._token)
        # Remove host workspace prefixes from errors returned by business code.
        if exc is not None and not isinstance(exc, WorkspaceDenied):
            if isinstance(exc, OSError):
                if exc.filename: exc.filename = self.redact(exc.filename)
                if exc.filename2: exc.filename2 = self.redact(exc.filename2)
            exc.args = tuple(self.redact(a) if isinstance(a, str) else a for a in exc.args)
        return False

def workspace():
    active = _POLICY.get()
    return active if active is not None else Workspace()

class WorkspaceTransport:
    """Guard TWYLT 1.0.0's implicit input.json/output.json before its file I/O."""
    @classmethod
    def _transport_payload(cls):
        payload, source = super()._transport_payload()
        if source is None and not _os.environ.get('INPUT_DESCRIBE', ''):
            with Workspace(cls.name) as ws:
                cwd = _Path.cwd()
                if not ws.contains(cwd): ws.deny('transport_cwd_outside_workspace')
                ws.inspect(cwd)
                for attribute in ['input_path', 'output_path']:
                    path = _Path(getattr(cls, attribute))
                    full = path if path.is_absolute() else cwd/path
                    setattr(cls, attribute, ws.inspect(full))
        return payload, source

    @classmethod
    def _write_output(cls, value):
        with Workspace(cls.name) as ws:
            ws.inspect(_Path(cls.output_path))
            return super()._write_output(value)

WORKSPACE_SOURCE = '"""Shared workspace policy 1.0.0. Embedded verbatim in standalone tools.\nExplicit path confinement, not an OS sandbox. Environment is trusted configuration.\n"""\nimport contextvars\nimport datetime as _datetime\nimport json as _json\nimport os as _os\nfrom pathlib import Path as _Path\nimport stat as _stat\nimport sys as _sys\nimport uuid as _uuid\n\n_POLICY = contextvars.ContextVar(\'twylt_workspace\', default=None)\n\nclass WorkspaceDenied(ValueError):\n    """A policy denial, already recorded in the incident sink."""\n    def __init__(self, code, incident_id):\n        self.code = code\n        self.incident_id = incident_id\n        super().__init__(f\'{code}: access denied (incident {incident_id})\')\n\nclass Workspace:\n    def __init__(self, tool=\'unknown\'):\n        self.tool = tool\n        self.log_path = None\n        self.root = None\n        value = _os.environ.get(\'TWYLT_WORKSPACE_ROOT\', \'\')\n        if not value or not _Path(value).is_absolute():\n            self.deny(\'workspace_not_configured\')\n        candidate = _Path(_os.path.abspath(value))\n        # The root and all of its ancestors must be real directories.\n        try:\n            for part in [*reversed(candidate.parents), candidate]:\n                st = part.lstat()\n                if self._link(st) or not _stat.S_ISDIR(st.st_mode):\n                    self.deny(\'invalid_workspace_root\')\n            if candidate.parent == candidate:\n                self.deny(\'filesystem_root_forbidden\')\n        except OSError:\n            self.deny(\'invalid_workspace_root\')\n        self.root = candidate\n        configured_log = _os.environ.get(\'TWYLT_INCIDENT_LOG\', \'\')\n        if configured_log:\n            p = _Path(configured_log)\n            if not p.is_absolute(): self.deny(\'invalid_incident_log\')\n            p = _Path(_os.path.abspath(p))\n            if self.contains(p): self.deny(\'incident_log_inside_workspace\')\n            try:\n                for parent in [*reversed(p.parent.parents), p.parent]:\n                    st = parent.lstat()\n                    if self._link(st) or not _stat.S_ISDIR(st.st_mode):\n                        self.deny(\'invalid_incident_log\')\n                self.log_path = p\n                fd = self._open_log()\n                _os.close(fd)\n            except WorkspaceDenied:\n                raise\n            except (OSError, ValueError):\n                self.log_path = None\n                self.deny(\'incident_log_unavailable\')\n\n    @staticmethod\n    def _link(st):\n        # Includes Windows junctions/reparse points even on Python without is_junction().\n        return _stat.S_ISLNK(st.st_mode) or bool(getattr(st, \'st_file_attributes\', 0) & 0x400)\n\n    def contains(self, path):\n        return self.root is not None and (path == self.root or self.root in path.parents)\n\n    def _open_log(self):\n        p = self.log_path\n        if p.exists() or p.is_symlink():\n            st = p.lstat()\n            if self._link(st) or not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:\n                raise ValueError(\'unsafe audit sink\')\n        flags = _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT | getattr(_os, \'O_NOFOLLOW\', 0) | getattr(_os, \'O_CLOEXEC\', 0) | getattr(_os, \'O_NONBLOCK\', 0)\n        fd = _os.open(p, flags, 0o600)\n        try:\n            st = _os.fstat(fd)\n            if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:\n                raise ValueError(\'unsafe audit sink\')\n        except BaseException:\n            _os.close(fd)\n            raise\n        return fd\n\n    def deny(self, code, requested=None):\n        identifier = str(_uuid.uuid4())\n        event = {\'event\': \'workspace_incident\', \'schema_version\': \'1.0\',\n                 \'timestamp\': _datetime.datetime.now(_datetime.timezone.utc).isoformat(),\n                 \'incident_id\': identifier, \'tool\': self.tool, \'pid\': _os.getpid(),\n                 \'action\': \'deny\', \'code\': code}\n        if requested is not None:\n            # Only explicit path values, never file contents or remote URLs/credentials.\n            event[\'requested_path\'] = str(requested)[:1024]\n        payload = (_json.dumps(event, ensure_ascii=True, separators=(\',\', \':\')) + \'\\n\').encode(\'utf-8\')\n        try:\n            if self.log_path is None:\n                _sys.stderr.write(payload.decode(\'utf-8\')); _sys.stderr.flush()\n            else:\n                fd = self._open_log()\n                try:\n                    if _os.write(fd, payload) != len(payload): raise OSError(\'short audit write\')\n                    _os.fsync(fd)\n                finally: _os.close(fd)\n        except (OSError, ValueError):\n            event[\'original_code\'] = code\n            event[\'code\'] = code = \'incident_log_unavailable\'\n            _sys.stderr.write(_json.dumps(event, ensure_ascii=True) + \'\\n\'); _sys.stderr.flush()\n        raise WorkspaceDenied(code, identifier)\n\n    def inspect(self, path, allow_leaf_link=False):\n        """Check physical path components without following any link."""\n        path = _Path(path)\n        if not self.contains(path): self.deny(\'path_outside_workspace\')\n        current = self.root\n        parts = path.relative_to(self.root).parts\n        for i, part in enumerate(parts):\n            current = current / part\n            try: st = current.lstat()\n            except FileNotFoundError: break\n            if self._link(st):\n                if allow_leaf_link and i == len(parts)-1: return path\n                self.deny(\'symlink_forbidden\', self.virtual_unchecked(path))\n            if _stat.S_ISREG(st.st_mode) and st.st_nlink > 1:\n                self.deny(\'hardlink_forbidden\', self.virtual_unchecked(path))\n            if not (_stat.S_ISREG(st.st_mode) or _stat.S_ISDIR(st.st_mode)):\n                self.deny(\'special_file_forbidden\', self.virtual_unchecked(path))\n            if st.st_dev != self.root.stat().st_dev:\n                self.deny(\'mount_boundary_forbidden\', self.virtual_unchecked(path))\n        return path\n\n    def resolve(self, value, base=None):\n        """POSIX virtual / is root; relative paths are root-based unless base is explicit."""\n        if not isinstance(value, str) or not value or \'\\x00\' in value:\n            self.deny(\'invalid_path\')\n        if \'\\\\\' in value or \':\' in value or value.startswith(\'//\') or value.startswith(\'~\'):\n            self.deny(\'invalid_path_syntax\', value)\n        parts = value.split(\'/\')\n        if \'..\' in parts: self.deny(\'path_outside_workspace\', value)\n        if _os.name == \'nt\':\n            reserved = {\'CON\',\'PRN\',\'AUX\',\'NUL\', *(\'COM\'+str(i) for i in range(1,10)), *(\'LPT\'+str(i) for i in range(1,10))}\n            if any(p.endswith((\' \', \'.\')) or p.split(\'.\')[0].upper() in reserved for p in parts if p not in {\'\', \'.\'}):\n                self.deny(\'invalid_path_syntax\', value)\n        start = self.root if value.startswith(\'/\') or base is None else base\n        path = start.joinpath(*(p for p in parts if p not in {\'\', \'.\'}))\n        return self.inspect(path)\n\n    def virtual_unchecked(self, path):\n        relative = _Path(path).relative_to(self.root).as_posix()\n        return \'/\' if relative == \'.\' else \'/\' + relative\n\n    def virtual(self, path):\n        if not self.contains(_Path(path)): self.deny(\'path_outside_workspace\')\n        return self.virtual_unchecked(path)\n\n    def protect_root(self, path):\n        if path == self.root: self.deny(\'workspace_root_mutation_forbidden\', \'/\')\n\n    def tree(self, path):\n        """Preflight every descendant before a recursive mutation."""\n        self.inspect(path)\n        if not path.is_dir(): return\n        stack = [path]\n        while stack:\n            parent = stack.pop()\n            with _os.scandir(parent) as children:\n                for child in children:\n                    p = self.inspect(_Path(child.path))\n                    if p.is_dir(): stack.append(p)\n\n    def redact(self, value):\n        if self.root is None: return str(value)\n        return str(value).replace(str(self.root) + _os.sep, \'/\').replace(str(self.root), \'/\')\n\n    def __enter__(self):\n        self._token = _POLICY.set(self)\n        return self\n\n    def __exit__(self, typ, exc, tb):\n        _POLICY.reset(self._token)\n        # Remove host workspace prefixes from errors returned by business code.\n        if exc is not None and not isinstance(exc, WorkspaceDenied):\n            if isinstance(exc, OSError):\n                if exc.filename: exc.filename = self.redact(exc.filename)\n                if exc.filename2: exc.filename2 = self.redact(exc.filename2)\n            exc.args = tuple(self.redact(a) if isinstance(a, str) else a for a in exc.args)\n        return False\n\ndef workspace():\n    active = _POLICY.get()\n    return active if active is not None else Workspace()\n\nclass WorkspaceTransport:\n    """Guard TWYLT 1.0.0\'s implicit input.json/output.json before its file I/O."""\n    @classmethod\n    def _transport_payload(cls):\n        payload, source = super()._transport_payload()\n        if source is None and not _os.environ.get(\'INPUT_DESCRIBE\', \'\'):\n            with Workspace(cls.name) as ws:\n                cwd = _Path.cwd()\n                if not ws.contains(cwd): ws.deny(\'transport_cwd_outside_workspace\')\n                ws.inspect(cwd)\n                for attribute in [\'input_path\', \'output_path\']:\n                    path = _Path(getattr(cls, attribute))\n                    full = path if path.is_absolute() else cwd/path\n                    setattr(cls, attribute, ws.inspect(full))\n        return payload, source\n\n    @classmethod\n    def _write_output(cls, value):\n        with Workspace(cls.name) as ws:\n            ws.inspect(_Path(cls.output_path))\n            return super()._write_output(value)\n'

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


class DockerRunTool(WorkspaceTransport, Tool[Input, Output]):
    input_model = Input
    output_model = Output
    name = 'docker_run'
    version = '0.3.0'
    description = 'Run an application in a disposable resource-limited Linux Docker/Podman container, with scoped host bind mounts and daemon-wide concurrency slots.'
    requirements = Requirements(tool='pip', format='requirements.txt', content='twylt==1.0.0\npydantic>=2,<3\ndocker==7.1.0\n')
    few_shots = [{'input': {'image':'alpine:3.20','command':['echo','hello'],'network':'isolated','timeout':10}, 'output': {'ok':True,'status':'succeeded','image':'docker.io/library/alpine:3.20','stdout':'hello\n','cleanup_ok':True,'exit_code':0,'container_exit_code':0}}]
    input_schema_name = 'docker_run.input'
    input_schema_version = '1.0.0'
    output_schema_name = 'docker_run.output'
    output_schema_version = '1.0.0'
    def biz(self, data):
        with Workspace(self.name):
            return execute(data)

if __name__ == '__main__':
    DockerRunTool.run()
