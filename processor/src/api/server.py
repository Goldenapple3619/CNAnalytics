"""Runs the API (uvicorn) in a background thread next to the processor."""
from __future__ import annotations

import threading
import time
from typing import Optional

import uvicorn

from ..config import Settings
from ..processor import Processor
from .app import create_app


class ApiServer(object):
    def __init__(self, processor: Processor, settings: Settings):
        config = uvicorn.Config(create_app(processor, settings), host=settings.api_host,
                                port=settings.api_port, log_level=settings.log_level.lower(),
                                timeout_graceful_shutdown=3, access_log=False)
        self.server = uvicorn.Server(config)
        self.settings = settings
        self._thread: Optional[threading.Thread] = None

    def start(self, timeout: float = 10.0) -> None:
        self._thread = threading.Thread(target=self.server.run, name="api", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"API failed to start on {self.settings.api_host}:{self.settings.api_port} "
                                   f"(port already in use?)")
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
