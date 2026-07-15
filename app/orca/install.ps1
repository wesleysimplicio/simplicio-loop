$ErrorActionPreference='Stop'
foreach($c in 'hermes','simplicio_agent','codex','claude','python'){if(-not(Get-Command $c -EA SilentlyContinue)){throw "Missing: $c"}}
& python -c 'import yaml'; if($LASTEXITCODE){throw 'Install PyYAML: python -m pip install --user pyyaml'}
$backup=Join-Path $HOME ('.orca-agent-preset-backup\'+(Get-Date -Format yyyyMMdd-HHmmss)); New-Item -ItemType Directory -Force $backup|Out-Null
foreach($f in @('.hermes\config.yaml','.simplicio_agent\config.yaml','.codex\config.toml','.claude\settings.json')){$p=Join-Path $HOME $f;if(Test-Path $p){Copy-Item $p (Join-Path $backup (Split-Path $p -Leaf))}}
foreach($a in 'hermes','simplicio_agent'){& $a config set model.default 'tencent/hy3:free';& $a config set model.provider openrouter;& $a config set approvals.mode off;& $a config set approvals.cron_mode approve}
$env:PRESET_HOME=$HOME
@'
import json,os
from pathlib import Path
import yaml
h=Path(os.environ['PRESET_HOME'])
for p in (h/'.hermes/config.yaml',h/'.simplicio_agent/config.yaml'):
 c=yaml.safe_load(p.read_text()) if p.exists() else {}; c=c or {}; c.setdefault('mcp_servers',{})['simplicio-runtime']={'command':'simplicio','args':['serve','--mcp','--stdio'],'enabled':True}; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(yaml.safe_dump(c,sort_keys=False))
p=h/'.codex/config.toml';p.parent.mkdir(parents=True,exist_ok=True);o=p.read_text() if p.exists() else '';o='\n'.join(x for x in o.splitlines() if not x.startswith(('model =','model_reasoning_effort =')));p.write_text('model = "gpt-5.6-terra"\nmodel_reasoning_effort = "medium"\n'+o.rstrip()+'\n')
p=h/'.claude/settings.json';p.parent.mkdir(parents=True,exist_ok=True);c=json.loads(p.read_text()) if p.exists() else {};c.update(model='sonnet',effortLevel='medium');p.write_text(json.dumps(c,indent=2)+'\n')
'@ | & python -
& codex --strict-config --help | Out-Null
Write-Host "Applied preset. Backup: $backup. Fast mode is unavailable with tencent/hy3:free."
