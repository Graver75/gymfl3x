"""In-memory job queue / load tracker for concurrent coach requests."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

HISTORY_LIMIT = 40


@dataclass
class JobInfo:
    job_id: str
    user_id: int | None
    user_label: str | None
    kind: str
    enqueued_at: float
    started_at: float | None = None


@dataclass
class HistoryEntry:
    job_id: str
    user_id: int | None
    user_label: str | None
    kind: str
    status: str  # ok | error | cancelled
    enqueued_at: float
    started_at: float | None
    finished_at: float
    finished_wall: str
    error: str | None = None

    @property
    def duration_sec(self) -> float:
        start = self.started_at or self.enqueued_at
        return max(0.0, self.finished_at - start)

    @property
    def wait_sec(self) -> float:
        if self.started_at is None:
            return round(self.finished_at - self.enqueued_at, 1)
        return round(max(0.0, self.started_at - self.enqueued_at), 1)


class LoadTracker:
    def __init__(self, max_concurrent: int = 1, history_limit: int = HISTORY_LIMIT) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._lock = asyncio.Lock()
        self.queued: dict[str, JobInfo] = {}
        self.active: dict[str, JobInfo] = {}
        self.history: deque[HistoryEntry] = deque(maxlen=max(10, history_limit))
        self.completed_total = 0
        self.failed_total = 0

    def _push_history(self, entry: HistoryEntry) -> None:
        self.history.appendleft(entry)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        queued = []
        active = []
        for j in list(self.queued.values()):
            queued.append(
                {
                    "job_id": j.job_id,
                    "user_id": j.user_id,
                    "user_label": j.user_label,
                    "kind": j.kind,
                    "status": "queued",
                    "waiting_sec": round(now - j.enqueued_at, 1),
                }
            )
        for j in list(self.active.values()):
            started = j.started_at or j.enqueued_at
            active.append(
                {
                    "job_id": j.job_id,
                    "user_id": j.user_id,
                    "user_label": j.user_label,
                    "kind": j.kind,
                    "status": "running",
                    "running_sec": round(now - started, 1),
                    "waited_sec": round((j.started_at or now) - j.enqueued_at, 1),
                }
            )
        queued.sort(key=lambda x: -x["waiting_sec"])
        active.sort(key=lambda x: -x["running_sec"])
        history = [
            {
                "job_id": h.job_id,
                "user_id": h.user_id,
                "user_label": h.user_label,
                "kind": h.kind,
                "status": h.status,
                "duration_sec": round(h.duration_sec, 1),
                "wait_sec": h.wait_sec,
                "finished_at": h.finished_wall,
                "error": h.error,
            }
            for h in list(self.history)
        ]
        return {
            "max_concurrent": self.max_concurrent,
            "active_count": len(active),
            "queued_count": len(queued),
            "active": active,
            "queued": queued,
            "history": history,
            "history_limit": self.history.maxlen,
            "completed_total": self.completed_total,
            "failed_total": self.failed_total,
        }

    async def run(
        self,
        *,
        user_id: int | None,
        user_label: str | None,
        kind: str,
        work: Callable[[], Awaitable[T]],
    ) -> T:
        job_id = uuid.uuid4().hex[:12]
        info = JobInfo(
            job_id=job_id,
            user_id=user_id,
            user_label=user_label,
            kind=kind,
            enqueued_at=time.monotonic(),
        )
        async with self._lock:
            self.queued[job_id] = info

        try:
            await self._sem.acquire()
        except asyncio.CancelledError:
            finished = time.monotonic()
            async with self._lock:
                self.queued.pop(job_id, None)
                self.failed_total += 1
                self._push_history(
                    HistoryEntry(
                        job_id=job_id,
                        user_id=user_id,
                        user_label=user_label,
                        kind=kind,
                        status="cancelled",
                        enqueued_at=info.enqueued_at,
                        started_at=None,
                        finished_at=finished,
                        finished_wall=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        error="cancelled_before_start",
                    )
                )
            raise

        async with self._lock:
            self.queued.pop(job_id, None)
            info.started_at = time.monotonic()
            self.active[job_id] = info

        status = "ok"
        err_text: str | None = None
        try:
            return await work()
        except asyncio.CancelledError:
            status = "cancelled"
            err_text = "cancelled"
            raise
        except Exception as exc:
            status = "error"
            err_text = str(exc)[:200]
            raise
        finally:
            finished = time.monotonic()
            async with self._lock:
                self.active.pop(job_id, None)
                if status == "ok":
                    self.completed_total += 1
                else:
                    self.failed_total += 1
                self._push_history(
                    HistoryEntry(
                        job_id=job_id,
                        user_id=user_id,
                        user_label=user_label,
                        kind=kind,
                        status=status,
                        enqueued_at=info.enqueued_at,
                        started_at=info.started_at,
                        finished_at=finished,
                        finished_wall=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        error=err_text,
                    )
                )
            self._sem.release()
