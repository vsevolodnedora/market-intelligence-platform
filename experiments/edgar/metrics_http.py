"""Lightweight async HTTP exporter for /metrics and /healthz.

Runs inside the daemon process using asyncio's low-level server API —
no external dependencies beyond the Python standard library.

Usage (inside the daemon's event loop):

    from metrics_http import start_metrics_server, stop_metrics_server
    from metrics import METRICS

    server = await start_metrics_server(METRICS, host="127.0.0.1", port=9108)
    # ... daemon runs ...
    await stop_metrics_server(server)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from metrics import MetricsRegistry

logger = logging.getLogger(__name__)


async def _handle_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    registry: MetricsRegistry,
) -> None:
    """Handle a single HTTP request on the metrics server."""
    try:
        # Read the request line with a size limit and timeout
        try:
            request_line = await asyncio.wait_for(
                reader.readline(), timeout=5.0,
            )
        except asyncio.TimeoutError:
            writer.close()
            return

        if not request_line:
            writer.close()
            return

        # Parse the request method and path
        parts = request_line.decode("utf-8", errors="replace").strip().split()
        if len(parts) < 2:
            await _send_response(writer, 400, "text/plain", b"Bad Request\n")
            return

        method = parts[0].upper()
        path = parts[1]

        # Drain remaining headers (we don't need them)
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                break
            if line in (b"\r\n", b"\n", b""):
                break

        if method != "GET":
            await _send_response(writer, 405, "text/plain", b"Method Not Allowed\n")
            return

        if path == "/metrics":
            body = registry.expose().encode("utf-8")
            await _send_response(
                writer, 200,
                "text/plain; version=0.0.4; charset=utf-8",
                body,
            )
        elif path == "/healthz":
            health = registry.health_check()
            body = json.dumps(health, indent=2).encode("utf-8")
            await _send_response(writer, 200, "application/json", body)
        else:
            await _send_response(writer, 404, "text/plain", b"Not Found\n")

    except Exception:
        logger.debug("metrics HTTP handler error", exc_info=True)
        try:
            await _send_response(writer, 500, "text/plain", b"Internal Server Error\n")
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _send_response(
    writer: asyncio.StreamWriter,
    status: int,
    content_type: str,
    body: bytes,
) -> None:
    """Write a complete HTTP/1.0 response and drain the transport buffer."""
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
              405: "Method Not Allowed", 500: "Internal Server Error"}.get(
        status, "Unknown"
    )
    header = (
        f"HTTP/1.0 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    writer.write(header.encode("utf-8") + body)
    await writer.drain()


async def start_metrics_server(
    registry: MetricsRegistry,
    host: str = "127.0.0.1",
    port: int = 9108,
) -> asyncio.Server:
    """Start the metrics HTTP server and return the asyncio.Server handle.

    The caller should ``await stop_metrics_server(server)`` during shutdown.
    """

    async def _on_connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        await _handle_request(reader, writer, registry)

    server = await asyncio.start_server(_on_connection, host, port)
    addr = server.sockets[0].getsockname() if server.sockets else (host, port)
    logger.info("metrics HTTP server listening on %s:%d", addr[0], addr[1])
    return server


async def stop_metrics_server(server: asyncio.Server | None) -> None:
    """Gracefully shut down the metrics server."""
    if server is None:
        return
    server.close()
    await server.wait_closed()
    logger.info("metrics HTTP server stopped")