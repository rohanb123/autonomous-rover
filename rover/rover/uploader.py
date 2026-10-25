"""Pi -> mapper uploads (doc 00 sections 6.4-6.5), backed by LocalStore so nothing is lost.

    uploader.upload_station(rel)  -> MapperStationReply   one try; UploadFailed if it didn't land
    uploader.upload_sample(rel)   -> SampleReply
    uploader.start() / stop()     background retry thread, backoff 1 s doubling to 30 s
    uploader.backlog              -> telemetry.uplink_backlog

The op that captured a bundle saves it first, then makes ONE upload attempt on its own worker
thread. If that fails the op still answers `done` (station: `mapper.ok = false`, P2 role doc
section 5.6), an `upload_retry` event goes to the brain, and the retry thread keeps trying
from the SD card -- across reboots, because the queue is the store's index, not a list.

A 4xx is the mapper saying the bundle itself is wrong; retrying it forever would block every
station behind it, so it is marked rejected (files kept for a human) and the queue moves on.

httpx + pydantic + stdlib only: mocks/fake_rover.py imports `multipart` from here, and it
must keep running on a laptop with no Pi libraries.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from marsrover_shared.schemas import MapperStationReply, SampleReply
from rover.local_store import Bundle, LocalStore, StoreError

log = logging.getLogger("rover.uploader")

DEFAULT_MAPPER = "http://192.168.50.10:8200"  # doc 00 section 3
UPLOAD_TIMEOUT_S = 10.0
RETRY_MIN_S, RETRY_MAX_S = 1.0, 30.0  # P2 role doc section 4: exponential, max 30 s
IDLE_POLL_S = 5.0  # with nothing pending, look again this often (or when woken)

ENDPOINT = {"station": "/ingest/station", "sample": "/ingest/sample"}

Emit = Callable[[str, dict[str, Any]], None]


class UploadFailed(RuntimeError):
    def __init__(self, rel: str, reason: str) -> None:
        super().__init__(f"{rel}: {reason}")
        self.rel = rel
        self.reason = reason


def multipart(meta_name: str, meta_json: str, files: Mapping[str, bytes]) -> list[tuple]:
    """Doc 00 6.4-6.5: the metadata part first, then one part per file, named after the file."""
    parts: list[tuple] = [(meta_name, (meta_name, meta_json.encode(), "application/json"))]
    for name, data in sorted(files.items()):
        kind = "image/png" if name.endswith(".png") else "image/jpeg"
        parts.append((name, (name, data, kind)))
    return parts


def _rejected(e: Exception) -> bool:
    """True when retrying cannot help: a 4xx (other than timeout / rate limit) or bad data."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return 400 <= code < 500 and code not in (408, 429)
    return isinstance(e, StoreError)


class Uploader:
    def __init__(
        self,
        store: LocalStore,
        mapper_url: str = DEFAULT_MAPPER,
        emit: Emit | None = None,
        *,
        timeout_s: float = UPLOAD_TIMEOUT_S,
        retry_min_s: float = RETRY_MIN_S,
        retry_max_s: float = RETRY_MAX_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.store = store
        self.mapper_url = mapper_url.rstrip("/")
        self.emit: Emit = emit or (lambda kind, detail: None)
        self.timeout_s = timeout_s
        self.retry_min_s = retry_min_s
        self.retry_max_s = retry_max_s
        self._transport = transport
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def backlog(self) -> int:
        return self.store.backlog

    # --- one attempt -----------------------------------------------------------------------

    def upload_station(self, rel: str) -> MapperStationReply:
        return MapperStationReply.model_validate_json(self._attempt(rel, "station"))

    def upload_sample(self, rel: str) -> SampleReply:
        return SampleReply.model_validate_json(self._attempt(rel, "sample"))

    def _attempt(self, rel: str, expect: str | None = None) -> bytes:
        """POST one bundle; on failure record it, tell the brain, wake the retry thread."""
        try:
            bundle = self.store.load(rel)
            if expect is not None and bundle.kind != expect:
                raise StoreError(f"{rel} is a {bundle.kind}, not a {expect}")
            body = self._post(bundle)
            self._validate(bundle, body)
        except (httpx.HTTPError, StoreError, ValueError) as e:
            reason = str(e) or type(e).__name__
            if _rejected(e):
                log.error("upload %s rejected, not retrying: %s", rel, reason)
                self.store.mark_rejected(rel, reason)
            else:
                log.warning("upload %s failed: %s", rel, reason)
                self.store.mark_failed(rel, reason)
                self._wake.set()
            self.emit(
                "upload_retry",
                {"ref": rel, "error": reason, "rejected": _rejected(e), "backlog": self.backlog},
            )
            raise UploadFailed(rel, reason) from e
        self.store.mark_uploaded(rel)
        return body

    def _post(self, bundle: Bundle) -> bytes:
        parts = multipart(bundle.meta_name, bundle.meta_json, bundle.files)
        with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
            r = client.post(f"{self.mapper_url}{ENDPOINT[bundle.kind]}", files=parts)
            r.raise_for_status()
            return r.content

    @staticmethod
    def _validate(bundle: Bundle, body: bytes) -> None:
        """A 200 with an off-contract body is not a delivery: ValueError -> retried."""
        model = MapperStationReply if bundle.kind == "station" else SampleReply
        reply = model.model_validate_json(body)
        if not reply.ok:
            raise ValueError(f"mapper answered ok=false for {bundle.path}")

    # --- background retry ----------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.retry_loop, name="rover-upload", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None

    def retry_loop(self) -> None:
        """Drain pending bundles oldest-first; back off while the mapper is unreachable."""
        backoff = self.retry_min_s
        while not self._stop.is_set():
            pending = self.store.pending_uploads()
            if not pending:
                backoff = self.retry_min_s
                self._wake.wait(IDLE_POLL_S)
                self._wake.clear()
                continue
            # Wait first: whatever is pending just failed (or failed before a reboot).
            if self._stop.wait(backoff):
                return
            try:
                self._attempt(pending[0])
            except UploadFailed:
                backoff = min(backoff * 2, self.retry_max_s)
                continue
            log.info("uploaded %s on retry (%d left)", pending[0], len(pending) - 1)
            backoff = self.retry_min_s
