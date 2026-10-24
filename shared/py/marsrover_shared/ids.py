"""ID generators (AGENTS.md §4, doc 00 §5).

The brain mints r_ m_ t_ c_ v_ a_ (plus q_ rover command ids and u_ dashboard command
uids); the Pi mints s_ k_ p_.
IDs are unique within a run and zero-padded to 4 digits. Command ids (q_, u_) use 6 digits,
matching the doc 00 §6.2 example (`q_000123`).
"""

from __future__ import annotations

import re
import threading
from datetime import datetime

PREFIXES = frozenset("mskpctvaqu")
WIDTH = {"q": 6, "u": 6}
_ID_RE = re.compile(r"^([a-z])_(\d+)$")


def new_run_id(now: datetime) -> str:
    """`r_YYYYMMDD_HHMMSS` from the given time (the caller picks the timezone)."""
    return now.strftime("r_%Y%m%d_%H%M%S")


class IdGen:
    """Per-prefix counters for one run. Thread-safe."""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}
        self._lock = threading.Lock()

    def next(self, prefix: str) -> str:
        if prefix not in PREFIXES:
            raise ValueError(f"unknown id prefix {prefix!r}")
        with self._lock:
            n = self._last.get(prefix, 0) + 1
            self._last[prefix] = n
        return f"{prefix}_{n:0{WIDTH.get(prefix, 4)}d}"

    def observe(self, id_: str) -> None:
        """Bump the counter past an existing id (e.g. rows reloaded after a restart)."""
        m = _ID_RE.match(id_)
        if not m or m.group(1) not in PREFIXES:
            raise ValueError(f"not a run-scoped id: {id_!r}")
        prefix, n = m.group(1), int(m.group(2))
        with self._lock:
            self._last[prefix] = max(self._last.get(prefix, 0), n)
