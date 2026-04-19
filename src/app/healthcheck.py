import asyncio
import contextlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, path: Path, interval_seconds: float = 5.0) -> None:
        self._path = path
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._run(), name="heartbeat")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except TimeoutError:
            self._task.cancel()
        self._task = None
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            log.warning("Failed to remove heartbeat file", extra={"path": str(self._path)})

    async def _run(self) -> None:
        log.info("Heartbeat started", extra={"path": str(self._path)})
        while not self._stop.is_set():
            try:
                self._path.touch(exist_ok=True)
            except OSError:
                log.exception(
                    "Failed to update heartbeat file",
                    extra={"path": str(self._path)},
                )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        log.info("Heartbeat stopped")
