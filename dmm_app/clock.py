from __future__ import annotations

import time
import threading
from datetime import datetime, timedelta


class AcquisitionClock:
    """One monotonic timebase shared by every acquisition worker."""

    def __init__(
        self,
        wall_start: datetime | None = None,
        monotonic_start_ns: int | None = None,
    ):
        self._lock = threading.Lock()
        self._wall_start = wall_start or datetime.now().astimezone()
        self._monotonic_start_ns = time.monotonic_ns() if monotonic_start_ns is None else monotonic_start_ns
        self._run_index = 0

    @property
    def wall_start(self) -> datetime:
        with self._lock:
            return self._wall_start

    def reset(self) -> None:
        with self._lock:
            self._wall_start = datetime.now().astimezone()
            self._monotonic_start_ns = time.monotonic_ns()
            self._run_index += 1

    def capture(self) -> tuple[datetime, float, int]:
        monotonic_now = time.monotonic_ns()
        with self._lock:
            elapsed_seconds = (monotonic_now - self._monotonic_start_ns) / 1_000_000_000
            timestamp = self._wall_start + timedelta(seconds=elapsed_seconds)
            run_index = self._run_index
        return timestamp, elapsed_seconds, run_index
