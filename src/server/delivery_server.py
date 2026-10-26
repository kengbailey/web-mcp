"""Web MCP server. Run: python -m src.server.delivery_server [--host 0.0.0.0] [--port 3090]."""

from __future__ import annotations

import argparse
import logging
import os

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from ..core.services import Services, install
from .auth import build_auth
from .middleware import NoOutputSchemas
from .tools import reddit, search, web, youtube

INSTRUCTIONS = (
    "Web research tools. Results are compact text. Long text ends with 'next: <cursor>'; "
    "pass it to read_content. Listings end with 'more: <cursor>'; pass it back to the same tool."
)


def make_services() -> Services:
    return Services()


@lifespan
async def services_lifespan(server):
    svc = make_services()
    install(svc)
    try:
        if svc.settings.browser_prewarm:
            await web.prewarm(svc)
        yield {"services": svc}
    finally:
        install(None)
        await svc.aclose()


mcp = FastMCP(
    "Web MCP",
    instructions=INSTRUCTIONS,
    auth=build_auth(),
    lifespan=services_lifespan,
    middleware=[NoOutputSchemas()],
    mask_error_details=True,
)

# Registration order is the catalog order; keep it stable for client prompt caching.
for module in (search, web, reddit, youtube):
    module.register(mcp)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "3090")))
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--transport", choices=["http", "sse"], default=os.getenv("MCP_TRANSPORT", "http"))
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    # httpx logs full request URLs at INFO; keep provider URLs (and any query secrets) out of logs.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
