from __future__ import annotations

import abc
import asyncio
import inspect
import json
import logging
import multiprocessing
import os
import signal
import sys
import typing as tp

from .pool import Pool

logger = logging.getLogger("oxalis")


class Task:
    def __init__(
        self, oxalis: Oxalis, func: tp.Callable, name="", timeout: float = -1
    ) -> None:
        self.oxalis = oxalis
        self.func = func
        self.name = name or self.get_name()
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}({self.name})>"

    async def __call__(self, *args: tp.Any, **kwargs: tp.Any) -> tp.Any:
        ret = self.func(*args, **kwargs)
        if inspect.iscoroutine(ret):
            ret = await ret

        return ret

    async def delay(self, *args, **kwargs) -> tp.Any:
        if self.oxalis.test:
            return await self(*args, **kwargs)
        else:
            await self.oxalis.send_task(self, *args, **kwargs)

    def get_name(self) -> str:
        return ".".join((self.func.__module__, self.func.__name__))


class TaskCodec:
    MESSAGE_TYPE = tp.Tuple[str, tp.Sequence[tp.Any], tp.Dict[str, tp.Any]]

    @classmethod
    def encode(
        cls,
        task: Task,
        task_args: tp.Sequence[tp.Any],
        task_kwargs: tp.Dict[str, tp.Any],
    ) -> bytes:
        return json.dumps([task.name, list(task_args), task_kwargs]).encode()

    @classmethod
    def decode(cls, content: bytes) -> MESSAGE_TYPE:
        return json.loads(content)


class Oxalis(abc.ABC):
    def __init__(
        self,
        task_cls: tp.Type[Task] = Task,
        task_codec: TaskCodec = TaskCodec(),
        pool: Pool = Pool(),
        timeout: float = 5.0,
        worker_num: int = 0,
        test: bool = False,
    ) -> None:
        self.task_cls = task_cls
        self.tasks: tp.Dict[str, Task] = {}
        self.task_codec = task_codec
        self.pool = pool
        self.running = False
        self.timeout = timeout
        self.test = test
        self._on_close_signal_count = 0
        self.worker_num = worker_num or os.cpu_count()
        self.pool_wait_spawn = True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(pid-{os.getpid()})>"

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    @abc.abstractmethod
    async def send_task(self, task: Task, *task_args, **task_kwargs):
        pass

    async def exec_task(self, task: Task, *task_args, **task_kwargs):
        logger.debug(f"Worker {self} execute task {task}...")
        await task(*task_args, **task_kwargs)

    def run_worker_master(self):
        signal.signal(signal.SIGINT, self.close)
        signal.signal(signal.SIGTERM, self.close)
        ps = []
        for _ in range(self.worker_num):
            ps.append(multiprocessing.Process(target=self.run_worker))
            ps[-1].start()
        for p in ps:
            p.join()

    def run_worker(self):
        logger.info(f"Run worker: {self}...")
        self.running = True
        self.on_worker_init()
        asyncio.get_event_loop().run_until_complete(self.connect())
        self._run_worker()
        asyncio.get_event_loop().run_until_complete(self.work())
        self.on_worker_close()

    @abc.abstractmethod
    def _run_worker(self):
        pass

    async def work(self):
        while self.running:
            await asyncio.sleep(self.timeout)
        await self.disconnect()
        await self.pool.wait_close()

    def close_worker(self, force: bool = False):
        logger.info(f"Close worker{'(force)' if force else ''}: {self}...")
        self.running = False
        if force:
            self.pool.fore_close()
            sys.exit()

    def register_task(self, task: Task):
        if task.name in self.tasks:
            raise ValueError("double task, check task name")
        self.tasks[task.name] = task

    def register(
        self, task_name: str = "", timeout: float = -1, **_
    ) -> tp.Callable[[tp.Callable], Task]:
        def wrapped(func):
            task = self.task_cls(self, func, name=task_name, timeout=timeout)
            self.register_task(task)
            return task

        return wrapped

    async def on_message_receive(self, content: bytes, *args):
        try:
            task_name, task_args, task_kwargs = self.task_codec.decode(content)
        except Exception as e:
            logger.exception(e)
            return

        if task_name not in self.tasks:
            logger.warning(f"Received task {task_name} not found")
        else:
            if self.pool_wait_spawn:
                await self.pool.wait_spawn(
                    self.exec_task(
                        self.tasks[task_name], *args, *task_args, **task_kwargs
                    ),
                    timeout=self.tasks[task_name].timeout,
                )
            else:
                self.pool.spawn(
                    self.exec_task(
                        self.tasks[task_name], *args, *task_args, **task_kwargs
                    ),
                    timeout=self.tasks[task_name].timeout,
                )

    def close(self, *_):
        self._on_close_signal_count += 1
        self.close_worker(force=self._on_close_signal_count >= 2)

    def on_worker_init(self):
        pass

    def on_worker_close(self):
        pass
