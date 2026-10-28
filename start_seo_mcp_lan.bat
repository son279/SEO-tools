@echo off
REM Serve the SEO Tools MCP (crawler + backlinks) to the LAN at http://<this-pc-ip>:8765/mcp
REM Backlink data folder: BACKLINK_DATA_DIR (default: .\backlink_data)
cd /d "%~dp0"
if "%MCP_TOKEN%"=="" if exist "%USERPROFILE%\.seo_mcp_token" set /p MCP_TOKEN=<"%USERPROFILE%\.seo_mcp_token"
".venv\Scripts\python.exe" seo_mcp_server.py --http --port 8765
