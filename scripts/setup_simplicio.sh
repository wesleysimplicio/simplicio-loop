#!/usr/bin/env bash
# setup_simplicio.sh — install + configure the Simplicio Token Monitor and compression proxy.
# The proxy is powered by headroom-ai (a third-party accelerator Simplicio integrates); its
# binary is still `headroom`, so install/run commands keep that name. Everything else is Simplicio.
# Usage: bash scripts/setup_simplicio.sh [--port 8788] [--dashboard-port 9090]
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

# 1. Install
echo "📦 Installing capture engine + menu-bar app deps..."
pip install headroom-ai httpx[http2] 2>&1 | tail -2
pip install --user rumps 2>&1 | tail -1 || echo "  (rumps optional — menu-bar tray needs it on macOS)"

# 2. Verify proxy binary
HEADROOM=$(which headroom 2>/dev/null || echo "")
if [ -z "$HEADROOM" ]; then
  HEADROOM=$(find ~/ -path "*/bin/headroom" -type f 2>/dev/null | head -1)
fi
if [ -z "$HEADROOM" ]; then
  echo "❌ proxy binary not found after install"
  exit 1
fi
echo "✅ proxy binary: $HEADROOM"

# 3. Create launchd plist for the compression proxy
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
        <string>$HEADROOM</string>
        <string>proxy</string>
        <string>--port</string>
        <string>$PORT</string>
        <string>--openai-api-url</string>
        <string>https://api.deepseek.com/v1</string>
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

# 4. Create launchd plist for the Simplicio Token Monitor
echo "📋 Creating launchd plist for token monitor ($MONITOR_SERVICE)..."
cat > "$LAUNCHD/$MONITOR_SERVICE.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$MONITOR_SERVICE</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$SCRIPT_DIR/hooks/simplicio_dashboard.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.hermes/logs/simplicio-token-monitor.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.hermes/logs/simplicio-token-monitor.error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PORT</key>
        <string>$DASH_PORT</string>
        <key>SIMPLICIO_PROXY_PORT</key>
        <string>$PORT</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin</string>
    </dict>
</dict>
</plist>
EOF
echo "✅ $LAUNCHD/$MONITOR_SERVICE.plist"

# 4b. Create launchd plist for the menu-bar tray app
echo "📋 Creating launchd plist for menu-bar tray ($TRAY_SERVICE)..."
cat > "$LAUNCHD/$TRAY_SERVICE.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$TRAY_SERVICE</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$SCRIPT_DIR/app/simplicio_tray.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.hermes/logs/simplicio-tray.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.hermes/logs/simplicio-tray.error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SIMPLICIO_PROXY_PORT</key>
        <string>$PORT</string>
        <key>SIMPLICIO_MONITOR_PORT</key>
        <string>$DASH_PORT</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin</string>
    </dict>
</dict>
</plist>
EOF
echo "✅ $LAUNCHD/$TRAY_SERVICE.plist"

# 5. Add env vars to .zshrc (idempotent)
echo "🔧 Configuring shell environment..."
for VAR in 'export ANTHROPIC_BASE_URL=http://127.0.0.1:'"$PORT" 'export OPENAI_BASE_URL=https://api.deepseek.com/v1' 'export SIMPLICIO_PROXY_PORT='"$PORT"; do
  KEY=$(echo "$VAR" | cut -d= -f1 | cut -d' ' -f2)
  if grep -q "$KEY" ~/.zshrc 2>/dev/null; then
    echo "  $KEY already in .zshrc"
  else
    echo "$VAR" >> ~/.zshrc
    echo "  ✅ $KEY added to .zshrc"
  fi
  eval "$VAR"
done

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
echo "🚀 Starting services..."
launchctl bootout "gui/$(id -u)/$PROXY_SERVICE" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCHD/$PROXY_SERVICE.plist" 2>&1
launchctl bootout "gui/$(id -u)/$MONITOR_SERVICE" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCHD/$MONITOR_SERVICE.plist" 2>&1
launchctl bootout "gui/$(id -u)/$TRAY_SERVICE" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCHD/$TRAY_SERVICE.plist" 2>&1

sleep 3
echo ""
echo "═══════════════════════════════════════"
echo "  ✅ Simplicio Token Monitor setup complete"
echo "═══════════════════════════════════════"
echo "  Proxy:          http://127.0.0.1:$PORT"
echo "  Token Monitor:  http://127.0.0.1:$DASH_PORT"
echo "  Menu-bar tray:  live tokens saved (hexagon icon in the menu bar)"
echo "  Hermes:         → proxy → DeepSeek"
echo "───────────────────────────────────────"
echo "  Capture more runtimes (transparent, per-client):"
echo "    bash scripts/simplicio-capture.sh status   # what's intercepting now"
echo "    bash scripts/simplicio-capture.sh init      # wire Claude/Codex/Copilot/OpenClaw"
echo "  (init forwards each client to its OWN provider — see"
echo "   .claude/skills/simplicio-tasks/references/token-capture.md)"
echo "═══════════════════════════════════════"
echo ""
