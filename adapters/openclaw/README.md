# OpenClaw adapter

OpenClaw is a multi-channel agent gateway (Node) with a plugin SDK exposing many named
extension points. It is both a target runtime AND a deployment surface — run simplicio-tasks
unattended, reachable from WhatsApp/Slack/Discord/Telegram/etc.

## Install

```bash
bash scripts/install.sh openclaw
```

The installer places the 6 skills under OpenClaw's skills tree and registers a plugin that maps
the OpenClaw SDK extension points (approval-*, channel-*, agent-*) onto the simplicio
contract — notably `notify`/`human_gate` (async approvals over a chat channel) and `watcher`
(the gateway's scheduler).

## Loop drive — native scheduler + channel approvals

The gateway's scheduler drives the loop; `simplicio-loop`'s evidence-gated promise governs exit.
Irreversible-op approvals route through `human_gate` → a chat message awaiting a human reply
(headless rule: no reply = block the destructive op, do the safe part).

## Native bind — plugin SDK

| Simplicio point | OpenClaw SDK |
|---|---|
| `notify` / `human_gate` | `channel-*` send + approval-request handlers |
| `watcher` | gateway scheduler |
| `execute` | agent fan-out across channel sessions |
| `status` | channel digest messages |

## Token economy

`orient_clamp.py` works as-is on the host; channel digests use the HUMAN density tier, internal
reports the MACHINE tier.

## Use

Message the bot on any connected channel: `/simplicio-tasks finish all the open issues`. Progress
digests and approval prompts come back on the same channel.
