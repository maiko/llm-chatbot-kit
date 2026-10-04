"""Bounded FIFO for addressed replies; one consumer per bot process."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ReplyQueueFull(Exception):
    """The pending reply capacity is exhausted."""


class ReplyQueue:
    def __init__(self, respond, user, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError("TEXT_QUEUE_LIMIT must be between 1 and 100")
        self.respond, self.user = respond, user
        self.queue = asyncio.Queue(maxsize=limit)
        self.admission = asyncio.Lock()
        self.task = None
        self.active = False
        self.closed = False

    @property
    def busy(self):
        return self.active or not self.queue.empty()

    async def reaction(self, message, add):
        method = getattr(message, "add_reaction" if add else "remove_reaction", None)
        if method is None:
            return
        try:
            if add:
                await method("📝")
            elif self.user():
                await method("📝", self.user())
        except Exception as exc:
            # A missing reaction permission must not lose an admitted reply.
            logger.warning("reply_queue reaction_failed=%s", type(exc).__name__)

    async def submit(self, message, history):
        async with self.admission:
            if self.closed:
                return
            if self.queue.full():
                raise ReplyQueueFull()
            await self.reaction(message, True)
            if self.closed:
                await self.reaction(message, False)
                return
            future = asyncio.get_running_loop().create_future()
            self.queue.put_nowait((message, history, future))
            logger.info("reply_queue admitted id=%s pending=%d", message.id, self.queue.qsize())
            if self.task is None:
                self.task = asyncio.create_task(self.run(), name="text-replies")
        await future

    async def run(self):
        while True:
            message, history, future = await self.queue.get()
            self.active = True
            error = None
            try:
                if not future.cancelled():
                    await self.respond(message, True, history)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:
                logger.warning("reply_queue response_failed=%s", type(exc).__name__)
                error = exc
            finally:
                await self.reaction(message, False)
                self.active = False
                self.queue.task_done()
                logger.info("reply_queue finished id=%s pending=%d", message.id, self.queue.qsize())
                if not future.done():
                    if error:
                        future.set_exception(error)
                    else:
                        future.set_result(None)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        while not self.queue.empty():
            message, _, future = self.queue.get_nowait()
            if not future.done():
                future.cancel()
            await self.reaction(message, False)
            self.queue.task_done()
            logger.info("reply_queue cancelled id=%s", message.id)
