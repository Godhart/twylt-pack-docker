from __future__ import annotations
import os
import re
from decimal import Decimal, InvalidOperation
from pydantic import BaseModel, ConfigDict
from twylt.guardrails import WorkspaceDenied, workspace

API_TIMEOUT = 10

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Settings:

    def __init__(self):
        ws = workspace()

        def invalid():
            ws.deny('docker_configuration_invalid')
        flag = os.environ.get('TWYLT_DOCKER_ALLOW_PULL', 'false').lower()
        if flag not in {'true', 'false', '1', '0'}:
            invalid()
        self.allow_pull = flag in {'true', '1'}
        flag = os.environ.get('TWYLT_DOCKER_DISABLE_NETWORK', 'false').lower()
        if flag not in {'true', 'false', '1', '0'}:
            invalid()
        self.disable_network = flag in {'true', '1'}
        self.registry = os.environ.get('TWYLT_DOCKER_REGISTRY', 'docker.io').rstrip('/')
        if not re.fullmatch('(?:localhost|[a-z0-9]+(?:[.-][a-z0-9]+)*)(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*', self.registry):
            invalid()
        try:
            cpus = Decimal(os.environ.get('TWYLT_DOCKER_CPUS', '1'))
            if not cpus.is_finite() or not Decimal('0.01') <= cpus <= 1024:
                invalid()
            self.nano_cpus = int(cpus * 1000000000)
            memory = os.environ.get('TWYLT_DOCKER_MEMORY', '512m').lower()
            match = re.fullmatch('([1-9][0-9]*)([bkmg]?)', memory)
            if match is None:
                invalid()
            self.memory = int(match[1]) * {'': 1, 'b': 1, 'k': 1024, 'm': 1024 ** 2, 'g': 1024 ** 3}[match[2]]
            if not 6 * 1024 ** 2 <= self.memory <= 1024 ** 4:
                invalid()
            self.maximum = int(os.environ.get('TWYLT_DOCKER_MAX_CONTAINERS', '2'))
            self.max_timeout = int(os.environ.get('TWYLT_DOCKER_MAX_TIMEOUT', '300'))
            if not 1 <= self.maximum <= 4096 or not 1 <= self.max_timeout <= 86400:
                invalid()
        except WorkspaceDenied:
            raise
        except (InvalidOperation, ValueError):
            invalid()
        self.engine = os.environ.get('TWYLT_CONTAINER_ENGINE', 'docker').lower()
        if self.engine not in {'docker', 'podman'}:
            invalid()
        self.api_version = '1.40' if self.engine == 'podman' else 'auto'
        if self.engine == 'docker':
            default_host = 'unix:///var/run/docker.sock'
            host_variable = 'DOCKER_HOST'
        else:
            host_variable = 'CONTAINER_HOST'
            if os.geteuid() == 0:
                default_host = 'unix:///run/podman/podman.sock'
            else:
                runtime_dir = os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.geteuid()}')
                if not runtime_dir.startswith('/') or '\x00' in runtime_dir:
                    invalid()
                default_host = 'unix://' + runtime_dir.rstrip('/') + '/podman/podman.sock'
        self.host = os.environ.get('TWYLT_CONTAINER_HOST', os.environ.get(host_variable, default_host))
        if os.name != 'posix' or not self.host.startswith('unix:///') or '\x00' in self.host:
            ws.deny('docker_local_linux_required')

    def image_name(self, value):
        ws = workspace()
        if not value or any((c.isspace() for c in value)) or '://' in value or (value.count('@') > 1):
            ws.deny('docker_image_forbidden')
        name = value
        if '@' in value:
            name, digest = value.rsplit('@', 1)
            if not re.fullmatch('sha256:[a-f0-9]{64}', digest):
                ws.deny('docker_image_forbidden')
        repository = name
        if name.rfind(':') > name.rfind('/'):
            repository, tag = name.rsplit(':', 1)
            if not re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag):
                ws.deny('docker_image_forbidden')
        parts = repository.split('/')
        first = parts[0]
        qualified = len(parts) > 1 and ('.' in first or ':' in first or first == 'localhost')
        if qualified and (not re.fullmatch('(?:localhost|[a-z0-9]+(?:[.-][a-z0-9]+)*)(?::[0-9]{1,5})?', first)):
            ws.deny('docker_image_forbidden')
        names = parts[1:] if qualified else parts
        if not names or any((not re.fullmatch('[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*', part) for part in names)):
            ws.deny('docker_image_forbidden')
        if qualified:
            if not value.startswith(self.registry + '/'):
                ws.deny('docker_registry_forbidden')
            result = value
        else:
            prefix = self.registry
            if prefix == 'docker.io' and '/' not in name:
                prefix += '/library'
            result = prefix + '/' + value
        return result
