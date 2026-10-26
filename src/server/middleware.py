"""Server middleware."""

from __future__ import annotations

from fastmcp.server.middleware import Middleware


class NoOutputSchemas(Middleware):
    """Backstop: never advertise output schemas (they cost catalog tokens every turn)."""

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [t if t.output_schema is None else t.model_copy(update={"output_schema": None}) for t in tools]
