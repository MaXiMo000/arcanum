"""Run the API server: python -m arcanum.server (configured through ARCANUM_* environment variables).

Hosting platforms that assign the port through PORT (Render, Heroku-style hosts) are followed
automatically.
"""

import os

import uvicorn

from .app import _flag, create_app

if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host=os.environ.get("ARCANUM_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT") or os.environ.get("ARCANUM_PORT") or "8000"),
        # Only these proxy addresses may set the client address through X-Forwarded-For. Never use "*":
        # the client address would then be the left-most entry, which any client can forge.
        forwarded_allow_ips=os.environ.get("ARCANUM_TRUSTED_PROXIES", "127.0.0.1"),
        limit_concurrency=int(os.environ.get("ARCANUM_MAX_CONNECTIONS", "500")),
        timeout_keep_alive=5,
        ws_max_size=64 * 1024,  # clients never need to send large WebSocket messages
        ws_ping_interval=20,
        ws_ping_timeout=20,
        h11_max_incomplete_event_size=16 * 1024,
        server_header=False,
        access_log=_flag(os.environ.get("ARCANUM_ACCESS_LOG", "0")),  # URLs carry session ids
    )
