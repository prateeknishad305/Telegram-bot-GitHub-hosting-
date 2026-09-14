from __future__ import annotations

import asyncio
import json
import threading

from aiohttp import WSMsgType, web

from .docker_runner import DockerRunner


async def bridge_terminal(ws: web.WebSocketResponse, runner: DockerRunner, container_id: str) -> None:
    try:
        exec_id, _shell = await asyncio.to_thread(runner.exec_shell, container_id)
        sock = await asyncio.to_thread(runner.exec_stream, exec_id)
    except Exception as exc:  # noqa: BLE001
        await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
        await ws.close()
        return

    raw = getattr(sock, "_sock", sock)
    try:
        raw.setblocking(True)
    except OSError:
        pass

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    closed = threading.Event()

    def reader() -> None:
        while not closed.is_set():
            try:
                data = raw.recv(8192)
            except OSError:
                break
            if not data:
                break
            loop.call_soon_threadsafe(queue.put_nowait, data)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    reader_thread = threading.Thread(target=reader, name=f"term-{container_id[:8]}", daemon=True)
    reader_thread.start()

    async def pump_output() -> None:
        while True:
            data = await queue.get()
            if data is None:
                await ws.close()
                return
            if ws.closed:
                return
            await ws.send_bytes(data)

    async def pump_input() -> None:
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                await asyncio.to_thread(_send_all, raw, message.data)
            elif message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "resize":
                    rows = int(payload.get("rows", 24))
                    cols = int(payload.get("cols", 80))
                    await asyncio.to_thread(runner.resize_exec, exec_id, rows, cols)

    try:
        await asyncio.gather(pump_output(), pump_input())
    except (ConnectionResetError, RuntimeError):
        pass
    finally:
        closed.set()
        try:
            raw.close()
        except OSError:
            pass
        if not ws.closed:
            await ws.close()


def _send_all(sock, data: bytes) -> None:
    try:
        sock.sendall(data)
    except OSError:
        pass
