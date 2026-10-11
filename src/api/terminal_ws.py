"""Interactive terminal over a WebSocket: a bash on a PTY inside a long-running session.

Protocol (same for the API and the console):
  client → server  binary frames: keystrokes (raw bytes)
                   text frames:   JSON control, {"type": "resize", "rows": 40, "cols": 120}
  server → client  binary frames: terminal output (raw bytes, may split UTF-8 sequences)
The server closes with 1000 when the shell exits, 1008 when the session is gone.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Optional

from starlette.websockets import WebSocket, WebSocketDisconnect

MAX_INPUT = 64 * 1024


async def bridge(websocket: WebSocket, pty, on_input: Optional[Callable[[], None]] = None,
                 idle_seconds: int = 3600) -> None:
    """Pumps bytes both ways until either side ends. The caller has accepted the socket."""
    loop = asyncio.get_running_loop()
    output: asyncio.Queue = asyncio.Queue(maxsize=256)

    def reader():
        while True:
            data = pty.read()
            try:   # a client that stops reading for a minute (output piles up) ends the terminal
                asyncio.run_coroutine_threadsafe(asyncio.wait_for(output.put(data), 60), loop).result(65)
            except Exception:
                return
            if not data:
                return

    reader_task = loop.run_in_executor(None, reader)
    last_input = time.monotonic()

    async def pump_out():
        while True:
            data = await output.get()
            if not data:
                return
            await websocket.send_bytes(data)

    async def pump_in():
        nonlocal last_input
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                data = message["bytes"][:MAX_INPUT]
                await asyncio.to_thread(pty.write, data)
                last_input = time.monotonic()
                if on_input:
                    on_input()
            elif message.get("text") is not None:
                try:
                    control = json.loads(message["text"])
                except ValueError:
                    continue
                if isinstance(control, dict) and control.get("type") == "resize":
                    await asyncio.to_thread(pty.resize, control.get("rows", 24), control.get("cols", 80))

    async def idle_guard():
        while time.monotonic() - last_input < idle_seconds:
            await asyncio.sleep(30)

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in()), asyncio.create_task(idle_guard())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.to_thread(pty.close)
        try:
            await asyncio.wait_for(reader_task, timeout=5)
        except Exception:
            pass
        try:
            await websocket.close(1000)
        except Exception:
            pass
