import asyncio
import collections
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

import config
from fastapi import WebSocket


@dataclass
class _SocketState:
    queue: collections.deque = field(default_factory=collections.deque)
    queued_bytes: int = 0
    ready: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)
    sender: asyncio.Task = None


class _WSManager:
    def __init__(self):
        self._connections: list[WebSocket] = []
        self._owners: dict[WebSocket, str] = {}  # ws -> signed-in session owner
        self._selections: dict[WebSocket, str] = {}  # ws -> selected device id
        self._states: dict[WebSocket, _SocketState] = {}
        self._closing: set[asyncio.Task] = set()  # keeps close tasks alive until done

    async def connect(self, ws: WebSocket, owner_id: str = None):
        await ws.accept()
        self._connections.append(ws)
        state = _SocketState()
        self._states[ws] = state
        state.sender = asyncio.create_task(self._send_queued(ws, state))
        if owner_id:
            self._owners[ws] = owner_id

    async def send_init(self, ws: WebSocket, msg: dict):
        """Queue a socket's first frame before every held outbound frame."""
        state = self._states.get(ws)
        if state is None:
            return
        data = json.dumps(msg)
        if state.queued_bytes + len(data) > config.SOCKET_QUEUE_MAX_BYTES:
            self._close_socket(ws)
            return
        state.queue.appendleft((data, None))
        state.queued_bytes += len(data)
        state.ready = True
        state.event.set()

    async def send_to(self, ws: WebSocket, msg: dict, still_current: Callable[[], bool] = None):
        self._enqueue(ws, json.dumps(msg), still_current)

    def drop(self, ws: WebSocket):
        self._connections = [c for c in self._connections if c is not ws]
        self._owners.pop(ws, None)
        self._selections.pop(ws, None)
        state = self._states.pop(ws, None)
        if state is not None:
            state.queue.clear()
            state.queued_bytes = 0
            if state.sender and state.sender is not asyncio.current_task():
                state.sender.cancel()

    def _close_socket(self, ws: WebSocket):
        self.drop(ws)
        task = asyncio.create_task(self._close_with_1013(ws))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _close_with_1013(self, ws: WebSocket):
        try:
            await asyncio.wait_for(ws.close(code=1013), timeout=config.SOCKET_SEND_DEADLINE)
        except Exception:
            pass

    def _enqueue(self, ws: WebSocket, data: str, still_current: Callable[[], bool] = None):
        state = self._states.get(ws)
        if state is None:
            return
        if state.queued_bytes + len(data) > config.SOCKET_QUEUE_MAX_BYTES:
            self._close_socket(ws)
            return
        state.queue.append((data, still_current))
        state.queued_bytes += len(data)
        if state.ready:
            state.event.set()

    async def _send_queued(self, ws: WebSocket, state: _SocketState):
        try:
            while True:
                await state.event.wait()
                if not state.ready:
                    state.event.clear()
                    continue
                while state.queue:
                    data, still_current = state.queue.popleft()
                    state.queued_bytes -= len(data)
                    try:
                        current = still_current is None or still_current()
                    except Exception:
                        current = False
                    if current:
                        await asyncio.wait_for(
                            ws.send_text(data), timeout=config.SOCKET_SEND_DEADLINE)
                    await asyncio.sleep(0)
                state.event.clear()
        except (asyncio.CancelledError, Exception):
            if ws in self._states:
                self._close_socket(ws)

    def set_selected_device(self, ws: WebSocket, device_id: Optional[str]):
        """Record (or clear) the device this socket has selected. An empty
        or missing id removes the socket's entry rather than storing None."""
        if device_id:
            self._selections[ws] = device_id
        else:
            self._selections.pop(ws, None)

    def selected_device_ids(self) -> set[str]:
        """The union of device ids currently selected by any connected socket."""
        return set(self._selections.values())

    def unselect_device_everywhere(self, device_id: str):
        """Remove a device id from every socket's selection, e.g. after it
        has been deleted."""
        for ws, did in list(self._selections.items()):
            if did == device_id:
                self._selections.pop(ws, None)

    def owner_of(self, ws: WebSocket) -> str:
        return self._owners.get(ws)

    def owner_count(self, owner_id: str) -> int:
        return sum(1 for o in self._owners.values() if o == owner_id)

    async def broadcast(self, msg: dict, still_current: Callable[[], bool] = None):
        if not self._connections:
            return
        data = json.dumps(msg)
        for ws in list(self._connections):
            self._enqueue(ws, data, still_current)

    async def send_to_owner(self, owner_id: str, msg: dict,
                            still_current: Callable[[], bool] = None):
        if not self._connections or not owner_id:
            return
        data = json.dumps(msg)
        for ws in list(self._connections):
            if self._owners.get(ws) == owner_id:
                self._enqueue(ws, data, still_current)
