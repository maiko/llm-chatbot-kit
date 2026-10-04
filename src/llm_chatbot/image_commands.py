"""Guild-only image slash commands and channel delivery."""

from __future__ import annotations

import io
import json
from contextlib import ExitStack, closing
from typing import Optional

import discord
from discord import app_commands

from .image_client import ImageClient, ImageError, validate_request
from .image_config import ImageConfig
from .image_jobs import ACTIVE, ImageWorker, JobStore
from .image_quality import QualityView
from .image_status import ImageStatus, render_status

TEXT = {
    "en": {
        "denied": "Image access is not allowed here.",
        "queued": "Job {id}: {state}. The image will be posted in this channel. Use /image-status to follow it.",
        "status": "Job {id}: {state}. {error}",
        "none": "No image job found.",
        "cancelled": "Pending job cancelled.",
        "cancel_denied": "Only your own pending job can be cancelled.",
        "result_missing": "No retained image is available. An uncertain generation is never retried automatically.",
        "result": "Image job {id} · {preset} · {size}",
        "error": "Request refused: {error}.",
    },
    "fr": {
        "denied": "La génération d’images n’est pas autorisée ici.",
        "queued": "Demande {id} : {state}. L’image sera publiée dans ce salon. Suivi avec /image-status.",
        "status": "Demande {id} : {state}. {error}",
        "none": "Aucune demande d’image trouvée.",
        "cancelled": "Demande en attente annulée.",
        "cancel_denied": "Tu peux seulement annuler ta propre demande en attente.",
        "result_missing": "Aucune image conservée disponible. Une génération incertaine n’est jamais relancée automatiquement.",
        "result": "Image {id} · {preset} · {size}",
        "error": "Demande refusée : {error}.",
    },
}


def text(language: str, key: str, **values) -> str:
    return TEXT.get(language, TEXT["en"])[key].format(**values)


class ImageCommands:
    def __init__(self, bot, cfg: ImageConfig, language: str, owner_id: str | None = None):
        self.bot, self.cfg = bot, cfg
        self.owner_id = owner_id
        self.language = "fr" if (language or "en").startswith("fr") else "en"
        self.store = JobStore(cfg)
        self.status = ImageStatus(bot, self.store)
        self.worker = ImageWorker(self.store, ImageClient(cfg), self.deliver)
        self.register()

    def allowed(self, interaction: discord.Interaction) -> bool:
        roles = {role.id for role in getattr(interaction.user, "roles", [])}
        return not interaction.user.bot and self.cfg.permits(interaction.guild_id, interaction.channel_id, roles)

    async def gate(self, interaction: discord.Interaction) -> bool:
        if not self.allowed(interaction):
            await interaction.response.send_message(text(self.language, "denied"), ephemeral=True)
            return False
        return True

    def preferred_preset(self, message) -> str:
        if not message.guild:
            return self.cfg.default_preset
        mode = self.store.preferred_mode(message.author.id, message.guild.id)
        return self.cfg.mode_presets.get(mode, self.cfg.default_preset)

    async def enqueue(self, job_id, user_id, guild_id, channel, payload, filesize_limit, preset=None, source=None) -> dict:
        job = self.store.admit(job_id, user_id, guild_id, channel.id, payload, self.language, filesize_limit, preset=preset, source=source)
        try:
            await self.status.publish(job, channel)
        finally:
            self.worker.wake.set()
        return self.store.get(job["id"])

    async def from_message(self, message, arguments: dict, source=None) -> dict:
        """Tool arguments cannot choose a requester, destination, endpoint or model ID."""
        roles = {role.id for role in getattr(message.author, "roles", [])}
        if (
            not message.guild
            or message.author.bot
            or message.webhook_id
            or not self.cfg.permits(message.guild.id, message.channel.id, roles)
        ):
            raise ImageError("access_denied")
        if not isinstance(arguments, dict) or set(arguments) - {"prompt", "preset", "size"}:
            raise ImageError("invalid_tool_arguments")
        prompt = arguments.get("prompt")
        preset = arguments.get("preset", self.preferred_preset(message))
        if source is not None and (not self.cfg.edits_enabled or not self.cfg.presets.get(preset, {}).get("supports_edits", False)):
            raise ImageError("image_edit_unsupported")
        if source is not None and "size" in arguments:
            raise ImageError("invalid_tool_arguments")
        size = source.size if source is not None else arguments.get("size", "1024x1024")
        if not all(isinstance(value, str) for value in (prompt, preset, size)):
            raise ImageError("invalid_tool_arguments")
        payload = validate_request(prompt, preset, size, None, self.cfg.presets)
        permissions = message.channel.permissions_for(message.guild.me)
        send = permissions.send_messages_in_threads if isinstance(message.channel, discord.Thread) else permissions.send_messages
        if not permissions.view_channel or not send or not permissions.attach_files:
            raise ImageError("missing_channel_permissions")
        if not message.channel.permissions_for(message.author).view_channel:
            raise ImageError("requester_access_revoked")
        return await self.enqueue(
            message.id,
            message.author.id,
            message.guild.id,
            message.channel,
            payload,
            message.guild.filesize_limit,
            preset=preset,
            source=source.data if source is not None else None,
        )

    async def deliver(self, job: dict, path) -> str:
        await self.bot.wait_until_ready()
        if not self.cfg.permits(int(job["guild_id"]), int(job["channel_id"]), self.cfg.role_ids):
            raise ImageError("destination_no_longer_allowed")
        channel = self.bot.get_channel(int(job["channel_id"])) or await self.bot.fetch_channel(int(job["channel_id"]))
        if not getattr(channel, "guild", None) or str(channel.guild.id) != job["guild_id"]:
            raise ImageError("invalid_destination")
        # Recheck member roles and view permission after waiting in the queue.
        member = channel.guild.get_member(int(job["user_id"])) or await channel.guild.fetch_member(int(job["user_id"]))
        roles = {role.id for role in member.roles}
        if not self.cfg.permits(channel.guild.id, channel.id, roles) or not channel.permissions_for(member).view_channel:
            raise ImageError("requester_access_revoked")
        permissions = channel.permissions_for(channel.guild.me)
        send_permission = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
        if not permissions.view_channel or not send_permission or not permissions.attach_files:
            raise ImageError("missing_channel_permissions")
        if not path.exists():
            raise ImageError("artifact_missing")
        if path.stat().st_size > min(job["filesize_limit"], channel.guild.filesize_limit):
            raise ImageError("attachment_too_large")
        options = json.loads(job["options"] or "{}")
        caption = text(
            job["language"],
            "result",
            id=job["id"],
            preset=options.get("model", "image"),
            size=options.get("size", "?"),
            seed=options.get("seed", "?"),
        )
        if options.get("operation") == "edit":
            caption += " · " + ("Photo retouchée" if job["language"] == "fr" else "Edited photo")
        if "seed" in options:
            caption += f" · seed {options['seed']}"
        caption += f" · <@{job['user_id']}>"
        prompt = options.get("prompt") if self.cfg.include_prompt else None
        prompt_attachment = None
        if prompt:
            with_prompt = caption + "\n\nPrompt :\n" + prompt
            if len(with_prompt) <= 2000:
                caption = with_prompt
            else:
                prompt_attachment = prompt.encode("utf-8")
                if len(prompt_attachment) > min(job["filesize_limit"], channel.guild.filesize_limit):
                    raise ImageError("prompt_attachment_too_large")
                caption += f"\nPrompt : prompt-{job['id']}.txt"
        view = self.quality_view(job)
        async with self.status.lock:
            current = self.store.get(job["id"])
            status_id = current["status_message_id"]
            with ExitStack() as stack:
                attachments = [stack.enter_context(closing(discord.File(path, filename=f"image-{job['id']}.png")))]
                if prompt_attachment:
                    attachments.append(
                        stack.enter_context(closing(discord.File(io.BytesIO(prompt_attachment), filename=f"prompt-{job['id']}.txt")))
                    )
                if status_id and status_id.isdecimal():
                    message = channel.get_partial_message(int(status_id))
                    try:
                        delivered = await message.edit(
                            content=caption,
                            attachments=attachments,
                            view=view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return str(delivered.id)
                    except discord.NotFound:
                        # The placeholder was deleted, so a fresh send cannot duplicate it.
                        self.store.save_status(job["id"], "unavailable")
                        for attachment in attachments:
                            attachment.reset()
                message = await channel.send(caption, files=attachments, view=view, allowed_mentions=discord.AllowedMentions.none())
                return str(message.id)

    def own_job(self, interaction, job_id: str | None) -> dict | None:
        job = self.store.get(job_id) if job_id else self.store.latest(interaction.user.id, interaction.guild_id)
        if job and (job["user_id"] != str(interaction.user.id) or job["guild_id"] != str(interaction.guild_id)):
            return None
        return job

    def register(self) -> None:
        guilds = [discord.Object(id=value) for value in sorted(self.cfg.guild_ids)]

        @self.bot.tree.command(name="imagine", description="Generate an image with the configured image backend")
        @app_commands.guilds(*guilds)
        @app_commands.guild_only()
        @app_commands.choices(
            mode=[app_commands.Choice(name="Rapide / Fast", value="fast"), app_commands.Choice(name="Qualité / Quality", value="quality")]
        )
        @app_commands.choices(preset=[app_commands.Choice(name=name, value=name) for name in self.cfg.presets])
        async def imagine(
            interaction: discord.Interaction,
            prompt: str,
            preset: Optional[str] = None,
            size: str = "1024x1024",
            seed: Optional[str] = None,
            mode: Optional[str] = None,
        ):
            if not await self.gate(interaction):
                return
            # Defer before any persistence or remote work, including rejected jobs.
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                if mode is not None and (mode not in self.cfg.mode_presets or preset is not None):
                    raise ImageError("choose_mode_or_preset")
                selected_mode = mode or self.store.preferred_mode(interaction.user.id, interaction.guild_id)
                preset = preset or self.cfg.mode_presets.get(selected_mode, self.cfg.default_preset)
                # String avoids Discord's IEEE-754 integer precision limit for 64-bit seeds.
                payload = validate_request(prompt, preset, size, int(seed) if seed is not None else None, self.cfg.presets)
                if not interaction.app_permissions.attach_files or not interaction.app_permissions.view_channel:
                    raise ImageError("missing_channel_permissions")
                send_permission = (
                    interaction.app_permissions.send_messages_in_threads
                    if isinstance(interaction.channel, discord.Thread)
                    else interaction.app_permissions.send_messages
                )
                if not send_permission:
                    raise ImageError("missing_channel_permissions")
                job = await self.enqueue(
                    interaction.id,
                    interaction.user.id,
                    interaction.guild_id,
                    interaction.channel,
                    payload,
                    min(interaction.filesize_limit, interaction.guild.filesize_limit),
                    preset=preset,
                )
                current = self.store.get(job["id"])
                message = render_status(current, self.store.queue_snapshot(job["id"]))
            except (ImageError, ValueError) as exc:
                message = text(self.language, "error", error=str(exc) if isinstance(exc, ImageError) else "invalid_seed")
            await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        if self.cfg.edits_enabled:

            @self.bot.tree.command(name="image-edit", description="Edit an attached photo with an instruction")
            @app_commands.guilds(*guilds)
            @app_commands.choices(
                mode=[app_commands.Choice(name="Fast", value="fast"), app_commands.Choice(name="Quality", value="quality")]
            )
            async def image_edit(interaction: discord.Interaction, image: discord.Attachment, prompt: str, mode: Optional[str] = None):
                if not await self.gate(interaction):
                    return
                await interaction.response.defer(ephemeral=True, thinking=True)
                try:
                    from .discord_media import read_attachment

                    if mode is not None and mode not in self.cfg.mode_presets:
                        raise ImageError("invalid_image_mode")
                    selected = mode or self.store.preferred_mode(interaction.user.id, interaction.guild_id)
                    preset = self.cfg.mode_presets.get(selected, self.cfg.default_preset)
                    if not self.cfg.presets[preset].get("supports_edits", False):
                        raise ImageError("image_edit_unsupported")
                    perms = interaction.app_permissions
                    send = perms.send_messages_in_threads if isinstance(interaction.channel, discord.Thread) else perms.send_messages
                    if not perms.view_channel or not perms.attach_files or not send:
                        raise ImageError("missing_channel_permissions")
                    source = await read_attachment(image)
                    if not self.allowed(interaction) or not interaction.channel.permissions_for(interaction.user).view_channel:
                        raise ImageError("requester_access_revoked")
                    payload = validate_request(prompt, preset, source.size, None, self.cfg.presets)
                    job = await self.enqueue(
                        interaction.id,
                        interaction.user.id,
                        interaction.guild_id,
                        interaction.channel,
                        payload,
                        min(interaction.filesize_limit, interaction.guild.filesize_limit),
                        preset=preset,
                        source=source.data,
                    )
                    reply = render_status(job, self.store.queue_snapshot(job["id"]))
                except ImageError as exc:
                    from .image_tools import tool_error

                    reply = tool_error(self.language, str(exc))
                await interaction.followup.send(reply, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        if self.cfg.mode_presets:

            @self.bot.tree.command(name="image-mode", description="Choose your default image speed and quality in this server")
            @app_commands.guilds(*guilds)
            @app_commands.guild_only()
            @app_commands.choices(
                mode=[
                    app_commands.Choice(name="Rapide / Fast", value="fast"),
                    app_commands.Choice(name="Qualité / Quality", value="quality"),
                ]
            )
            async def image_mode(interaction: discord.Interaction, mode: str):
                if not await self.gate(interaction):
                    return
                self.store.set_mode(interaction.user.id, interaction.guild_id, mode)
                reply = ("Mode image : " if self.language == "fr" else "Image mode: ") + mode
                await interaction.response.send_message(reply, ephemeral=True)

        if self.cfg.quality_rerun_enabled:

            @self.bot.tree.command(name="image-quality", description="Explicitly regenerate your fast image using the quality preset")
            @app_commands.guilds(*guilds)
            @app_commands.guild_only()
            async def image_quality(interaction: discord.Interaction, job_id: Optional[str] = None):
                await self.rerun_quality(interaction, job_id)

        @self.bot.tree.command(name="image-status", description="Check your latest image job")
        @app_commands.guilds(*guilds)
        @app_commands.guild_only()
        async def status(interaction: discord.Interaction, job_id: Optional[str] = None):
            if not await self.gate(interaction):
                return
            job = self.own_job(interaction, job_id)
            message = render_status(job, self.store.queue_snapshot(job["id"])) if job else text(self.language, "none")
            if job and job["error"]:
                message += "\n" + text(self.language, "error", error=job["error"])
            await interaction.response.send_message(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        @self.bot.tree.command(name="image-cancel", description="Cancel your pending image job")
        @app_commands.guilds(*guilds)
        @app_commands.guild_only()
        async def cancel(interaction: discord.Interaction, job_id: Optional[str] = None):
            if not await self.gate(interaction):
                return
            job = self.own_job(interaction, job_id)
            cancelled = job and self.store.cancel(job["id"], interaction.user.id, interaction.guild_id)
            await interaction.response.send_message(text(self.language, "cancelled" if cancelled else "cancel_denied"), ephemeral=True)

        @self.bot.tree.command(name="image-resolve", description="Owner: release an uncertain job AFTER verifying the backend is idle")
        @app_commands.guilds(*guilds)
        @app_commands.guild_only()
        async def resolve(interaction: discord.Interaction, job_id: str, backend_idle_confirmed: bool):
            if (
                not self.owner_id
                or str(interaction.user.id) != self.owner_id
                or interaction.user.bot
                or interaction.guild_id not in self.cfg.guild_ids
                or interaction.channel_id not in self.cfg.channel_ids
                or not backend_idle_confirmed
            ):
                await interaction.response.send_message(text(self.language, "denied"), ephemeral=True)
                return
            job = self.store.get(job_id)
            if not job or job["guild_id"] != str(interaction.guild_id) or job["state"] != "unknown":
                await interaction.response.send_message(text(self.language, "none"), ephemeral=True)
                return
            self.store.update(job_id, "failed", "owner_resolved_after_idle_check")
            self.worker.wake.set()
            await interaction.response.send_message(text(self.language, "status", id=job_id, state="failed", error=""), ephemeral=True)

        @self.bot.tree.command(name="image-result", description="Retrieve your retained image privately without regenerating it")
        @app_commands.guilds(*guilds)
        @app_commands.guild_only()
        async def result(interaction: discord.Interaction, job_id: Optional[str] = None):
            if not await self.gate(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            job = self.own_job(interaction, job_id)
            if job and job["state"] in (*ACTIVE, "unknown"):
                await interaction.followup.send(
                    render_status(job, self.store.queue_snapshot(job["id"])),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            path = self.store.artifact(job["id"]) if job else None
            if not job or job["state"] not in {"sent", "delivery_failed", "delivery_unknown"} or not path.exists():
                await interaction.followup.send(text(self.language, "result_missing"), ephemeral=True)
                return
            if path.stat().st_size > interaction.filesize_limit:
                await interaction.followup.send(text(self.language, "error", error="attachment_too_large"), ephemeral=True)
                return
            with closing(discord.File(path, filename=f"image-{job['id']}.png")) as attachment:
                await interaction.followup.send(file=attachment, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    def quality_view(self, job: dict):
        options = json.loads(job["options"] or "{}")
        if (
            self.cfg.quality_rerun_enabled
            and options.get("preset") == self.cfg.mode_presets["fast"]
            and "prompt" in options
            and "seed" in options
        ):
            return QualityView(self, job["id"], job["language"])
        return None

    async def rerun_quality(self, interaction, job_id=None):
        if not await self.gate(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            self.store.cleanup()
            job = self.own_job(interaction, job_id)
            if not self.cfg.quality_rerun_enabled or not job or job["state"] not in {"sent", "delivery_failed", "delivery_unknown"}:
                raise ImageError("quality_source_unavailable")
            options = json.loads(job["options"] or "{}")
            if options.get("preset") != self.cfg.mode_presets["fast"] or "prompt" not in options or "seed" not in options:
                raise ImageError("quality_source_unavailable")
            if not interaction.channel.permissions_for(interaction.user).view_channel:
                raise ImageError("requester_access_revoked")
            permissions = interaction.app_permissions
            send = permissions.send_messages_in_threads if isinstance(interaction.channel, discord.Thread) else permissions.send_messages
            if not permissions.view_channel or not permissions.attach_files or not send:
                raise ImageError("missing_channel_permissions")
            source = None
            if options.get("operation") == "edit":
                source_path = self.store.source_artifact(job["id"])
                if not source_path.exists():
                    raise ImageError("quality_source_unavailable")
                source = source_path.read_bytes()
            preset = self.cfg.mode_presets["quality"]
            if source is not None and (not self.cfg.edits_enabled or not self.cfg.presets[preset].get("supports_edits", False)):
                raise ImageError("image_edit_unsupported")
            payload = validate_request(options["prompt"], preset, options["size"], options["seed"], self.cfg.presets)
            queued = await self.enqueue(
                interaction.id,
                interaction.user.id,
                interaction.guild_id,
                interaction.channel,
                payload,
                min(interaction.filesize_limit, interaction.guild.filesize_limit),
                preset=preset,
                source=source,
            )
            reply = render_status(queued, self.store.queue_snapshot(queued["id"]))
        except ImageError as exc:
            reply = text(self.language, "error", error=str(exc))
        await interaction.followup.send(reply, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def setup(self) -> None:
        self.store.cleanup()
        if self.cfg.quality_rerun_enabled:
            for row in self.store.db.execute("SELECT id,message_id FROM jobs WHERE state='sent' AND message_id IS NOT NULL").fetchall():
                view = self.quality_view(self.store.get(row["id"]))
                if view and row["message_id"].isdecimal():
                    self.bot.add_view(view, message_id=int(row["message_id"]))
        if self.cfg.sync_commands:
            for value in sorted(self.cfg.guild_ids):
                await self.bot.tree.sync(guild=discord.Object(id=value))
        self.status.start()
        self.worker.start()

    async def close(self) -> None:
        await self.status.close()
        await self.worker.close()
