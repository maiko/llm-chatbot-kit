"""Persistent, requester-authorized quality regeneration button."""

from __future__ import annotations

import discord


class QualityView(discord.ui.View):
    def __init__(self, feature, job_id: str, language: str):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label="Refaire en qualité" if language == "fr" else "Regenerate in quality",
            emoji="✨",
            custom_id=f"image-quality:{job_id}",
            style=discord.ButtonStyle.secondary,
        )

        async def regenerate(interaction):
            await feature.rerun_quality(interaction, job_id)

        button.callback = regenerate
        self.add_item(button)
