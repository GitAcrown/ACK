import asyncio
import functools
import logging
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageChops, ImageDraw, ImageFont

from utils import dataio, pretty

logger = logging.getLogger("WANDER.Profil")

CARD_SIZE = 512
CORNER_RADIUS = 64


def _round_corners(img: Image.Image, rad: int) -> Image.Image:
    circle = Image.new("L", (rad * 2, rad * 2), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, rad * 2, rad * 2), fill=255)
    w, h = img.size
    alpha = img.split()[3] if img.mode == "RGBA" else None
    mask = Image.new("L", img.size, 255)
    mask.paste(circle.crop((0, 0, rad, rad)), (0, 0))
    mask.paste(circle.crop((rad, 0, rad * 2, rad)), (w - rad, 0))
    mask.paste(circle.crop((0, rad, rad, rad * 2)), (0, h - rad))
    mask.paste(circle.crop((rad, rad, rad * 2, rad * 2)), (w - rad, h - rad))
    if alpha:
        img.putalpha(ImageChops.multiply(alpha, mask))
    else:
        img.putalpha(mask)
    return img


def _fit_cover(img: Image.Image, size: int) -> Image.Image:
    """Recadre l'image au centre pour remplir un carré `size`×`size`."""
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.LANCZOS)


def _add_bottom_scrim(img: Image.Image, height_ratio: float = 0.38) -> Image.Image:
    """Assombrit le bas de l'image pour rendre le texte lisible."""
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    start = int(h * (1 - height_ratio))
    for y in range(start, h):
        t = (y - start) / max(1, h - start)
        alpha = int(200 * (t**1.35))
        draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay)


def _render_card_sync(
    avatar_raw: bytes,
    display_name: str,
    username: str,
    name_font: ImageFont.FreeTypeFont,
    user_font: ImageFont.FreeTypeFont,
) -> Image.Image:
    size = CARD_SIZE
    card = _fit_cover(Image.open(BytesIO(avatar_raw)).convert("RGBA"), size)
    card = _add_bottom_scrim(card)

    draw = ImageDraw.Draw(card)
    name = pretty.shorten_text(display_name, 22)
    draw.text((size / 2, size - 78), name, font=name_font, fill=(255, 255, 255, 255), anchor="md")

    handle = pretty.shorten_text(f"@{username}", 28)
    draw.text((size / 2, size - 40), handle, font=user_font, fill=(220, 224, 230, 230), anchor="md")

    return _round_corners(card, CORNER_RADIUS)


class Profil(commands.Cog):
    """Génère une carte de profil carrée arrondie."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.__fonts: dict[str, ImageFont.FreeTypeFont] = {}

    def _font(self, name: str, size: int) -> ImageFont.FreeTypeFont:
        key = f"{name}_{size}"
        if key not in self.__fonts:
            path = dataio.COMMON_RESOURCES_PATH / "fonts" / f"{name}.ttf"
            self.__fonts[key] = ImageFont.truetype(str(path), size)
        return self.__fonts[key]

    async def build_card(self, member: discord.Member | discord.User) -> Image.Image:
        raw = await member.display_avatar.with_size(512).read()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(
                _render_card_sync,
                raw,
                member.display_name,
                member.name,
                self._font("gg_sans_semi", 38),
                self._font("gg_sans", 22),
            ),
        )

    @app_commands.command(name="profil")
    @app_commands.rename(user="utilisateur")
    @app_commands.describe(user="Membre dont générer la carte (par défaut, vous)")
    @app_commands.checks.cooldown(1, 10)
    async def profil(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        """Génère une image carrée arrondie avec la photo et le nom d'un membre."""
        target = user or interaction.user
        await interaction.response.defer()

        try:
            image = await self.build_card(target)
        except Exception as e:
            logger.exception(e)
            return await interaction.followup.send(
                "**Erreur ·** Impossible de générer la carte de profil.", ephemeral=True
            )

        buf = BytesIO()
        image.save(buf, format="PNG")
        buf.seek(0)
        await interaction.followup.send(
            file=discord.File(buf, filename="profil.png", description=f"Carte de profil de {target.display_name}")
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profil(bot))
