from __future__ import annotations

import copy
import socket
import threading
import time

import uvicorn

from .app import DEFAULT_CONFIG, MOCK_EMAIL, MOCK_PASSWORD, MockState, create_app

__all__ = ["MockServer", "MOCK_EMAIL", "MOCK_PASSWORD"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MockServer:
    def __init__(self) -> None:
        self.state = MockState()
        self.port = _free_port()
        config = uvicorn.Config(create_app(self.state), host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> MockServer:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("the mock server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def reset(self, **config) -> None:
        self.state.reset()
        self.configure(**config)

    def configure(self, **config) -> None:
        unknown = set(config) - set(DEFAULT_CONFIG)
        if unknown:
            raise KeyError(f"unknown mock setting(s): {sorted(unknown)}")
        with self.state.lock:
            self.state.config.update(config)

    def set_work_orders(self, items: list[dict]) -> None:
        self.state.set_work_orders(items)

    def stats(self) -> dict:
        with self.state.lock:
            return copy.deepcopy(self.state.stats)

    def expire_sessions(self) -> None:
        with self.state.lock:
            self.state.sessions.clear()
