import json
from typing import Optional

from fastapi import WebSocket


class _WSManager:
    def __init__(self):
        self._connections: list[WebSocket] = []
        self._owners: dict[WebSocket, str] = {}  # ws -> owner (browser id)
        self._selections: dict[WebSocket, str] = {}  # ws -> selected device id

    async def connect(self, ws: WebSocket, owner_id: str = None):
        await ws.accept()
        self._connections.append(ws)
        if owner_id:
            self._owners[ws] = owner_id

    def drop(self, ws: WebSocket):
        self._connections = [c for c in self._connections if c is not ws]
        self._owners.pop(ws, None)
        self._selections.pop(ws, None)

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

    async def broadcast(self, msg: dict):
        if not self._connections:
            return
        data = json.dumps(msg)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.drop(ws)

    async def send_to_owner(self, owner_id: str, msg: dict):
        if not self._connections or not owner_id:
            return
        data = json.dumps(msg)
        dead = []
        for ws in self._connections:
            if self._owners.get(ws) != owner_id:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.drop(ws)
