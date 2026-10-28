"""Run a FastMCP server over Streamable HTTP so other machines on the LAN can use it.

Usage from a server's entry point:

    from lan_http import run_from_cli
    run_from_cli(mcp, default_port=8765)

Command line (stdio stays the default, so existing Claude Desktop configs keep working):

    python server.py                                  # stdio (unchanged)
    python server.py --http                           # LAN, 0.0.0.0:<default_port>/mcp
    python server.py --http --port 9000 --token SECRET

The bearer token can also come from the MCP_TOKEN environment variable.
When a token is set, every request must send:  Authorization: Bearer <token>
"""
from __future__ import annotations

import argparse
import hmac
import os
import socket
import sys


def _lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


class BearerAuth:
    """Tiny ASGI middleware: reject HTTP requests without the shared token."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", b"Bearer")],
                })
                await send({"type": "http.response.body",
                            "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)  # lifespan etc. pass straight through


def run_from_cli(mcp, default_port: int) -> None:
    parser = argparse.ArgumentParser(description=f"{mcp.name} MCP server")
    parser.add_argument("--http", action="store_true",
                        help="Serve over Streamable HTTP for the LAN instead of stdio.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Interface to bind (default 0.0.0.0 = all).")
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--token", default=os.environ.get("MCP_TOKEN", ""),
                        help="Shared bearer token (or set MCP_TOKEN). Strongly recommended.")
    args = parser.parse_args()

    if not args.http:
        mcp.run(transport="stdio")
        return

    import uvicorn

    # FastMCP auto-enables localhost-only Host checking when it is constructed with
    # the default host, which would reject requests addressed to the LAN IP.
    # Clear it; access control on the LAN is the bearer token instead.
    mcp.settings.transport_security = None
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    # Tools here are request/response; stateless mode survives server restarts
    # without clients having to re-handshake.
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True

    app = mcp.streamable_http_app()
    if args.token:
        app = BearerAuth(app, args.token)

    path = mcp.settings.streamable_http_path
    print(f"[{mcp.name}] Streamable HTTP on {args.host}:{args.port}{path}", file=sys.stderr)
    for ip in _lan_ips():
        print(f"[{mcp.name}]   LAN URL: http://{ip}:{args.port}{path}", file=sys.stderr)
    if not args.token:
        print(f"[{mcp.name}] WARNING: no token set - anyone on this network can call the tools.",
              file=sys.stderr)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
