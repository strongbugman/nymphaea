from __future__ import annotations

import typing as tp
import asyncio
import abc
import inspect
import json
import os
import sys
import multiprocessing
import signal
import logging


logger = logging.getLogger("oxalis")


from .pool import Pool


class Task:
    def __init__(self, app: App, func: tp.Callable, name="") -> None:
        self.app = app
        self.func = func
        self.name = name or self.get_name()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}({self.name})>"

    async def __call__(self, *args: tp.Any, **kwargs: tp.Any) -> tp.Any:
        ret = self.func(*args, **kwargs)
        if inspect.iscoroutine(ret):
            ret = await ret

        return ret
    
    async def delay(self, *args, **kwargs):
        await self.app.send_task(self, *args, **kwargs)

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


class App(abc.ABC):
    def __init__(
        self, task_codec: TaskCodec = TaskCodec(), pool: Pool = Pool()
    ) -> None:
        self.tasks: tp.Dict[str, Task] = {}
        self.task_codec = task_codec
        self.pool = pool
        self.running = False
        self._on_close_signal_count = 0

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
        for _ in range(4):
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
    
    @abc.abstractmethod
    def _run_worker(self):
        pass

    async def work(self):
        while self.running:
            await asyncio.sleep(0.5)
        await self.pool.close()
        await self.disconnect()

    def close_worker(self, force: bool = False):
        logger.info(f"Close worker{'(force)' if force else ''}: {self}...")
        self.running = False
        if force:
            sys.exit()

    def register(self, task_name: str = "", **kwargs) -> tp.Callable[[tp.Callable], Task]:
        def wrapped(func):
            task = Task(self, func, name=task_name)
            if task.name in self.tasks:
                raise ValueError("double task, check task name")
            self.tasks[task.name] = task
            return task

        return wrapped
    
    async def on_message_receive(self, content: bytes, *args):
        if not content:
            return
        task_name, task_args, task_kwargs = self.task_codec.decode(content)
        if task_name not in self.tasks:
            logger.exception(f"Task {task_name} not found")
        else:
            await self.pool.spawn(self.exec_task(self.tasks[task_name], *args, *task_args, **task_kwargs), block=True)
    
    def close(self, *_):
        self._on_close_signal_count += 1
        self.close_worker(force=self._on_close_signal_count >= 2)

    def on_worker_init(self):
        pass
