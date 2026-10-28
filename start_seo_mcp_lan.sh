#!/bin/bash
# Serve the SEO Tools MCP (crawler + backlinks) to the LAN at http://<this-mac-ip>:8765/mcp (macOS / Linux)
# Backlink data folder: BACKLINK_DATA_DIR (default: ./backlink_data)
cd "$(dirname "$0")"
ulimit -n 10240 2>/dev/null || true   # follow-up link checks open many sockets
TF="${MCP_TOKEN_FILE:-$HOME/.seo_mcp_token}"
if [ -z "${MCP_TOKEN:-}" ] && [ -f "$TF" ]; then export MCP_TOKEN="$(cat "$TF")"; fi
exec .venv-mac/bin/python -u seo_mcp_server.py --http --port 8765
