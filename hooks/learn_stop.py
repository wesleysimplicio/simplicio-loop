#!/usr/bin/env python3
"""simplicio-learn — session-end trigger (Stop / SubagentStop, fail-open).

Drops a lightweight marker so the next simplicio-tasks tick (or the human) runs the
`simplicio-learn` retrospective on the just-finished run. It does NOT itself mine the
transcript (that's the skill's job, where the model can judge signal) — it only signals
"there is a finished run worth learning from", with the transcript path if the host gave one.

Fail-open and silent: never blocks stopping, never errors out loud.
"""
import json
import os
import sys
import time

LEARN_DIR = os.path.join(".orchestrator", "learn")
QUEUE = os.path.join(LEARN_DIR, "pending.jsonl")


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        os.makedirs(LEARN_DIR, exist_ok=True)
        rec = {
            "at": int(time.time()),
            "transcript": data.get("transcript_path") or data.get("transcriptPath"),
            "session": data.get("session_id") or data.get("sessionId"),
        }
        with open(QUEUE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    sys.exit(0)  # never block the stop


if __name__ == "__main__":
    main()
