from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
policy=(BASE/'src/workspace.py').read_text()
source=(BASE/'src/runtime.py').read_text().replace('from workspace import Workspace, WorkspaceDenied, WorkspaceTransport, workspace',policy)
source=source.replace("WORKSPACE_SOURCE = Path(__file__).with_name('workspace.py').read_text(encoding='utf-8')",'WORKSPACE_SOURCE = '+repr(policy))
code=source+'''

class DockerRunTool(WorkspaceTransport, Tool[Input, Output]):
    input_model = Input
    output_model = Output
    name = 'docker_run'
    version = '0.3.0'
    description = 'Run an application in a disposable resource-limited Linux Docker/Podman container, with scoped host bind mounts and daemon-wide concurrency slots.'
    requirements = Requirements(tool='pip', format='requirements.txt', content='twylt==1.0.0\\npydantic>=2,<3\\ndocker==7.1.0\\n')
    few_shots = [{'input': {'image':'alpine:3.20','command':['echo','hello'],'network':'isolated','timeout':10}, 'output': {'ok':True,'status':'succeeded','image':'docker.io/library/alpine:3.20','stdout':'hello\\n','cleanup_ok':True,'exit_code':0,'container_exit_code':0}}]
    input_schema_name = 'docker_run.input'
    input_schema_version = '1.0.0'
    output_schema_name = 'docker_run.output'
    output_schema_version = '1.0.0'
    def biz(self, data):
        with Workspace(self.name):
            return execute(data)

if __name__ == '__main__':
    DockerRunTool.run()
'''
folder=BASE/'tools/docker_run';folder.mkdir(parents=True,exist_ok=True)
(folder/'tool.py').write_text(code)
(folder/'run.py').write_text('from pathlib import Path\nfrom twylt.bootstrap import run_tool_file\n\nif __name__ == "__main__":\n    run_tool_file(Path(__file__).with_name("tool.py"))\n')
(folder/'example.json').write_text('{"image":"alpine:3.20","command":["echo","hello"],"network":"isolated","timeout":10}\n')

images_code=source+'''

class DockerImagesTool(WorkspaceTransport, Tool[ImagesInput, ImagesOutput]):
    input_model = ImagesInput
    output_model = ImagesOutput
    name = 'docker_images'
    version = '0.3.0'
    description = 'List local tagged Docker/Podman images with sorted tags, byte sizes, creation dates and pagination. Never pulls images or starts containers.'
    requirements = Requirements(tool='pip', format='requirements.txt', content='twylt==1.0.0\\npydantic>=2,<3\\ndocker==7.1.0\\n')
    few_shots = [{'input': {'offset':0,'limit':100}, 'output': {'ok':True,'images':[],'total':0,'offset':0,'limit':100,'next_offset':None,'error':None}}]
    input_schema_name = 'docker_images.input'
    input_schema_version = '1.0.0'
    output_schema_name = 'docker_images.output'
    output_schema_version = '1.0.0'
    def biz(self, data):
        with Workspace(self.name):
            return list_images(data)

if __name__ == '__main__':
    DockerImagesTool.run()
'''
folder=BASE/'tools/docker_images';folder.mkdir(parents=True,exist_ok=True)
(folder/'tool.py').write_text(images_code)
(folder/'run.py').write_text((BASE/'tools/docker_run/run.py').read_text())
(folder/'example.json').write_text('{"offset":0,"limit":100}\n')
