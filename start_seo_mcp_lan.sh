#!/bin/bash
# Serve the seo-crawler MCP to the LAN at http://<this-mac-ip>:8765/mcp (macOS / Linux)
cd "$(dirname "$0")"
TF="${MCP_TOKEN_FILE:-$HOME/.seo_mcp_token}"
if [ -z "${MCP_TOKEN:-}" ] && [ -f "$TF" ]; then export MCP_TOKEN="$(cat "$TF")"; fi
exec .venv-mac/bin/python -u seo_mcp_server.py --http --port 8765
