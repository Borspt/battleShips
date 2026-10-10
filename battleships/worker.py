"""Killable strategy process: a slow or broken strategy cannot own the socket."""
import asyncio
import importlib
import multiprocessing as mp
import time


def _run(connection, strategy_path: str):
    try:
        module, class_name = strategy_path.split(":", 1)
        strategy = getattr(importlib.import_module(module), class_name)()
        connection.send((True, "ready"))
        while True:
            method, args = connection.recv()
            try:
                connection.send((True, getattr(strategy, method)(*args)))
            except Exception as exc:
                connection.send((False, type(exc).__name__))
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception as exc:
        try:
            connection.send((False, type(exc).__name__))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()


class StrategyWorker:
    def __init__(self, path: str = "battleships.strategy:BaselineStrategy"):
        self.path = path
        self.process = None
        self.connection = None

    async def _receive(self, deadline: float):
        while time.monotonic() < deadline:
            if self.connection.poll():
                ok, result = self.connection.recv()
                if not ok:
                    raise RuntimeError(f"Strategy failed: {result}")
                return result
            if not self.process.is_alive():
                raise RuntimeError("Strategy exited")
            await asyncio.sleep(0.002)
        raise TimeoutError("Strategy time budget exhausted")

    async def start(self, timeout: float = 5.0):
        if self.process is not None:
            return
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        self.connection = parent
        self.process = context.Process(target=_run, args=(child, self.path), daemon=True)
        self.process.start()
        child.close()
        try:
            await self._receive(time.monotonic() + timeout)
        except BaseException:
            await self.close()
            raise

    async def call(self, method: str, args: tuple, timeout: float):
        deadline = time.monotonic() + timeout
        try:
            await self.start(timeout)
            self.connection.send((method, args))
            return await self._receive(deadline)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        process, connection = self.process, self.connection
        self.process, self.connection = None, None
        if connection is not None:
            connection.close()
        if process is not None:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 0.1)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 0.1)
            if not process.is_alive():
                process.close()
