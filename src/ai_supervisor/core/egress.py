from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class EgressError(RuntimeError):
    pass


class EgressProxy:
    """Allowlisted HTTP CONNECT proxy on a Unix socket.

    The worker runs in its own network namespace. It can reach only the local
    relay via loopback; that relay reaches this Unix socket. Direct outbound
    sockets are impossible because the namespace has no route.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        allowed_hosts: list[str],
        allowed_ports: list[int],
        max_connections: int = 32,
    ) -> None:
        self.socket_path = socket_path
        self.allowed_hosts = [host.lower().strip() for host in allowed_hosts if host.strip()]
        self.allowed_ports = set(allowed_ports)
        self._semaphore = asyncio.Semaphore(max_connections)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.socket_path), limit=64 * 1024
        )
        os.chmod(self.socket_path, 0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    def _allowed(self, host: str, port: int) -> bool:
        if port not in self.allowed_ports:
            return False
        host = host.lower().strip("[]")
        for allowed in self.allowed_hosts:
            if allowed.startswith("*."):
                suffix = allowed[1:]
                if host.endswith(suffix) and host != suffix[1:]:
                    return True
            elif host == allowed:
                return True
        return False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self._semaphore:
            upstream: asyncio.StreamWriter | None = None
            try:
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
                if len(header) > 32 * 1024:
                    raise EgressError("header too large")
                lines = header.decode("latin-1").split("\r\n")
                request = lines[0].split()
                if len(request) != 3:
                    raise EgressError("malformed proxy request")
                method, target, version = request
                if method.upper() == "CONNECT":
                    host, port = self._split_authority(target, default_port=443)
                    self._check(host, port)
                    upstream_reader, upstream_writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port), timeout=20
                    )
                    upstream = upstream_writer
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    await self._pipe(reader, writer, upstream_reader, upstream_writer)
                    return
                # Plain HTTP proxy request. HTTPS is the normal path; this
                # fallback keeps local/dev endpoints usable without weakening
                # the host/port allowlist.
                parsed = urlsplit(target)
                if parsed.scheme != "http" or not parsed.hostname:
                    raise EgressError("only absolute http:// proxy targets are supported")
                port = parsed.port or 80
                self._check(parsed.hostname, port)
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(parsed.hostname, port), timeout=20
                )
                upstream = upstream_writer
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                rewritten = f"{method} {path} {version}\r\n" + "\r\n".join(lines[1:])
                upstream_writer.write(rewritten.encode("latin-1"))
                await upstream_writer.drain()
                await self._pipe(reader, writer, upstream_reader, upstream_writer)
            except (TimeoutError, OSError, EgressError, ValueError) as exc:
                if not writer.is_closing():
                    body = f"egress denied: {type(exc).__name__}"
                    writer.write(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\nConnection: close\r\n\r\n"
                        + body.encode()
                    )
                    with contextlib.suppress(Exception):
                        await writer.drain()
            finally:
                if upstream is not None:
                    upstream.close()
                    with contextlib.suppress(Exception):
                        await upstream.wait_closed()
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    def _check(self, host: str, port: int) -> None:
        if not self._allowed(host, port):
            raise EgressError(f"host is not allowed: {host}:{port}")

    @staticmethod
    def _split_authority(authority: str, *, default_port: int) -> tuple[str, int]:
        if authority.startswith("["):
            end = authority.find("]")
            if end < 0:
                raise EgressError("invalid IPv6 authority")
            host = authority[1:end]
            rest = authority[end + 1 :]
            port = int(rest[1:]) if rest.startswith(":") else default_port
            return host, port
        if authority.count(":") == 1:
            host, raw_port = authority.rsplit(":", 1)
            return host, int(raw_port)
        return authority, default_port

    @staticmethod
    async def _pipe(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def copy(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
            try:
                while data := await source.read(64 * 1024):
                    target.write(data)
                    await target.drain()
            except (ConnectionError, OSError):
                return
            finally:
                with contextlib.suppress(Exception):
                    target.write_eof()

        await asyncio.gather(
            copy(client_reader, upstream_writer), copy(upstream_reader, client_writer)
        )
