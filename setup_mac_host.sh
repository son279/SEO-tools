#!/bin/bash
# One-time setup for a Mac that hosts the SEO Tools MCP server (crawler + backlinks) on the LAN.
# Expected layout (NOT inside Downloads/Documents/Desktop - macOS blocks background access there):
#   ~/seo-mcp/SEO System                 (this script lives here)
#   backlink data: $BACKLINK_DATA_DIR, default ~/seo-mcp/SEO System/backlink_data
# Run:  bash setup_mac_host.sh        (asks for your Mac password for the sudo steps)
#       BACKLINK_DATA_DIR=/Volumes/SSD/backlink_data bash setup_mac_host.sh
set -euo pipefail

SEO_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${BACKLINK_DATA_DIR:-$SEO_DIR/backlink_data}"
mkdir -p "$DATA_DIR"
DATA_DIR="$(cd "$DATA_DIR" && pwd)"
PY="${PYTHON:-python3}"
TOKEN_FILE="$HOME/.seo_mcp_token"
LOG_DIR="$HOME/Library/Logs/seo-mcp"
LABEL="com.seotools.mcp"
PORT=8765

case "$SEO_DIR|$DATA_DIR" in
  *"/Downloads/"*|*"/Documents/"*|*"/Desktop/"*)
    echo "ERROR: move the project and backlink data out of Downloads/Documents/Desktop (e.g. to ~/seo-mcp/)."
    echo "macOS privacy protection blocks background services from reading those folders."
    exit 1 ;;
esac

echo "==> Checking Python"
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required, found " + sys.version' \
  || { echo "Install Python 3.10+ (python.org installer or: brew install python@3.12)"; exit 1; }

echo "==> Creating virtual environment (.venv-mac, leaves a Windows .venv alone)"
[ -x "$SEO_DIR/.venv-mac/bin/python" ] || "$PY" -m venv "$SEO_DIR/.venv-mac"
"$SEO_DIR/.venv-mac/bin/python" -m pip install -q --upgrade pip
"$SEO_DIR/.venv-mac/bin/pip" install -q -r "$SEO_DIR/requirements.txt"

echo "==> Shared token"
if [ ! -s "$TOKEN_FILE" ]; then
  openssl rand -hex 32 | tr -d '\n' > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
TOKEN="$(cat "$TOKEN_FILE")"
mkdir -p "$LOG_DIR"
chmod +x "$SEO_DIR/start_seo_mcp_lan.sh"

echo "==> Removing the older two-server services if present"
for old in com.bigriver.seo-mcp.crawler com.bigriver.seo-mcp.backlink; do
  if [ -f "/Library/LaunchDaemons/$old.plist" ]; then
    sudo launchctl bootout system "/Library/LaunchDaemons/$old.plist" 2>/dev/null || true
    sudo rm -f "/Library/LaunchDaemons/$old.plist"
  fi
done

echo "==> Installing startup service (starts at boot, restarts if it crashes)"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
sudo tee "$PLIST" > /dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>UserName</key><string>$USER</string>
  <key>WorkingDirectory</key><string>$SEO_DIR</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$SEO_DIR/start_seo_mcp_lan.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MCP_TOKEN_FILE</key><string>$TOKEN_FILE</string>
    <key>BACKLINK_DATA_DIR</key><string>$DATA_DIR</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>SoftResourceLimits</key><dict><key>NumberOfFiles</key><integer>10240</integer></dict>
  <key>HardResourceLimits</key><dict><key>NumberOfFiles</key><integer>10240</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/seo-tools.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/seo-tools.log</string>
</dict>
</plist>
PLIST
sudo chown root:wheel "$PLIST"
sudo chmod 644 "$PLIST"
sudo launchctl bootout system "$PLIST" 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST"

echo "==> Allowing Python through the macOS firewall (if it is on)"
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
real="$("$SEO_DIR/.venv-mac/bin/python" -c 'import os,sys; print(os.path.realpath(sys.executable))')"
sudo "$FW" --add "$real" > /dev/null || true
sudo "$FW" --unblockapp "$real" > /dev/null || true

echo "==> Keeping the Mac awake and restarting after power loss"
sudo pmset -a sleep 0 disksleep 0 womp 1
sudo pmset -a autorestart 1 2>/dev/null || true

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<this-mac-ip>')"
sleep 3
echo
echo "Done."
echo "Host IP      : $IP   (reserve this IP in your router's DHCP settings)"
echo "SEO Tools MCP: http://$IP:$PORT/mcp   (crawler + backlink tools)"
echo "Backlink data: $DATA_DIR"
echo "Token        : $TOKEN   (stored in $TOKEN_FILE)"
echo "Logs         : $LOG_DIR/seo-tools.log"
echo "Check        : curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:$PORT/mcp   (expect 401)"
