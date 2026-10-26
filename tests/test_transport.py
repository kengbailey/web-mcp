"""Actual loopback HTTP and SSE transport; no external provider calls."""

import asyncio
import socket
import sys

import pytest
from fastmcp import Client


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,path", [("http", "/mcp"), ("sse", "/sse")])
async def test_v2_transport(transport, path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.server.delivery_server",
        "--port",
        str(port),
        "--transport",
        transport,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if proc.returncode is not None:
                pytest.fail("Server exited during startup")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            pytest.fail("Server startup timeout")
        async with Client(f"http://127.0.0.1:{port}{path}", timeout=10) as client:
            tools = await client.list_tools()
            assert len(tools) >= 11
            bad = await client.call_tool(
                "read_content", {"cursor": "expired"}, raise_on_error=False
            )
            assert bad.is_error
            assert "CURSOR_EXPIRED" in bad.content[0].text
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
