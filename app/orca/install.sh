#!/usr/bin/env bash
set -euo pipefail

for cmd in hermes simplicio_agent codex claude python3; do command -v "$cmd" >/dev/null || { echo "Missing: $cmd" >&2; exit 1; }; done
python3 -c 'import yaml' >/dev/null || { echo 'Install PyYAML: python3 -m pip install --user pyyaml' >&2; exit 1; }
backup="$HOME/.orca-agent-preset-backup/$(date +%Y%m%d-%H%M%S)"; mkdir -p "$backup"
for file in "$HOME/.hermes/config.yaml" "$HOME/.simplicio_agent/config.yaml" "$HOME/.codex/config.toml" "$HOME/.claude/settings.json"; do [[ -f "$file" ]] && cp "$file" "$backup/$(basename "$file")"; done
for agent in hermes simplicio_agent; do
  "$agent" config set model.default tencent/hy3:free
  "$agent" config set model.provider openrouter
  "$agent" config set approvals.mode off
  "$agent" config set approvals.cron_mode approve
done
PRESET_HOME="$HOME" python3 - <<'PY'
import json, os
from pathlib import Path
import yaml
h=Path(os.environ['PRESET_HOME'])
for p in (h/'.hermes/config.yaml',h/'.simplicio_agent/config.yaml'):
 c=yaml.safe_load(p.read_text()) if p.exists() else {}; c=c or {}
 c.setdefault('mcp_servers',{})['simplicio-runtime']={'command':'simplicio','args':['serve','--mcp','--stdio'],'enabled':True}
 p.parent.mkdir(parents=True,exist_ok=True); p.write_text(yaml.safe_dump(c,sort_keys=False))
p=h/'.codex/config.toml'; p.parent.mkdir(parents=True,exist_ok=True); old=p.read_text() if p.exists() else ''
old='\n'.join(x for x in old.splitlines() if not x.startswith(('model =','model_reasoning_effort =')))
p.write_text('model = "gpt-5.6-terra"\nmodel_reasoning_effort = "medium"\n'+old.rstrip()+'\n')
p=h/'.claude/settings.json'; p.parent.mkdir(parents=True,exist_ok=True); c=json.loads(p.read_text()) if p.exists() else {}; c.update(model='sonnet',effortLevel='medium'); p.write_text(json.dumps(c,indent=2)+'\n')
PY
codex --strict-config --help >/dev/null
echo "Applied preset. Backup: $backup. Fast mode is unavailable with tencent/hy3:free."
