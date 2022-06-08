import os
import asyncio

import pytest

from oxalis.amqp import App



@pytest.mark.asyncio
async def test_amqp():
    app = App("amqp://root:letmein@rabbitmq:5672/")
    await app.connect()
    async with app.connection.channel() as channel:
        q = await channel.declare_queue("test_queue")
        e = await channel.declare_exchange("test_change")
        await q.bind(e, routing_key="test")

    x = 1
    y = 1

    @app.register()
    def task():
        nonlocal x
        x = 2
        return 1

    @app.register(queue=q, exchange=e, routing_key="test")
    def task2():
        nonlocal y
        y = 2
        return 1
    
    async def close():
        await asyncio.sleep(1)
        app.close_worker()

    asyncio.ensure_future(close())

    await app.send_task(task)
    app.running = True
    app.on_worker_init()
    app._run_worker()
    await asyncio.sleep(0.3)
    await app.send_task(task2)
    await app.loop()
    assert x == 2
    assert y == 2