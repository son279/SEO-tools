#!/bin/bash
# One-time setup for a Mac that hosts both SEO MCP servers on the LAN.
# Expected layout (NOT inside Downloads/Documents/Desktop - macOS blocks background access there):
#   ~/seo-mcp/SEO System        (this script lives here)
#   ~/seo-mcp/PythonProject5
# Run:  bash setup_mac_host.sh        (asks for your Mac password for the sudo steps)
set -euo pipefail

SEO_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKLINK_DIR="${BACKLINK_DIR:-$SEO_DIR/../PythonProject5}"
BACKLINK_DIR="$(cd "$BACKLINK_DIR" && pwd)"
PY="${PYTHON:-python3}"
TOKEN_FILE="$HOME/.seo_mcp_token"
LOG_DIR="$HOME/Library/Logs/seo-mcp"

case "$SEO_DIR|$BACKLINK_DIR" in
  *"/Downloads/"*|*"/Documents/"*|*"/Desktop/"*)
    echo "ERROR: move both folders out of Downloads/Documents/Desktop (e.g. to ~/seo-mcp/)."
    echo "macOS privacy protection blocks background services from reading those folders."
    exit 1 ;;
esac

echo "==> Checking Python"
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required, found " + sys.version' \
  || { echo "Install Python 3.10+ (python.org installer or: brew install python@3.12)"; exit 1; }

echo "==> Creating virtual environments (.venv-mac, leaves the Windows .venv alone)"
for d in "$SEO_DIR" "$BACKLINK_DIR"; do
  [ -x "$d/.venv-mac/bin/python" ] || "$PY" -m venv "$d/.venv-mac"
  "$d/.venv-mac/bin/python" -m pip install -q --upgrade pip
done
"$SEO_DIR/.venv-mac/bin/pip" install -q -r "$SEO_DIR/requirements.txt"
"$BACKLINK_DIR/.venv-mac/bin/pip" install -q "mcp[cli]<2" requests aiohttp networkx tldextract

echo "==> Shared token"
if [ ! -s "$TOKEN_FILE" ]; then
  openssl rand -hex 32 | tr -d '\n' > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
TOKEN="$(cat "$TOKEN_FILE")"
mkdir -p "$LOG_DIR"
chmod +x "$SEO_DIR/start_seo_mcp_lan.sh" "$BACKLINK_DIR/start_backlink_mcp_lan.sh"

write_daemon() {  # label  workdir  script  logname
  local plist="/Library/LaunchDaemons/$1.plist"
  sudo tee "$plist" > /dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>UserName</key><string>$USER</string>
  <key>WorkingDirectory</key><string>$2</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$3</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MCP_TOKEN_FILE</key><string>$TOKEN_FILE</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/$4.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$4.log</string>
</dict>
</plist>
PLIST
  sudo chown root:wheel "$plist"
  sudo chmod 644 "$plist"
  sudo launchctl bootout system "$plist" 2>/dev/null || true
  sudo launchctl bootstrap system "$plist"
}

echo "==> Installing startup services (start at boot, restart if they crash)"
write_daemon com.bigriver.seo-mcp.crawler  "$SEO_DIR"      "$SEO_DIR/start_seo_mcp_lan.sh"           crawler
write_daemon com.bigriver.seo-mcp.backlink "$BACKLINK_DIR" "$BACKLINK_DIR/start_backlink_mcp_lan.sh" backlink

echo "==> Allowing Python through the macOS firewall (if it is on)"
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
for d in "$SEO_DIR" "$BACKLINK_DIR"; do
  real="$("$d/.venv-mac/bin/python" -c 'import os,sys; print(os.path.realpath(sys.executable))')"
  sudo "$FW" --add "$real" > /dev/null || true
  sudo "$FW" --unblockapp "$real" > /dev/null || true
done

echo "==> Keeping the Mac awake and restarting after power loss"
sudo pmset -a sleep 0 disksleep 0 womp 1
sudo pmset -a autorestart 1 2>/dev/null || true

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<this-mac-ip>')"
sleep 3
echo
echo "Done."
echo "Host IP : $IP   (reserve this IP in your router's DHCP settings)"
echo "Crawler : http://$IP:8765/mcp"
echo "Backlink: http://$IP:8766/mcp"
echo "Token   : $TOKEN   (stored in $TOKEN_FILE)"
echo "Logs    : $LOG_DIR"
echo "Check   : curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8765/mcp   (expect 401)"
