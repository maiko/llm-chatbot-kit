"""Durable channel status messages, independent of interaction token expiry."""

from __future__ import annotations

import asyncio
import logging

import discord

from .image_jobs import ACTIVE, JobStore

logger = logging.getLogger(__name__)
STATES = {
    "en": {
        "queued": "Waiting",
        "running": "Generating",
        "ready": "Image ready",
        "delivering": "Uploading",
        "sent": "Delivered",
        "cancelled": "Cancelled",
        "failed": "Failed",
        "unknown": "Generation outcome uncertain",
        "delivery_failed": "Upload failed",
        "delivery_unknown": "Upload outcome uncertain",
    },
    "fr": {
        "queued": "En attente",
        "running": "Génération en cours",
        "ready": "Image prête",
        "delivering": "Envoi en cours",
        "sent": "Livrée",
        "cancelled": "Annulée",
        "failed": "Échec",
        "unknown": "Résultat de génération incertain",
        "delivery_failed": "Échec de l’envoi",
        "delivery_unknown": "Résultat de l’envoi incertain",
    },
}


def render_status(job: dict, snapshot: dict) -> str:
    language = job["language"] if job["language"] in STATES else "en"
    state = STATES[language].get(job["state"], job["state"])
    lines = [f"🎨 {job['id']} · {state}"]
    if job["state"] in ACTIVE:
        position = snapshot["position"] or "–"
        if language == "fr":
            lines.append(f"Position {position}/{snapshot['total']} · file : {snapshot['waiting']} en attente")
        else:
            lines.append(f"Position {position}/{snapshot['total']} · queue: {snapshot['waiting']} waiting")
    progress = job.get("progress")
    if job["state"] == "running" and progress:
        phase = progress.get("phase")
        labels = {
            "fr": {
                "preparing": "Préparation",
                "sampling": "Échantillonnage",
                "decoding": "Décodage",
                "saving": "Finalisation",
                "completed": "Image prête",
            },
            "en": {
                "preparing": "Preparing",
                "sampling": "Sampling",
                "decoding": "Decoding",
                "saving": "Finalizing",
                "completed": "Image ready",
            },
        }
        if phase in labels[language]:
            label = labels[language][phase]
            value, maximum = progress.get("value"), progress.get("max")
            if phase == "sampling" and type(value) is int and type(maximum) is int and 0 <= value <= maximum and maximum > 0:
                percent = value * 100 // maximum
                filled = value * 10 // maximum
                lines.append(f"{label} · [{'█' * filled}{'░' * (10 - filled)}] {percent}% · {value}/{maximum}")
            else:
                lines.append(label)
    if snapshot["paused"]:
        lines.append(
            "⏸ File suspendue · vérification par l’administrateur nécessaire."
            if language == "fr"
            else "⏸ Queue paused · administrator verification required."
        )
    if job["state"] in {"unknown", "delivery_unknown", "delivery_failed", "failed"}:
        lines.append(
            "Suivi : /image-status · image conservée : /image-result"
            if language == "fr"
            else "Details: /image-status · retained image: /image-result"
        )
    return "\n".join(lines)


class ImageStatus:
    def __init__(self, bot, store: JobStore):
        self.bot, self.store = bot, store
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.store.on_change = self.wake.set

    async def publish(self, job: dict, channel) -> None:
        # Reserve before awaiting Discord: duplicate delivery of an interaction must not publish twice.
        if job["state"] not in ACTIVE or not self.store.reserve_status(job["id"]):
            return
        async with self.lock:
            content = render_status(self.store.get(job["id"]), self.store.queue_snapshot(job["id"]))
            try:
                message = await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
                self.store.save_status(job["id"], str(message.id), content)
            except Exception as exc:
                # A status send can be ambiguous; do not automatically publish a duplicate.
                self.store.save_status(job["id"], "unavailable")
                logger.warning("image_status id=%s publish_failed=%s", job["id"], type(exc).__name__)
        self.wake.set()

    async def refresh(self) -> None:
        for listed in self.store.status_jobs():
            async with self.lock:
                job = self.store.get(listed["id"])
                if job["state"] in {"sent", "delivering"}:
                    continue
                content = render_status(job, self.store.queue_snapshot(job["id"]))
                if content == job["status_text"]:
                    continue
                try:
                    channel = self.bot.get_channel(int(job["channel_id"])) or await self.bot.fetch_channel(int(job["channel_id"]))
                    if not getattr(channel, "guild", None) or str(channel.guild.id) != job["guild_id"]:
                        continue
                    if int(job["guild_id"]) not in self.store.cfg.guild_ids or int(job["channel_id"]) not in self.store.cfg.channel_ids:
                        continue
                    message = channel.get_partial_message(int(job["status_message_id"]))
                    await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
                    self.store.save_status(job["id"], job["status_message_id"], content)
                except (discord.NotFound, discord.Forbidden):
                    self.store.save_status(job["id"], "unavailable")
                except Exception as exc:
                    logger.warning("image_status id=%s refresh_failed=%s", job["id"], type(exc).__name__)

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="image-status")

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            self.wake.clear()
            await self.refresh()
            try:
                await asyncio.wait_for(self.wake.wait(), 5)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        self.store.on_change = None
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
