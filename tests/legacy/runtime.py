from pathlib import Path
import importlib.util,sys
BASE=Path(__file__).resolve().parents[2]
for path in sorted((BASE/'tools').glob('*/tool.py')):
    spec=importlib.util.spec_from_file_location('legacy_'+path.parent.name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    globals().update({k:v for k,v in vars(module).items() if not k.startswith('__') and k not in {'TOOL'}})

from twylt.guardrails import Workspace, WorkspaceDenied, workspace
import types
class TestFacade(types.ModuleType):
    def __setattr__(self,name,value):
        super().__setattr__(name,value)
        for mod in list(sys.modules.values()):
            if mod and (getattr(mod,'__name__','').startswith('legacy_') or getattr(mod,'__name__','').startswith(('filesystem_common.','docker_common.'))):
                if name in vars(mod):setattr(mod,name,value)
sys.modules[__name__].__class__=TestFacade

import os
