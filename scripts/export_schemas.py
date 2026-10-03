"""Export actual public TWYLT metadata; requires installed requirements.txt."""
from pathlib import Path
import json
import subprocess
import sys
BASE = Path(__file__).resolve().parents[1]
output = BASE / 'schemas'
output.mkdir(exist_ok=True)
for folder in sorted((BASE/'tools').iterdir()):
    if not folder.is_dir(): continue
    result = subprocess.run([sys.executable, str(folder/'run.py'), '{"describe":"json_spec"}'],
        check=True, capture_output=True, text=True, cwd=BASE)
    spec = json.loads(result.stdout)
    (output/(folder.name+'.json')).write_text(json.dumps(spec,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
