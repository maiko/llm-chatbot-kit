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
            return True
        now_ts = self.now()
        ts = self._bucket_for(dim, key)
        windows = self.caps[dim]
        if windows:
            self._prune(ts, max(window for window, _ in windows), now_ts)
        for window, max_events in windows:
            if sum(stamp >= now_ts - window for stamp in ts) >= max_events:
                return False
        ts.append(now_ts)
        return True

    def reserve(self, keys: Dict[str, str]) -> float:
        """Atomically reserve one event across dimensions, or return its delay.

        Denied attempts consume no capacity in any dimension. The small delay
        margin respects the inclusive cutoff used by legacy ``allow`` callers.
        """
        now_ts = self.now()
        buckets = []
        delay = 0.0
        for dim, key in keys.items():
            windows = self.caps.get(dim, [])
            if not windows:
                continue
            if any(window <= 0 or maximum <= 0 for window, maximum in windows):
                raise ValueError("Rate limit windows and capacities must be positive")
            ts = self._bucket_for(dim, key)
            self._prune(ts, max(window for window, _ in windows), now_ts)
            buckets.append(ts)
            for window, maximum in windows:
                active = [stamp for stamp in ts if stamp >= now_ts - window]
                if len(active) >= maximum:
                    delay = max(delay, active[-maximum] + window - now_ts + 0.001)
        if delay == 0.0:
            for ts in buckets:
                ts.append(now_ts)
        return delay
