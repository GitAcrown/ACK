import asyncio
import functools
import logging
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from utils import dataio, pretty

logger = logging.getLogger("WANDER.Profil")

CARD_SIZE = 256
CORNER_RADIUS = 36
# Masque d'arrondi précalculé (réutilisé à chaque rendu)
_CORNER_MASK: Image.Image | None = None


def _corner_mask() -> Image.Image:
    global _CORNER_MASK
    if _CORNER_MASK is None:
        rad = CORNER_RADIUS
        mask = Image.new("L", (CARD_SIZE, CARD_SIZE), 255)
        circle = Image.new("L", (rad * 2, rad * 2), 0)
        ImageDraw.Draw(circle).ellipse((0, 0, rad * 2, rad * 2), fill=255)
        mask.paste(circle.crop((0, 0, rad, rad)), (0, 0))
        mask.paste(circle.crop((rad, 0, rad * 2, rad)), (CARD_SIZE - rad, 0))
        mask.paste(circle.crop((0, rad, rad, rad * 2)), (0, CARD_SIZE - rad))
        mask.paste(circle.crop((rad, rad, rad * 2, rad * 2)), (CARD_SIZE - rad, CARD_SIZE - rad))
        _CORNER_MASK = mask
    return _CORNER_MASK


def _fit_cover(img: Image.Image, size: int) -> Image.Image:
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.BILINEAR)


def _contrasting_color(img: Image.Image) -> tuple[int, int, int]:
    """Blanc ou noir selon la luminosité moyenne de la zone texte (bas de l'image)."""
    sample = img.crop((0, int(CARD_SIZE * 0.62), CARD_SIZE, CARD_SIZE)).resize((8, 8), Image.Resampling.BOX)
    pixels = list(sample.getdata())
    r = sum(p[0] for p in pixels) / len(pixels)
    g = sum(p[1] for p in pixels) / len(pixels)
    b = sum(p[2] for p in pixels) / len(pixels)
    luminosity = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return (255, 255, 255) if luminosity < 140 else (0, 0, 0)


def _fit_font(text: str, max_width: int, base: ImageFont.FreeTypeFont, path: str, start: int, min_size: int = 18) -> ImageFont.FreeTypeFont:
    size = start
    font = base
    while size > min_size and font.getlength(text) > max_width:
        size -= 2
        font = ImageFont.truetype(path, size)
    return font


def _render_card_sync(avatar_raw: bytes, username: str, font_path: str) -> Image.Image:
    card = _fit_cover(Image.open(BytesIO(avatar_raw)).convert("RGB"), CARD_SIZE)
    text_color = _contrasting_color(card)
    stroke = (0, 0, 0) if text_color == (255, 255, 255) else (255, 255, 255)

    name = pretty.shorten_text(username, 18)
    font = _fit_font(name, CARD_SIZE - 24, ImageFont.truetype(font_path, 54), font_path, 54)

    draw = ImageDraw.Draw(card)
    draw.text(
        (CARD_SIZE / 2, CARD_SIZE - 28),
        name,
        font=font,
        fill=text_color,
        anchor="md",
        stroke_width=2,
        stroke_fill=stroke,
    )

    card = card.convert("RGBA")
    card.putalpha(_corner_mask())
    return card


class Profil(commands.Cog):
    """Génère une carte de profil carrée arrondie."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._font_path = str(dataio.COMMON_RESOURCES_PATH / "fonts" / "NotoBebasNeue.ttf")

    async def build_card(self, member: discord.Member | discord.User) -> bytes:
        raw = await member.display_avatar.with_size(256).read()
        image = await asyncio.to_thread(_render_card_sync, raw, member.name, self._font_path)
        buf = BytesIO()
        image.save(buf, format="PNG", compress_level=1)
        return buf.getvalue()

    @app_commands.command(name="pdp")
    @app_commands.rename(user="utilisateur")
    @app_commands.describe(user="Membre dont générer la carte (par défaut, vous)")
    @app_commands.checks.cooldown(1, 5)
    async def pdp(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        """Génère une image carrée arrondie avec la photo et le pseudo d'un membre."""
        target = user or interaction.user
        await interaction.response.defer()

        try:
            png = await self.build_card(target)
        except Exception as e:
            logger.exception(e)
            return await interaction.followup.send(
                "**Erreur ·** Impossible de générer la carte de profil.", ephemeral=True
            )

        await interaction.followup.send(
            file=discord.File(BytesIO(png), filename="profil.png", description=f"Carte de profil de {target.name}")
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profil(bot))
