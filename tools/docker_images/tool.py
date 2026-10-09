from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared'))
import docker
from docker.errors import APIError
from requests.exceptions import RequestException
from pydantic import Field
from twylt import Tool, Requirements
from twylt.guardrails import Workspace
from docker_common.common import API_TIMEOUT, Model, Settings

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
    settings = Settings()
    result = ImagesOutput(ok=False, offset=data.offset, limit=data.limit)
    client = None
    factory = client_factory or (lambda: docker.DockerClient(base_url=settings.host, timeout=API_TIMEOUT, version=settings.api_version))
    try:
        client = factory()
        snapshot = client.api.images(all=True)
        grouped = {}
        for item in snapshot:
            tags = {tag for tag in item.get('RepoTags') or [] if isinstance(tag, str) and '@' not in tag and (':' in tag.rsplit('/', 1)[-1]) and ('<none>' not in tag) and tag.rsplit(':', 1)[-1]}
            if not tags:
                continue
            image_id = item['Id']
            if image_id in grouped:
                grouped[image_id].tags = sorted(set(grouped[image_id].tags) | tags)
            else:
                created = item.get('Created')
                if isinstance(created, (int, float)):
                    from datetime import datetime, timezone
                    created = datetime.fromtimestamp(created, timezone.utc).isoformat().replace('+00:00', 'Z')
                grouped[image_id] = ImageRecord(image_id=image_id, tags=sorted(tags), size_bytes=item['Size'], created=created)
        ordered = sorted(grouped.values(), key=lambda row: (row.tags[0], row.image_id))
        result.total = len(ordered)
        result.images = ordered[data.offset:data.offset + data.limit]
        if data.offset + data.limit < result.total:
            result.next_offset = data.offset + data.limit
        result.ok = True
    except (APIError, RequestException, RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        result.error = 'Docker image listing failed: ' + type(exc).__name__
    finally:
        if client is not None:
            client.close()
    return result

class DockerImagesTool(Tool[ImagesInput, ImagesOutput]):
    input_model = ImagesInput
    output_model = ImagesOutput
    name = 'docker_images'
    version = '0.4.0'
    description = 'List local tagged Docker/Podman images with sorted tags, byte sizes, creation dates and pagination. Never pulls images or starts containers.'
    requirements = Requirements(tool='pip', format='requirements.txt', content='twylt>=1.1.1,<2\npydantic>=2,<3\ndocker==7.1.0\n')
    few_shots = [{'input': {'offset': 0, 'limit': 100}, 'output': {'ok': True, 'images': [], 'total': 0, 'offset': 0, 'limit': 100, 'next_offset': None, 'error': None}}]
    input_schema_name = 'docker_images.input'
    input_schema_version = '1.0.0'
    output_schema_name = 'docker_images.output'
    output_schema_version = '1.0.0'

    def biz(self, data):
        with Workspace(self.name):
            return list_images(data)
TOOL = DockerImagesTool
if __name__ == '__main__':
    DockerImagesTool.run()
