"""Bounded worker and socket delivery without network I/O."""

import asyncio
import json
import threading
import time

from simple_network_dashboard import config, main
from simple_network_dashboard.ssh_manager import SSHManager, _Session
from simple_network_dashboard.ws_manager import _WSManager


class _Socket:
    def __init__(self, stall=False):
        self.frames = []
        self.closed = []
        self.stall = stall
        self.sending = asyncio.Event()

    async def accept(self):
        return None

    async def send_text(self, data):
        self.sending.set()
        if self.stall:
            await asyncio.Event().wait()
        self.frames.append(json.loads(data))

    async def close(self, *, code):
        self.closed.append(code)


class _OpenLoop:
    def is_closed(self):
        return False

    def call_soon_threadsafe(self, _callback):
        return None


def _queued(manager, msg):
    size = len(json.dumps(msg))
    manager._delivery.queue.append((msg, size))
    manager._delivery.bytes += size


def test_worker_pushes_reach_broadcast_in_order_without_merging():
    async def exercise():
        delivered = []

        async def broadcast(msg):
            delivered.append(msg)

        manager = SSHManager(broadcast)
        manager.set_loop(asyncio.get_running_loop())
        manager._push({"type": "one"})
        manager._push({"type": "two"})
        manager._push({"type": "three"})
        for _ in range(5):
            await asyncio.sleep(0)
        manager.shutdown_delivery()
        return delivered

    assert asyncio.run(exercise()) == [
        {"type": "one"}, {"type": "two"}, {"type": "three"},
    ]


def test_terminal_byte_and_item_limits_reserve_capacity_for_non_terminal(monkeypatch):
    monkeypatch.setattr(config, "DELIVERY_QUEUE_MAX_BYTES", 100)
    monkeypatch.setattr(config, "DELIVERY_QUEUE_MAX_ITEMS", 10)
    monkeypatch.setattr(config, "DELIVERY_RESERVED_FRACTION", 0.10)
    manager = SSHManager(lambda _msg: None)
    manager._delivery._loop = _OpenLoop()
    _queued(manager, {"data": "x" * 78})

    result = []
    thread = threading.Thread(
        target=lambda: result.append(manager._push_terminal({"data": "x"}, lambda: True)))
    thread.start()
    time.sleep(0.03)
    assert thread.is_alive()
    manager._push({"type": "ssh_status"})
    assert len(manager._delivery.queue) == 2
    manager.shutdown_delivery()
    thread.join(timeout=1)
    assert result == [False]

    item_manager = SSHManager(lambda _msg: None)
    for number in range(8):
        _queued(item_manager, {"n": number})
    assert item_manager._push_terminal({"n": 9}, lambda: True) is True
    # The tenth terminal item would exceed the 90% item reservation.
    result = []
    thread = threading.Thread(
        target=lambda: result.append(item_manager._push_terminal({"n": 10}, lambda: True)))
    thread.start()
    time.sleep(0.03)
    assert thread.is_alive()
    item_manager.shutdown_delivery()
    thread.join(timeout=1)
    assert result == [False]


def test_blocked_terminal_producer_proceeds_after_drain_pops(monkeypatch):
    monkeypatch.setattr(config, "DELIVERY_QUEUE_MAX_ITEMS", 2)
    monkeypatch.setattr(config, "DELIVERY_QUEUE_MAX_BYTES", 1000)

    async def exercise():
        delivered = []

        async def broadcast(msg):
            delivered.append(msg)

        manager = SSHManager(broadcast)
        manager.set_loop(asyncio.get_running_loop())
        _queued(manager, {"type": "held"})
        _queued(manager, {"type": "held"})
        result = []
        thread = threading.Thread(
            target=lambda: result.append(manager._push_terminal({"type": "terminal"}, lambda: True)))
        thread.start()
        await asyncio.sleep(0)
        assert thread.is_alive()
        manager._delivery.wake()
        for _ in range(50):
            await asyncio.sleep(0)
            if len(delivered) == 3:
                break
        thread.join(timeout=1)
        manager.shutdown_delivery()
        return result, delivered

    result, delivered = asyncio.run(exercise())
    assert result == [True]
    assert delivered == [{"type": "held"}, {"type": "held"}, {"type": "terminal"}]


def test_terminal_wait_stops_when_continue_is_false(monkeypatch):
    monkeypatch.setattr(config, "DELIVERY_QUEUE_MAX_BYTES", 100)
    manager = SSHManager(lambda _msg: None)
    _queued(manager, {"data": "x" * 78})
    calls = 0

    def should_continue():
        nonlocal calls
        calls += 1
        return calls == 1

    assert manager._push_terminal({"data": "x"}, should_continue) is False


def test_shutdown_delivery_wakes_a_blocked_terminal_producer():
    manager = SSHManager(lambda _msg: None)
    _queued(manager, {"data": "x" * (4 * 1024 * 1024)})
    result = []
    thread = threading.Thread(
        target=lambda: result.append(manager._push_terminal({"type": "terminal"}, lambda: True)))
    thread.start()
    time.sleep(0.03)
    manager.shutdown_delivery()
    thread.join(timeout=1)
    assert result == [False]


def test_stale_generation_is_skipped_when_socket_dequeues_it():
    async def exercise():
        socket_manager = _WSManager()
        socket = _Socket()
        await socket_manager.connect(socket)
        await socket_manager.send_init(socket, {"type": "init"})

        async def broadcast(msg):
            still_current = msg.pop("_still_current", None)
            await socket_manager.broadcast(msg, still_current=still_current)

        delivery_manager = SSHManager(broadcast)
        original = _Session({"host": "host", "username": "user"}, "pw", "owner", "d1")
        original.generation = 1
        delivery_manager.sessions["d1"] = original
        await delivery_manager._deliver({"type": "ssh_log", "device_id": "d1", "_gen": 1})
        replacement = _Session({"host": "host", "username": "user"}, "pw", "owner", "d1")
        replacement.generation = 2
        delivery_manager.sessions["d1"] = replacement
        for _ in range(4):
            await asyncio.sleep(0)
        socket_manager.drop(socket)
        return socket.frames

    assert asyncio.run(exercise()) == [{"type": "init"}]


def test_stalled_socket_does_not_delay_other_socket_or_revalidation(monkeypatch):
    monkeypatch.setattr(config, "SOCKET_SEND_DEADLINE", 0.02)

    async def socket_exercise():
        manager = _WSManager()
        stalled, healthy = _Socket(stall=True), _Socket()
        await manager.connect(stalled)
        await manager.connect(healthy)
        await manager.send_init(stalled, {"type": "init"})
        await manager.send_init(healthy, {"type": "init"})
        await stalled.sending.wait()
        await manager.broadcast({"type": "metrics"})
        for _ in range(5):
            await asyncio.sleep(0)
        assert healthy.frames == [{"type": "init"}, {"type": "metrics"}]
        await asyncio.sleep(0.03)
        return stalled.closed

    assert asyncio.run(socket_exercise()) == [1013]

    async def revalidation_exercise():
        monkeypatch.setattr(main, "WS_REVALIDATE_SECONDS", 0.01)
        manager = _WSManager()
        socket = _Socket(stall=True)
        socket.query_params = {}
        socket.cookies = {main.SESSION_COOKIE_NAME: "token"}

        async def receive_text():
            await asyncio.Event().wait()

        socket.receive_text = receive_text
        monkeypatch.setattr(main, "ws_mgr", manager)
        monkeypatch.setattr(main, "_websocket_request_is_valid", lambda _ws: _async_true())
        monkeypatch.setattr(main, "_session_token_is_valid", lambda _token: _async_false())
        monkeypatch.setattr(main.ssh_mgr, "owner_ssh_snapshot", lambda _owner: ([], []))
        await main.ws_endpoint(socket)
        return socket.closed

    async def _async_true():
        return True

    async def _async_false():
        return False

    assert asyncio.run(revalidation_exercise()) == [main.WS_POLICY_VIOLATION_CODE]


def test_socket_over_byte_limit_is_closed_and_dropped(monkeypatch):
    monkeypatch.setattr(config, "SOCKET_QUEUE_MAX_BYTES", 20)

    async def exercise():
        manager = _WSManager()
        socket = _Socket()
        await manager.connect(socket)
        await manager.send_init(socket, {"type": "i"})
        await manager.broadcast({"type": "message-that-overflows"})
        await asyncio.sleep(0)
        return socket.closed, socket in manager._connections

    assert asyncio.run(exercise()) == ([1013], False)


def test_init_precedes_held_broadcasts_and_cached_metrics():
    async def exercise():
        manager = _WSManager()
        socket = _Socket()
        await manager.connect(socket)
        await manager.broadcast({"type": "before-init"})
        await manager.send_init(socket, {"type": "init"})
        await manager.send_to(socket, {"type": "metrics"})
        # Python 3.11's wait_for adds loop turns per send, so wait on the
        # frame count rather than a fixed number of yields.
        for _ in range(100):
            if len(socket.frames) >= 3:
                break
            await asyncio.sleep(0)
        manager.drop(socket)
        return socket.frames

    assert asyncio.run(exercise()) == [
        {"type": "init"}, {"type": "before-init"}, {"type": "metrics"},
    ]


def test_a_failed_delivery_does_not_stop_later_messages():
    async def exercise():
        delivered, logged = [], []

        async def broadcast(msg):
            if msg["type"] == "bad":
                raise RuntimeError("socket gone")
            delivered.append(msg)

        manager = SSHManager(broadcast, logged.append)
        manager.set_loop(asyncio.get_running_loop())
        manager._push({"type": "bad"})
        manager._push({"type": "good"})
        for _ in range(5):
            await asyncio.sleep(0)
        manager.shutdown_delivery()
        return delivered, logged

    delivered, logged = asyncio.run(exercise())
    assert delivered == [{"type": "good"}]
    assert logged == ["Outbound message delivery failed (RuntimeError)"]
