@echo off
REM Serve the seo-crawler MCP to the LAN at http://<this-pc-ip>:8765/mcp
cd /d "%~dp0"
if "%MCP_TOKEN%"=="" if exist "%USERPROFILE%\.seo_mcp_token" set /p MCP_TOKEN=<"%USERPROFILE%\.seo_mcp_token"
".venv\Scripts\python.exe" seo_mcp_server.py --http --port 8765
