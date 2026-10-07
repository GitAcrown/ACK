import asyncio
import logging
import re
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from utils import dataio, pretty

logger = logging.getLogger("WANDER.Profil")

CARD_SIZE = 256
CORNER_RADIUS = 36
MAX_MEMBERS = 10

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


def _fit_font(text: str, max_width: int, path: str, start: int, min_size: int = 18) -> ImageFont.FreeTypeFont:
    size = start
    font = ImageFont.truetype(path, size)
    while size > min_size and font.getlength(text) > max_width:
        size -= 2
        font = ImageFont.truetype(path, size)
    return font


def _render_card_sync(avatar_raw: bytes, username: str, font_path: str) -> bytes:
    card = _fit_cover(Image.open(BytesIO(avatar_raw)).convert("RGB"), CARD_SIZE)
    text_color = _contrasting_color(card)
    stroke = (0, 0, 0) if text_color == (255, 255, 255) else (255, 255, 255)

    name = pretty.shorten_text(username, 18)
    font = _fit_font(name, CARD_SIZE - 24, font_path, 54)

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
    buf = BytesIO()
    card.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _safe_filename(name: str, user_id: int) -> str:
    safe = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_") or "user"
    return f"pdp_{safe[:24]}_{user_id}.png"


class MemberSelectView(discord.ui.View):
    def __init__(self, cog: "Profil", owner: discord.abc.User, *, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner = owner
        self._message: discord.Message | None = None

        select = discord.ui.UserSelect(
            placeholder="Sélectionnez un ou plusieurs membres…",
            min_values=1,
            max_values=MAX_MEMBERS,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                "Seul l'auteur de la commande peut utiliser ce menu.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self._message:
            try:
                await self._message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _on_select(self, interaction: discord.Interaction) -> None:
        select: discord.ui.UserSelect = self.children[0]  # type: ignore[assignment]
        users = list(select.values)
        self.stop()

        await interaction.response.edit_message(content="Génération en cours…", view=None)
        await self.cog.send_cards(interaction, users, edit=True)


class Profil(commands.Cog):
    """Génère une carte de profil carrée arrondie."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._font_path = str(dataio.COMMON_RESOURCES_PATH / "fonts" / "NotoBebasNeue.ttf")

    async def build_card(self, member: discord.Member | discord.User) -> bytes:
        raw = await member.display_avatar.with_size(256).read()
        return await asyncio.to_thread(_render_card_sync, raw, member.name, self._font_path)

    async def send_cards(
        self,
        interaction: discord.Interaction,
        members: list[discord.Member | discord.User],
        *,
        edit: bool = False,
    ) -> None:
        # Dédupliquer en conservant l'ordre
        seen: set[int] = set()
        unique: list[discord.Member | discord.User] = []
        for m in members:
            if m.id not in seen:
                seen.add(m.id)
                unique.append(m)
        members = unique[:MAX_MEMBERS]

        try:
            pngs = await asyncio.gather(*(self.build_card(m) for m in members))
        except Exception as e:
            logger.exception(e)
            msg = "**Erreur ·** Impossible de générer la/les carte(s) de profil."
            if edit:
                await interaction.edit_original_response(content=msg, view=None, attachments=[])
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return

        files = [
            discord.File(BytesIO(png), filename=_safe_filename(m.name, m.id), description=f"PDP de {m.name}")
            for m, png in zip(members, pngs)
        ]

        if edit:
            await interaction.edit_original_response(content="", view=None, attachments=files)
        else:
            await interaction.followup.send(files=files)

    @app_commands.command(name="pdp")
    @app_commands.rename(user="utilisateur")
    @app_commands.describe(user="Raccourci : générer directement la carte de ce membre")
    @app_commands.checks.cooldown(1, 8)
    async def pdp(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        """Génère une carte PDP. Sans membre : menu de sélection multiple."""
        if user is not None:
            await interaction.response.defer()
            return await self.send_cards(interaction, [user])

        view = MemberSelectView(self, interaction.user)
        await interaction.response.send_message(
            f"Sélectionnez jusqu'à **{MAX_MEMBERS}** membres pour générer leurs PDP.",
            view=view,
            ephemeral=True,
        )
        view._message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profil(bot))
