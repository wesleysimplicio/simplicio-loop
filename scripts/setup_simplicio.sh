#!/usr/bin/env bash
# setup_simplicio.sh — install + configure the Simplicio Token Monitor and capture proxy.
# The capture proxy is the native Simplicio engine (engine/simplicio_engine.py) — self-contained,
# stdlib only, no external dependency. Everything is Simplicio.
# Usage: bash scripts/setup_simplicio.sh [--port 8788] [--dashboard-port 9090] [--upstream HOST]
set -euo pipefail

PORT="${2:-8788}"
DASH_PORT="${4:-9090}"
HERMES_CONFIG="$HOME/.hermes/config.yaml"
LAUNCHD="$HOME/Library/LaunchAgents"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROXY_SERVICE="ai.simplicio.proxy"
MONITOR_SERVICE="ai.simplicio.token-monitor"
TRAY_SERVICE="ai.simplicio.tray"

echo "⬡ Simplicio Token Monitor setup — simplicio-loop"
echo ""

# 1. Install (native engine is stdlib-only; only the optional menu-bar tray needs a dep)
echo "📦 Installing menu-bar tray dep (optional)..."
pip install --user rumps 2>&1 | tail -1 || echo "  (rumps optional — menu-bar tray needs it on macOS)"

# 2. Native capture engine (self-contained, no binary to install)
UPSTREAM="${6:-https://api.deepseek.com}"
ENGINE="$SCRIPT_DIR/engine/simplicio_engine.py"
echo "✅ capture engine: $ENGINE (native)"

# 3. Create launchd plist for the capture proxy
echo "📋 Creating launchd plist for proxy ($PROXY_SERVICE)..."
mkdir -p "$LAUNCHD"
cat > "$LAUNCHD/$PROXY_SERVICE.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PROXY_SERVICE</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$ENGINE</string>
        <string>proxy</string>
        <string>--port</string>
        <string>$PORT</string>
        <string>--upstream</string>
        <string>$UPSTREAM</string>
        <string>--host</string>
        <string>127.0.0.1</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.hermes/logs/simplicio-proxy.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.hermes/logs/simplicio-proxy.error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/Library/Python/3.9/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin</string>
    </dict>
</dict>
</plist>
EOF
echo "✅ $LAUNCHD/$PROXY_SERVICE.plist"

# 4. Token Monitor dashboard + menu-bar tray are ON-DEMAND (not auto-started). Open them only
# when you want: `bash scripts/simplicio-economy.sh monitor` / `... tray`. Only the capture proxy
# (above) stays always-on, because the wired clients need it reachable.
# Remove any leftover auto-start plists from a previous install so they don't auto-launch at login.
launchctl bootout "gui/$(id -u)/$MONITOR_SERVICE" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$TRAY_SERVICE" 2>/dev/null || true
rm -f "$LAUNCHD/$MONITOR_SERVICE.plist" "$LAUNCHD/$TRAY_SERVICE.plist" 2>/dev/null || true
echo "✅ token monitor + tray: on-demand (open with simplicio-economy monitor / tray)"

# 5. Add env vars to .zshrc (idempotent)
echo "🔧 Configuring shell environment..."
for VAR in 'export OPENAI_BASE_URL=https://api.deepseek.com/v1' 'export SIMPLICIO_PROXY_PORT='"$PORT"; do
  KEY=$(echo "$VAR" | cut -d= -f1 | cut -d' ' -f2)
  if grep -q "$KEY" ~/.zshrc 2>/dev/null; then
    echo "  $KEY already in .zshrc"
  else
    echo "$VAR" >> ~/.zshrc
    echo "  ✅ $KEY added to .zshrc"
  fi
  eval "$VAR"
done
# Claude (Anthropic) auth via OAuth, which 401s through the proxy — only route it with a static key.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  if grep -q "ANTHROPIC_BASE_URL" ~/.zshrc 2>/dev/null; then
    echo "  ANTHROPIC_BASE_URL already in .zshrc"
  else
    echo 'export ANTHROPIC_BASE_URL=http://127.0.0.1:'"$PORT" >> ~/.zshrc
    echo "  ✅ ANTHROPIC_BASE_URL added to .zshrc (static key present)"
  fi
else
  echo "  ⊘ ANTHROPIC_BASE_URL skipped (OAuth claude; set ANTHROPIC_API_KEY to capture Claude)"
fi

# 6. Configure Hermes base_url
echo "🔧 Configuring Hermes base_url..."
if command -v hermes &>/dev/null; then
  CURRENT=$(grep "base_url:" "$HERMES_CONFIG" 2>/dev/null | head -1 | tr -d ' ')
  if [ "$CURRENT" != "base_url:http://127.0.0.1:$PORT/v1" ]; then
    hermes config set model.base_url "http://127.0.0.1:$PORT/v1" 2>&1
    echo "  ✅ model.base_url = http://127.0.0.1:$PORT/v1"
  else
    echo "  model.base_url already set"
  fi
fi

# 7. Load services
echo "🚀 Starting the capture proxy (only always-on service)..."
# Idempotent (re-install safe): bootout is async, so wait before bootstrap; if the service is
# still loaded (bootstrap → I/O error), force-restart it with kickstart instead.
for SVC in "$PROXY_SERVICE"; do
  launchctl bootout "gui/$(id -u)/$SVC" 2>/dev/null || true
  sleep 1
  launchctl bootstrap "gui/$(id -u)" "$LAUNCHD/$SVC.plist" 2>/dev/null \
    || launchctl kickstart -k "gui/$(id -u)/$SVC" 2>/dev/null || true
done

sleep 3
echo ""
echo "═══════════════════════════════════════"
echo "  ✅ Simplicio Token Monitor setup complete"
echo "═══════════════════════════════════════"
echo "  Proxy:          http://127.0.0.1:$PORT"
echo "  Token Monitor:  opens once on first install, then on-demand"
echo "                  re-open → simplicio-loop dashboard   (or: bash scripts/simplicio-economy.sh monitor, opens :$DASH_PORT)"
echo "  Menu-bar tray:  on-demand → bash scripts/simplicio-economy.sh tray"
echo "  Hermes:         → proxy → DeepSeek (auto-routed)"
echo "───────────────────────────────────────"
echo "  Optional MCP tools per client (memory/retrieve/stats — does NOT route traffic):"
echo "    bash scripts/simplicio-capture.sh init      # Claude/Codex/Copilot/OpenClaw MCP tools"
echo "═══════════════════════════════════════"
echo ""
# Turn on always-capture: route Claude (Anthropic) + Codex/OpenAI clients through the capture
# proxy so the monitor measures all three. The engine routes each model to its REAL provider
# (no model swap); effective on the next shell. Opt out with SIMPLICIO_NO_WIRE=1. Reversible.
echo "🔌 Enabling always-capture (Claude + Codex/OpenAI → capture proxy, measured)..."
bash "$SCRIPT_DIR/scripts/simplicio-economy.sh" wire 2>/dev/null || \
  echo "  (run 'bash scripts/simplicio-economy.sh wire' to enable always-capture)"
echo ""
# Token-economy module is now active — show the integrated stack status.
bash "$SCRIPT_DIR/scripts/simplicio-economy.sh" status 2>/dev/null || true
echo ""
echo "  Manage the whole economy stack any time:  bash scripts/simplicio-economy.sh {status|up}"
echo ""
