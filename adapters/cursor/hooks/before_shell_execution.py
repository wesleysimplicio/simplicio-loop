#!/usr/bin/env python3
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.cursor.adapter import decide
event = json.loads(sys.stdin.read() or "{}")
event.setdefault("hook_event_name", "beforeShellExecution")
decision = decide(event)
print(json.dumps(decision, ensure_ascii=False))
raise SystemExit(0 if decision.get("decision") != "block" else 2)
