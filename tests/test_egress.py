from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai_supervisor.core.egress import EgressProxy


@pytest.mark.asyncio
async def test_egress_proxy_tunnels_allowed_host_and_denies_other(tmp_path: Path) -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(100)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    socket_path = tmp_path / "egress.sock"
    proxy = EgressProxy(
        socket_path,
        allowed_hosts=["127.0.0.1"],
        allowed_ports=[port, 80, 443],
        max_connections=4,
    )
    await proxy.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 200")
        writer.write(b"hello")
        await writer.drain()
        assert await reader.read(20) == b"echo:hello"
        writer.close()
        await writer.wait_closed()

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(b"CONNECT denied.invalid:443 HTTP/1.1\r\nHost: denied.invalid\r\n\r\n")
        await writer.drain()
        denied = await reader.readuntil(b"\r\n\r\n")
        assert denied.startswith(b"HTTP/1.1 403")
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()
