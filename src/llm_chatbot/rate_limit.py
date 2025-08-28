from __future__ import annotations

import time
from typing import Callable, Dict, List, Tuple

Window = Tuple[int, int]  # (window_seconds, max_events)


class MultiKeySlidingWindow:
    def __init__(
        self,
        caps: Dict[str, List[Window]],
        buckets: Dict[str, Dict[str, List[float]]] | None = None,
        now_func: Callable[[], float] | None = None,
    ) -> None:
        self.caps = {dim: [(int(w), int(m)) for (w, m) in caps_list] for dim, caps_list in (caps or {}).items()}
        self.buckets = buckets if buckets is not None else {}
        self.now = now_func or time.time

    def _bucket_for(self, dim: str, key: str) -> List[float]:
        d = self.buckets.setdefault(dim, {})
        return d.setdefault(str(key), [])

    def _prune(self, timestamps: List[float], window: int, now_ts: float) -> None:
        cutoff = now_ts - float(window)
        i = 0
        n = len(timestamps)
        while i < n and timestamps[i] < cutoff:
            i += 1
        if i > 0:
            del timestamps[:i]

    def allow(self, dim: str, key: str) -> bool:
        if dim not in self.caps:
            ts = self._bucket_for(dim, key)
            ts.append(self.now())
            return True
        now_ts = self.now()
        ts = self._bucket_for(dim, key)
        for window, max_events in self.caps[dim]:
            self._prune(ts, window, now_ts)
            if len(ts) >= max_events:
                return False
        ts.append(now_ts)
        return True
