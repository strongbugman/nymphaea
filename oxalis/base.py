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
import time
import typing as tp
from contextlib import suppress

from .pool import Pool

logger = logging.getLogger("oxalis")


TASK_TV = tp.TypeVar("TASK_TV", bound="Task")
PARAM = tp.ParamSpec("PARAM")
RT = tp.TypeVar("RT")


class Task(tp.Generic[PARAM, RT]):
    def __init__(
        self,
        oxalis: Oxalis,
        func: tp.Callable[PARAM, RT],
        name="",
        timeout: float = -1,
        pool: tp.Optional[Pool] = None,
    ) -> None:
        self.oxalis = oxalis
        self.func = func
        self.name = name or self.get_name()
        self.timeout = timeout
        self.pool = pool or oxalis.pools[0]

    def config(self: TASK_TV, **__) -> TASK_TV:
        return self

    def clean_config(self):
        pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}({self.name})>"

    async def __call__(self, *args: PARAM.args, **kwargs: PARAM.kwargs) -> RT:
        ret = self.func(*args, **kwargs)
        if inspect.iscoroutine(ret):
            ret = await ret

        return ret

    async def delay(self, *args: PARAM.args, **kwargs: PARAM.kwargs) -> None:
        if self.oxalis.test:
            await self.__call__(*args, **kwargs)
        else:
            await self.oxalis.send_task(self, *args, **kwargs)
        self.clean_config()

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


class Oxalis(abc.ABC, tp.Generic[TASK_TV]):
    READY_FILE_PATH: tp.ClassVar[str] = "/tmp/oxalis_ready"
    HEARTBEAT_FILE_PATH: tp.ClassVar[str] = "/tmp/oxalis_heartbeat"

    def __init__(
        self,
        task_cls: tp.Type[TASK_TV],
        task_codec: TaskCodec = TaskCodec(),
        pool: Pool = Pool(),
        timeout: float = 5.0,
        worker_num: int = 0,
        test: bool = False,
    ) -> None:
        self.task_cls = task_cls
        self.tasks: tp.Dict[str, TASK_TV] = {}
        self.task_codec = task_codec
        self.pools: tp.List[Pool] = [pool]
        self.running = False
        self.timeout = timeout
        self.test = test
        self._on_close_signal_count = 0
        self.worker_num = worker_num or os.cpu_count()
        self.is_worker = False
        self.consuming_count = 0
        self.health = True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(pid-{os.getpid()})>"

    @property
    def pool(self) -> Pool:
        return self.pools[0]

    async def connect(self):
        pass

    async def wait_close(self):
        while self.consuming_count:
            await asyncio.sleep(self.timeout)

    async def disconnect(self):
        pass

    @abc.abstractmethod
    async def send_task(
        self,
        task: TASK_TV,
        *task_args,
        **task_kwargs,
    ):
        pass

    async def exec_task(self, task: TASK_TV, *task_args, **task_kwargs):
        logger.debug(f"Worker {self} execute task {task}...")
        await task(*task_args, **task_kwargs)

    def run_worker_master(self):
        for task in self.tasks.values():
            logger.info(f"Registered Task: {task}")

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
        self.is_worker = True
        self.on_worker_init()
        asyncio.get_event_loop().run_until_complete(self.connect())
        self._run_worker()
        asyncio.get_event_loop().run_until_complete(self.work())
        self.on_worker_close()

    @abc.abstractmethod
    def _run_worker(self):
        pass

    async def work(self):
        with open(self.READY_FILE_PATH, "w") as f:
            f.write(f"{time.time():.0f}\n")
        while self.running:
            if self.health:
                with open(self.HEARTBEAT_FILE_PATH, "w") as f:
                    f.write(f"{time.time():.0f}\n")
            await asyncio.sleep(self.timeout)

        await self.wait_close()
        await asyncio.wait(
            [asyncio.get_event_loop().create_task(p.wait_close()) for p in self.pools],
        )
        await self.disconnect()
        with suppress(FileNotFoundError):
            os.remove(self.READY_FILE_PATH)
        with suppress(FileNotFoundError):
            os.remove(self.HEARTBEAT_FILE_PATH)

    def close_worker(self, force: bool = False):
        logger.info(f"Close worker{'(force)' if force else ''}: {self}...")
        self.running = False
        if force:
            logger.warning(f"Force close: {self}, may lose some message!")
            for p in self.pools:
                p.force_close()
            sys.exit()

    def register_task(self, task: TASK_TV):
        if task.name in self.tasks:
            raise ValueError("double task, check task name")
        self.tasks[task.name] = task

    def register(
        self,
        *,
        task_name: str = "",
        timeout: float = -1,
        pool: tp.Optional[Pool] = None,
        **_,
    ) -> tp.Callable[
        [tp.Callable[PARAM, tp.Union[tp.Awaitable[RT], RT]]], Task[PARAM, RT]
    ]:
        def wrapped(func: tp.Callable[PARAM, RT]):
            task = self.task_cls(self, func, name=task_name, timeout=timeout, pool=pool)
            self.register_task(task)
            return task

        return wrapped

    def load_task(self, content: bytes) -> tp.Tuple[TASK_TV, tp.Sequence, tp.Dict]:
        task_name, task_args, task_kwargs = self.task_codec.decode(content)
        if task_name not in self.tasks:
            raise ValueError(f"task_name {task_name} not founded!")

        return self.tasks[task_name], task_args, task_kwargs

    async def load_and_execute_task(self, content: bytes, *_):
        task, task_args, task_kwargs = self.load_task(content)
        await asyncio.wait_for(
            self.exec_task(task, *_, *task_args, **task_kwargs),
            timeout=self.pool.timeout if task.timeout == -1 else task.timeout,
        )

    async def on_message_receive(self, content: bytes, *_):
        """should not raise exception"""
        try:
            return await self.load_and_execute_task(content, *_)
        except Exception as e:
            logger.exception(e)

    def close(self, *_):
        """Close self but wait pool"""
        if not self.is_worker:
            return
        self._on_close_signal_count += 1
        self.close_worker(force=self._on_close_signal_count >= 2)

    def on_worker_init(self):
        pass

    def on_worker_close(self):
        pass
