from __future__ import annotations

import asyncio
import time

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}


def _filter_request_headers(request: web.Request) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP and key.lower() != "host"
    }
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.scheme
    return headers


def _filter_response_headers(response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP
    }


async def proxy_http(
    request: web.Request,
    host_port: int,
    tail: str = "",
    prefix: str = "",
    job_id: str = "",
) -> web.StreamResponse:
    session: ClientSession = request.app["http"]
    monitor = request.app.get("monitor")
    target = f"http://127.0.0.1:{host_port}/{tail}"
    body = await request.read()
    path = "/" + tail
    started = time.monotonic()
    try:
        response = await session.request(
            method=request.method,
            url=target,
            params=request.query,
            headers=_filter_request_headers(request),
            data=body if body else None,
            timeout=ClientTimeout(total=120, sock_connect=10),
            allow_redirects=False,
        )
    except asyncio.TimeoutError:
        if monitor and job_id:
            monitor.record_request(job_id, path, 0, (time.monotonic() - started) * 1000, failed=True)
        raise web.HTTPGatewayTimeout(text="Upstream timed out")
    except Exception as exc:  # noqa: BLE001
        if monitor and job_id:
            monitor.record_request(job_id, path, 0, (time.monotonic() - started) * 1000, failed=True)
        raise web.HTTPBadGateway(text=f"Cannot reach service: {exc}")

    if monitor and job_id:
        monitor.record_request(job_id, path, response.status, (time.monotonic() - started) * 1000)

    headers = _filter_response_headers(response)
    location = headers.get("Location")
    if location and location.startswith("/") and prefix:
        headers["Location"] = prefix + location.lstrip("/")

    stream = web.StreamResponse(status=response.status, headers=headers)
    await stream.prepare(request)
    async for chunk in response.content.iter_chunked(16384):
        await stream.write(chunk)
    await stream.write_eof()
    response.close()
    return stream


async def proxy_websocket(request: web.Request, host_port: int, tail: str = "") -> web.WebSocketResponse:
    ws_client = web.WebSocketResponse(autoping=False, max_msg_size=8 * 1024 * 1024)
    await ws_client.prepare(request)

    scheme = "wss" if request.scheme == "https" else "ws"
    target = f"{scheme}://127.0.0.1:{host_port}/{tail}"
    session: ClientSession = request.app["http"]
    try:
        upstream = await session.ws_connect(
            target,
            headers={"Host": f"127.0.0.1:{host_port}"},
            timeout=ClientTimeout(total=None, sock_connect=10),
            autoping=False,
            max_msg_size=8 * 1024 * 1024,
        )
    except Exception as exc:  # noqa: BLE001
        await ws_client.send_str(f"upstream error: {exc}")
        await ws_client.close()
        return ws_client

    async def client_to_upstream() -> None:
        async for message in ws_client:
            if message.type == WSMsgType.TEXT:
                await upstream.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await upstream.send_bytes(message.data)
            elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    async def upstream_to_client() -> None:
        async for message in upstream:
            if message.type == WSMsgType.TEXT:
                await ws_client.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await ws_client.send_bytes(message.data)
            elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    try:
        await asyncio.gather(client_to_upstream(), upstream_to_client())
    finally:
        await upstream.close()
        if not ws_client.closed:
            await ws_client.close()
    return ws_client


def is_websocket(request: web.Request) -> bool:
    return "websocket" in request.headers.get("Upgrade", "").lower()
