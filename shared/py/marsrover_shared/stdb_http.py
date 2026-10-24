"""SpacetimeDB HTTP writer (docs/02 §8).

Python has no SpacetimeDB SDK, so writers call reducers over
`POST /v1/database/<db>/call/<reducer>` with a JSON array of positional args.
Never call STDB on the control path: the brain submits through RateLimitedWriter.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)


class StdbError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"STDB HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class StdbClient:
    RETRIES = 2  # connection errors only: the request never reached the server

    def __init__(
        self,
        base_url: str,
        db: str,
        token: str,
        timeout_s: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.db = db
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s, transport=transport
        )

    @classmethod
    def from_env(cls, token_var: str = "STDB_TOKEN_BRAIN", **kw: Any) -> StdbClient:
        """Build from config/.env vars (exported by scripts/_env.sh)."""
        return cls(
            os.environ.get("STDB_URL", "http://192.168.50.10:3000"),
            os.environ.get("STDB_DB", "marsrover"),
            os.environ.get(token_var, ""),
            **kw,
        )

    def _post(self, path: str, **kw: Any) -> httpx.Response:
        for attempt in range(self.RETRIES + 1):
            try:
                resp = self._http.post(path, **kw)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Not retrying read timeouts: the reducer may already have run.
                if attempt == self.RETRIES:
                    raise
                time.sleep(0.05 * (attempt + 1))
        if not resp.is_success:
            raise StdbError(resp.status_code, resp.text)
        return resp

    def call(self, reducer: str, *args: Any) -> None:
        # UNVERIFIED: positional args map in declaration order (U-11, checked at I0).
        self._post(f"/v1/database/{self.db}/call/{reducer}", json=list(args))

    def sql(self, query: str) -> list[dict[str, Any]]:
        """Run SQL and return the first statement's rows as dicts. Tests/debug only."""
        resp = self._post(f"/v1/database/{self.db}/sql", content=query.encode())
        # UNVERIFIED: response shape is
        #   [{"schema": {"elements": [{"name": {"some": col}}]}, "rows": [...]}]
        stmts = resp.json()
        if not stmts:
            return []
        names = []
        for el in stmts[0]["schema"]["elements"]:
            name = el.get("name")
            names.append(name.get("some") if isinstance(name, dict) else name)
        return [dict(zip(names, row, strict=False)) for row in stmts[0]["rows"]]

    def close(self) -> None:
        self._http.close()


class RateLimitedWriter:
    """Background writer so callers never block on STDB.

    Keys listed in `hz` are coalesced: only the latest submit per key is sent, at most
    `hz[key]` times per second (pose 2, status 2, telemetry 1). Any other key is a
    must-send write (candidate, visit, analysis): queued FIFO, bounded, oldest dropped
    on overflow. `on_drop(reason, count)` lets the owner `log_event('stdb_backpressure')`.
    """

    def __init__(
        self,
        client: StdbClient,
        hz: dict[str, float],
        max_queue: int = 1000,
        on_drop: Callable[[str, int], None] | None = None,
    ) -> None:
        self._client = client
        self._period = {k: 1.0 / v for k, v in hz.items()}
        self._latest: dict[str, tuple[str, tuple[Any, ...]]] = {}
        self._next_due: dict[str, float] = {}
        self._queue: deque[tuple[str, tuple[Any, ...]]] = deque()
        self._max_queue = max_queue
        self._on_drop = on_drop
        self._dropped = 0
        self._cv = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="stdb-writer", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        return self._dropped

    def submit(self, key: str, reducer: str, *args: Any) -> None:
        with self._cv:
            if key in self._period:
                self._latest[key] = (reducer, args)
            else:
                if len(self._queue) >= self._max_queue:
                    self._queue.popleft()
                    self._dropped += 1
                    if self._on_drop:
                        self._on_drop("queue_full", self._dropped)
                self._queue.append((reducer, args))
            self._cv.notify()

    def close(self, flush: bool = True, timeout_s: float = 5.0) -> None:
        with self._cv:
            if flush:
                for key in list(self._latest):
                    self._next_due[key] = 0.0
            else:
                self._queue.clear()
                self._latest.clear()
            self._closed = True
            self._cv.notify()
        self._thread.join(timeout_s)

    def _take(self) -> list[tuple[str, tuple[Any, ...]]]:
        """Under the lock: pop everything due now, or wait until something is."""
        while True:
            now = time.monotonic()
            batch = list(self._queue)
            self._queue.clear()
            wait = None
            for key in list(self._latest):
                due = self._next_due.get(key, 0.0)
                if due <= now:
                    batch.append(self._latest.pop(key))
                    self._next_due[key] = now + self._period[key]
                else:
                    wait = due - now if wait is None else min(wait, due - now)
            if batch or (self._closed and not self._latest):
                return batch
            self._cv.wait(wait)

    def _run(self) -> None:
        while True:
            with self._cv:
                batch = self._take()
                done = self._closed and not batch and not self._latest
            if done:
                return
            for reducer, args in batch:
                try:
                    self._client.call(reducer, *args)
                except Exception as e:  # DB down or rejected: events.jsonl is the backup
                    self._dropped += 1
                    log.warning("stdb write %s failed: %s", reducer, e)
                    if self._on_drop:
                        self._on_drop(f"{reducer}: {e}", self._dropped)
