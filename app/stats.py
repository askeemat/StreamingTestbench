"""In-memory request log and statistics for segment generation."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass

from .config import settings


@dataclass
class RequestRecord:
    time: float
    client: str
    user_agent: str
    stream_id: str
    stream_name: str
    kind: str  # manifest | video | audio | init
    number: int
    status: int
    bytes: int
    gen_ms: float  # time to produce the response
    seg_duration: float  # media duration of the segment (s)
    attempts: int = 1
    verified: bool = True
    error: str = ""

    @property
    def realtime_ratio(self) -> float:
        """Generation time / media duration. Must stay well below 1."""
        return (self.gen_ms / 1000) / self.seg_duration if self.seg_duration > 0 else 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["realtime_ratio"] = self.realtime_ratio
        return d


class RequestLog:
    def __init__(self, size: int):
        self.records: deque[RequestRecord] = deque(maxlen=size)
        self.started = time.time()
        self.total = 0
        self.errors = 0

    def add(self, rec: RequestRecord) -> None:
        self.records.append(rec)
        self.total += 1
        if rec.status >= 400:
            self.errors += 1

    def clear(self) -> None:
        self.records.clear()
        self.total = 0
        self.errors = 0

    def recent(self, limit: int = 100) -> list[RequestRecord]:
        return list(self.records)[-limit:][::-1]

    def summary(self) -> dict:
        segs = [r for r in self.records if r.kind in ("video", "audio") and r.status < 400]
        out: dict = {"total": self.total, "errors": self.errors, "segments": len(segs)}
        for kind in ("video", "audio"):
            items = [r for r in segs if r.kind == kind]
            if not items:
                out[kind] = None
                continue
            ms = sorted(r.gen_ms for r in items)
            ratios = [r.realtime_ratio for r in items]
            dur = sum(r.seg_duration for r in items)
            out[kind] = {
                "count": len(items),
                "avg_ms": sum(ms) / len(ms),
                "p95_ms": ms[min(len(ms) - 1, int(len(ms) * 0.95))],
                "max_ms": ms[-1],
                "max_ratio": max(ratios),
                "throughput_mbps": sum(r.bytes for r in items) * 8 / dur / 1e6 if dur else 0.0,
                "unverified": sum(1 for r in items if not r.verified),
                "retries": sum(1 for r in items if r.attempts > 1),
            }
        return out


request_log = RequestLog(settings.request_log_size)
