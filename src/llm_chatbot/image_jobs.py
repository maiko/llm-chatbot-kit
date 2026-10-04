"""Single-process durable admission, generation and delivery queue."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Awaitable, Callable

from .image_client import BackendBusy, ImageClient, ImageError
from .image_config import ImageConfig

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "running", "ready", "delivering")


class JobStore:
    def __init__(self, cfg: ImageConfig):
        self.cfg = cfg
        self.progress_by_job: dict[str, dict] = {}
        self.on_change: Callable[[], None] | None = None
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg.state_dir.chmod(0o700)
        self.lock = (cfg.state_dir / "worker.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("An image worker already owns IMAGE_STATE_DIR") from None
        self.db = sqlite3.connect(cfg.state_dir / "jobs.sqlite3")
        self.db.row_factory = sqlite3.Row
        (cfg.state_dir / "jobs.sqlite3").chmod(0o600)
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, payload TEXT, state TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL, error TEXT, message_id TEXT,
            language TEXT NOT NULL, filesize_limit INTEGER NOT NULL, options TEXT, status_message_id TEXT, status_text TEXT)""")
        if "options" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN options TEXT")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name in ("status_message_id", "status_text"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
        self.db.execute("UPDATE jobs SET state='unknown', payload=NULL, error='restart_during_generation' WHERE state='running'")
        self.db.execute("UPDATE jobs SET state='delivery_unknown', error='restart_during_upload' WHERE state='delivering'")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS image_preferences (guild_id TEXT, user_id TEXT, mode TEXT NOT NULL, PRIMARY KEY(guild_id,user_id))"
        )
        self.db.commit()
        for row in self.db.execute("SELECT id FROM jobs WHERE state NOT IN ('queued','running','ready','delivering')").fetchall():
            self.clear_delivery_prompt(row["id"])

    def cleanup(self) -> None:
        cutoff = time.time() - self.cfg.retention_hours * 3600
        rows = self.db.execute(
            "SELECT id FROM jobs WHERE updated < ? AND state NOT IN ('queued','running','ready','delivering','unknown')", (cutoff,)
        ).fetchall()
        for row in rows:
            self.artifact(row["id"]).unlink(missing_ok=True)
        with self.db:
            self.db.execute(
                "DELETE FROM jobs WHERE updated < ? AND state NOT IN ('queued','running','ready','delivering','unknown')", (cutoff,)
            )

    def artifact(self, job_id: str) -> Path:
        # IDs originate from Discord snowflakes, not a user-provided path.
        if not job_id.isdecimal():
            raise ValueError("Invalid job ID")
        return self.cfg.state_dir / (job_id + ".png")

    def get(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return dict(row, progress=self.progress_by_job.get(job_id))

    def latest(self, user_id: int, guild_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE user_id=? AND guild_id=? ORDER BY created DESC LIMIT 1", (str(user_id), str(guild_id))
        ).fetchone()
        return self.get(row["id"]) if row else None

    def preferred_mode(self, user_id: int, guild_id: int) -> str:
        row = self.db.execute("SELECT mode FROM image_preferences WHERE guild_id=? AND user_id=?", (str(guild_id), str(user_id))).fetchone()
        return row[0] if row and row[0] in self.cfg.mode_presets else self.cfg.default_mode

    def set_mode(self, user_id: int, guild_id: int, mode: str) -> None:
        if mode not in self.cfg.mode_presets:
            raise ImageError("invalid_image_mode")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO image_preferences VALUES (?,?,?)", (str(guild_id), str(user_id), mode))

    def admit(
        self,
        job_id: int,
        user_id: int,
        guild_id: int,
        channel_id: int,
        payload: dict,
        language: str,
        filesize_limit: int,
        preset: str | None = None,
    ) -> dict:
        self.cleanup()
        existing = self.get(str(job_id))
        if existing:
            return existing
        with self.db:
            if self.db.execute(
                "SELECT 1 FROM jobs WHERE user_id=? AND state IN ('queued','running','ready','delivering')", (str(user_id),)
            ).fetchone():
                raise ImageError("user_busy")
            count = self.db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','ready','delivering')").fetchone()[0]
            if count >= self.cfg.queue_limit:
                raise ImageError("queue_full")
            since = time.time() - 86400
            used = self.db.execute("SELECT count(*) FROM jobs WHERE user_id=? AND created >= ?", (str(user_id), since)).fetchone()[0]
            if used >= self.cfg.daily_limit:
                raise ImageError("daily_limit")
            now = time.time()
            self.db.execute(
                "INSERT INTO jobs (id,user_id,guild_id,channel_id,payload,state,created,updated,language,filesize_limit,options) "
                "VALUES (?,?,?,?,?,'queued',?,?,?,?,?)",
                (
                    str(job_id),
                    str(user_id),
                    str(guild_id),
                    str(channel_id),
                    json.dumps(payload),
                    now,
                    now,
                    language,
                    filesize_limit,
                    json.dumps(
                        {key: payload[key] for key in ("model", "quality", "size", "seed") if key in payload}
                        | ({"prompt": payload["prompt"]} if self.cfg.include_prompt or self.cfg.quality_rerun_enabled else {})
                        | ({"preset": preset} if preset else {})
                    ),
                ),
            )
        self.notify()
        return self.get(str(job_id))

    def update(self, job_id: str, state: str, error: str | None = None, message_id: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state=?,error=?,message_id=?,updated=?,"
                "payload=CASE WHEN ? IN ('queued','running') THEN payload ELSE NULL END WHERE id=?",
                (state, error, message_id, time.time(), state, job_id),
            )
        if state != "running":
            self.progress_by_job.pop(job_id, None)
        logger.info("image_job id=%s state=%s error=%s", job_id, state, error)
        if state not in ACTIVE:
            self.clear_delivery_prompt(job_id)
        self.notify()

    def set_progress(self, job_id: str, progress: dict) -> None:
        job = self.get(job_id)
        if job and job["state"] == "running" and self.progress_by_job.get(job_id) != progress:
            self.progress_by_job[job_id] = dict(progress)
            self.notify()

    def clear_delivery_prompt(self, job_id: str) -> None:
        job = self.get(job_id)
        if job:
            options = json.loads(job["options"] or "{}")
            retain = self.cfg.quality_rerun_enabled and job["state"] in {"sent", "delivery_failed", "delivery_unknown"}
            if "prompt" in options and not retain:
                del options["prompt"]
                with self.db:
                    self.db.execute("UPDATE jobs SET options=? WHERE id=?", (json.dumps(options), job_id))

    def notify(self) -> None:
        if self.on_change:
            self.on_change()

    def queue_snapshot(self, job_id: str) -> dict:
        rows = self.db.execute(
            "SELECT id,state FROM jobs WHERE state IN ('queued','running','ready','delivering') ORDER BY created,id"
        ).fetchall()
        ids = [row["id"] for row in rows]
        return {
            "position": ids.index(job_id) + 1 if job_id in ids else None,
            "total": len(rows),
            "waiting": sum(row["state"] == "queued" for row in rows),
            "paused": self.uncertain(),
        }

    def status_jobs(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE status_message_id NOT IN ('pending','unavailable') "
            "AND state NOT IN ('delivering','sent') ORDER BY created,id"
        ).fetchall()
        return [dict(row) for row in rows]

    def reserve_status(self, job_id: str) -> bool:
        with self.db:
            cursor = self.db.execute("UPDATE jobs SET status_message_id='pending' WHERE id=? AND status_message_id IS NULL", (job_id,))
        return cursor.rowcount == 1

    def save_status(self, job_id: str, message_id: str, content: str | None = None) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status_message_id=?,status_text=? WHERE id=?", (message_id, content, job_id))

    def busy(self) -> bool:
        return bool(
            self.db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running','ready','delivering','unknown') LIMIT 1").fetchone()
        )

    def uncertain(self) -> bool:
        return bool(self.db.execute("SELECT 1 FROM jobs WHERE state='unknown' LIMIT 1").fetchone())

    def next_job(self) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE state IN ('queued','ready') ORDER BY created LIMIT 1").fetchone()
        return dict(row) if row else None

    def cancel(self, job_id: str, user_id: int, guild_id: int) -> bool:
        with self.db:
            cursor = self.db.execute(
                "UPDATE jobs SET state='cancelled',payload=NULL,updated=? WHERE id=? AND user_id=? AND guild_id=? AND state='queued'",
                (time.time(), job_id, str(user_id), str(guild_id)),
            )
        if cursor.rowcount:
            self.clear_delivery_prompt(job_id)
            self.notify()
        return cursor.rowcount == 1

    def close(self) -> None:
        self.db.close()
        self.lock.close()


class ImageWorker:
    def __init__(self, store: JobStore, client: ImageClient, deliver: Callable[[dict, Path], Awaitable[str]]):
        self.store, self.client, self.deliver = store, client, deliver
        self.execution_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="image-worker")

    async def process(self, job: dict) -> None:
        job_id = job["id"]
        if job["state"] == "queued":
            self.store.update(job_id, "running")
            try:
                for attempt in range(3):
                    try:
                        payload = json.loads(job["payload"])
                        if self.store.cfg.progress_enabled:
                            image = await self.client.generate(
                                payload, on_progress=lambda progress: self.store.set_progress(job_id, progress)
                            )
                        else:
                            image = await self.client.generate(payload)
                        break
                    except BackendBusy as exc:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(exc.retry_after)
                path = self.store.artifact(job_id)
                temporary = path.with_suffix(".tmp")
                with temporary.open("wb") as output:
                    os.chmod(temporary, 0o600)
                    output.write(image)
                temporary.replace(path)
                self.store.update(job_id, "ready")
            except asyncio.CancelledError:
                self.store.update(job_id, "unknown", "shutdown_during_generation")
                raise
            except ImageError as exc:
                code = str(exc)
                self.store.update(job_id, "unknown" if code == "outcome_unknown" else "failed", code)
                return
            except Exception:
                self.store.update(job_id, "unknown", "local_generation_failure")
                return
        await self.deliver_job(self.store.get(job_id))

    async def deliver_job(self, job: dict) -> None:
        job_id = job["id"]
        self.store.update(job_id, "delivering")
        try:
            message_id = await self.deliver(job, self.store.artifact(job_id))
            self.store.update(job_id, "sent", message_id=str(message_id))
        except asyncio.CancelledError:
            self.store.update(job_id, "delivery_unknown", "shutdown_during_upload")
            raise
        except ImageError as exc:
            self.store.update(job_id, "delivery_failed", str(exc))
        except Exception:
            # Upload may have succeeded: no automatic second send.
            self.store.update(job_id, "delivery_unknown", "upload_outcome_unknown")

    async def run(self) -> None:
        while True:
            self.wake.clear()
            self.store.cleanup()
            job = self.store.next_job()
            if job and not self.store.uncertain():
                async with self.execution_lock:
                    # A queued job may have been cancelled while waiting for text.
                    current = self.store.get(job["id"])
                    if current["state"] in {"queued", "ready"}:
                        await self.process(current)
            else:
                try:
                    await asyncio.wait_for(self.wake.wait(), 60)
                except asyncio.TimeoutError:
                    pass

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.client.close()
        self.store.close()
