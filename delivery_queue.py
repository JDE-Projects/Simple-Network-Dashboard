"""Bounded handoff of outbound messages from worker threads to the event loop.

One FIFO shared by every SSH worker, bounded by bytes and items, drained by
a single task on the event loop. Terminal output waits for room
(backpressure to the device); every other message goes in at once and may
use the capacity reserved for it."""

import asyncio
import collections
import json
import threading

import config


class DeliveryQueue:
    def __init__(self, deliver, debug_write):
        """deliver: async callable(msg) run on the event loop for each item.
        debug_write: callable(str) for fixed failure lines, never content."""
        self._deliver = deliver
        self._debug_write = debug_write
        self._loop = None          # set via start() after uvicorn starts
        self.queue = collections.deque()
        self.bytes = 0
        self._condition = threading.Condition()
        self._event = None
        self._task = None
        self._shutdown = False

    def start(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._event = asyncio.Event()
        self._task = loop.create_task(self._drain())

    def push(self, msg: dict):
        """Queue a non-terminal message without blocking its caller."""
        data = json.dumps(msg)
        with self._condition:
            if self._shutdown or not self._loop or self._loop.is_closed():
                return
            was_empty = not self.queue
            size = len(data)
            self.queue.append((msg, size))
            self.bytes += size
            if was_empty:
                self.wake()

    def push_terminal(self, msg: dict, should_continue) -> bool:
        """Queue terminal output, waiting for capacity. Never call this while
        holding a device lock: it can wait as long as the browser is slow."""
        data = json.dumps(msg)
        size = len(data)
        with self._condition:
            while True:
                if self._shutdown or not should_continue():
                    return False
                byte_limit = config.DELIVERY_QUEUE_MAX_BYTES * (1 - config.DELIVERY_RESERVED_FRACTION)
                item_limit = config.DELIVERY_QUEUE_MAX_ITEMS * (1 - config.DELIVERY_RESERVED_FRACTION)
                if self.bytes + size <= byte_limit and len(self.queue) + 1 <= item_limit:
                    was_empty = not self.queue
                    self.queue.append((msg, size))
                    self.bytes += size
                    if was_empty:
                        self.wake()
                    return True
                self._condition.wait(timeout=0.25)

    def wake(self):
        loop = self._loop
        event = self._event
        if loop and event and not loop.is_closed():
            loop.call_soon_threadsafe(event.set)

    async def _drain(self):
        while True:
            await self._event.wait()
            while True:
                with self._condition:
                    if not self.queue:
                        self._event.clear()
                        break
                    msg, size = self.queue.popleft()
                    self.bytes -= size
                    self._condition.notify_all()
                try:
                    await self._deliver(msg)
                except Exception as exc:
                    # One failed message must not stop delivery for the rest.
                    self._debug_write(f"Outbound message delivery failed ({type(exc).__name__})")
                await asyncio.sleep(0)

    def shutdown(self):
        """Wake terminal producers and stop the drain task."""
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
        if self._task and not self._task.done():
            self._loop.call_soon_threadsafe(self._task.cancel)
