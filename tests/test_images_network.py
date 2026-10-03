import json
from types import SimpleNamespace
import pytest
from test_runtime import setup, execute, r, Workspace, WorkspaceDenied

@pytest.mark.parametrize('value',['true','1','TRUE'])
def test_network_denied_before_client(setup,monkeypatch,value):
    root,engine=setup
    monkeypatch.setenv('TWYLT_DOCKER_DISABLE_NETWORK',value)
    def forbidden(): pytest.fail('must reject before daemon access or pull')
    with Workspace('docker_run'),pytest.raises(WorkspaceDenied):
        r.execute(r.Input(image='alpine',network='enabled'),client_factory=forbidden)
    events=[json.loads(line) for line in (root.parent/'audit.jsonl').read_text().splitlines()]
    assert len(events)==1 and events[0]['code']=='docker_network_forbidden'
    assert events[0]['tool']=='docker_run'

@pytest.mark.parametrize('value',['true','1','false','0'])
def test_isolated_always_none(setup,monkeypatch,value):
    root,engine=setup
    monkeypatch.setenv('TWYLT_DOCKER_DISABLE_NETWORK',value)
    assert execute(engine).ok
    assert engine.created[0].kwargs['network_mode']=='none'

@pytest.mark.parametrize('value',['false','0'])
def test_enabled_when_policy_permits(setup,monkeypatch,value):
    root,engine=setup
    monkeypatch.setenv('TWYLT_DOCKER_DISABLE_NETWORK',value)
    assert execute(engine,network='enabled').ok
    assert engine.created[0].kwargs['network_mode']=='bridge'

@pytest.mark.parametrize('value',['yes','','no','2'])
def test_invalid_flag_fails_closed(setup,monkeypatch,value):
    root,engine=setup
    monkeypatch.setenv('TWYLT_DOCKER_DISABLE_NETWORK',value)
    with pytest.raises(WorkspaceDenied):execute(engine)
    assert not engine.created
    assert json.loads((root.parent/'audit.jsonl').read_text().splitlines()[0])['code']=='docker_configuration_invalid'

class Client:
    def __init__(self,rows=None,error=None):
        self.rows=rows;self.error=error;self.closed=False;self.calls=[]
        self.api=SimpleNamespace(images=self.images)
    def images(self,**kwargs):
        self.calls.append(kwargs)
        if self.error:raise self.error
        return self.rows
    def close(self):self.closed=True

def row(id,tags):return dict(Id=id,RepoTags=tags,Size=1234,Created=0)
def listing(client,**kwargs):
    with Workspace('docker_images'):
        return r.list_images(r.ImagesInput(**kwargs),client_factory=lambda:client)

def test_tag_filter_grouping_sorting_and_pagination(setup,monkeypatch):
    monkeypatch.setenv('TWYLT_DOCKER_DISABLE_NETWORK','true')
    monkeypatch.setenv('TWYLT_DOCKER_REGISTRY','private.example/team')
    client=Client([row('b',['z:latest','z:latest','z@sha256:abc']),
                   row('c',['<none>:<none>']),row('d',None),row('e',[]),
                   row('f',['registry:5000/repo','repo@sha256:abc']),
                   row('a',['outside.example/a:v2','outside.example/a:v1']),
                   row('a',['outside.example/a:extra'])])
    result=listing(client,limit=1)
    assert result.ok and result.total==2 and result.next_offset==1
    image=result.images[0]
    assert image.image_id=='a' and image.tags==['outside.example/a:extra','outside.example/a:v1','outside.example/a:v2']
    assert image.size_bytes==1234 and image.created=='1970-01-01T00:00:00Z'
    assert client.closed and client.calls==[{'all':True}]
    result=listing(client,offset=1,limit=1)
    assert result.images[0].tags==['z:latest'] and result.next_offset is None
    result=listing(client,offset=20)
    assert result.ok and result.images==[] and result.total==2 and result.next_offset is None

def test_no_tagged_images(setup):
    result=listing(Client([row('a',None)]))
    assert result.ok and result.total==0 and result.images==[]

def test_listing_failure_closes_and_redacts(setup):
    client=Client(error=r.APIError('secret daemon details'))
    result=listing(client)
    assert not result.ok and client.closed
    assert result.error=='Docker image listing failed: APIError'

@pytest.mark.parametrize('kwargs',[{'offset':-1},{'limit':0},{'limit':1001},{'limit':True},{'unknown':1}])
def test_pagination_validation(kwargs):
    with pytest.raises(ValueError):r.ImagesInput(**kwargs)
