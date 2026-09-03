"""Cog Critiques — carnet de notes type Senscritique / Letterboxd, par serveur."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .dyn import (
    FicheDynButton,
    FicheRecord,
    bind_record,
    create_record,
    get_record,
    hit_from_dict,
    hit_to_dict,
    is_live,
    mark_stripped,
    sweep_expired,
    update_payload,
)
from .emojis import BOOK, EXPLICIT, GAME, MUSIC, RIVAL, SALE, STAR, STAR_EMPTY, STAR_HALF, TWIN, TV, XP
from .progress import (
    Affinity,
    MIN_AFFINITY_OVERLAP,
    XpAward,
    agreement_percent,
    apply_daily_limits,
    compute_review_xp,
    level_for_xp,
    level_progress,
    title_for_level,
)
from .providers import MediaCatalog, MediaHit, parse_search_query
from utils import dataio, fuzzy, pretty

logger = logging.getLogger("ACK.Reviews")

NO_PINGS = discord.AllowedMentions.none()


async def apply_view(interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
    """Met à jour le message comme MARIA : `edit_message`, sans allowed_mentions."""
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)


class ReviewsLayout(discord.ui.LayoutView):
    """LayoutView ACK : un Container (texte + ActionRows), comme MARIA."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("Vue %s / %s : %s", type(self).__name__, type(item).__name__, error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "**Erreur ·** Le bouton a planté. Réessaie après un `&reload reviews`.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass

    def set_layout(self, body: list[discord.ui.Item], *rows: discord.ui.ActionRow | None) -> None:
        self.clear_items()
        children = list(body)
        for row in rows:
            if row is not None:
                children.append(row)
        if children:
            self.add_item(discord.ui.Container(*children))

VALID_RATINGS = tuple(i / 2 for i in range(11))
DEFAULT_COMMENT_MAX = 280
MIN_COMMENT_MAX = 50
MAX_COMMENT_MAX = 500
JOURNAL_PAGE = 4
REVIEWS_PAGE = 5
CATALOG_PAGE = 8

TYPE_META: dict[str, tuple[str, str]] = {
    "movie": (TV, "Film"),
    "tv": (TV, "Série"),
    "game": (GAME, "Jeu"),
    "album": (MUSIC, "Album"),
    "track": (MUSIC, "Morceau"),
    "book": (BOOK, "Livre"),
}

TYPE_CHOICES = [
    app_commands.Choice(name="Tous les types", value="all"),
    app_commands.Choice(name="Film", value="movie"),
    app_commands.Choice(name="Série", value="tv"),
    app_commands.Choice(name="Jeu", value="game"),
    app_commands.Choice(name="Album", value="album"),
    app_commands.Choice(name="Morceau", value="track"),
    app_commands.Choice(name="Livre", value="book"),
]

PERIOD_SECONDS = {"semaine": 7 * 86400, "mois": 30 * 86400}

FAVORITE_LABELS = {
    1: "Fétiche",
    2: "Coup de cœur",
    3: "Pépite",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_stars(rating: float) -> str:
    """Rangée de 5 étoiles custom, pour les fiches et messages."""
    full = int(rating)
    half = (rating - full) >= 0.45
    empty = 5 - full - (1 if half else 0)
    return STAR * full + (STAR_HALF if half else "") + STAR_EMPTY * empty


def format_stars_compact(rating: float) -> str:
    """Une seule étoile custom + note, pour boutons et texte rich."""
    if rating <= 0:
        icon = STAR_EMPTY
    elif rating % 1 >= 0.45:
        icon = STAR_HALF
    else:
        icon = STAR
    return f"{icon} {rating:g}"


def format_stars_select(rating: float) -> str:
    """Étoiles unicode : un Select n'affiche pas les customs dans label/description."""
    if rating <= 0:
        icon = "☆"
    elif rating % 1 >= 0.45:
        icon = "★☆"
    else:
        icon = "★"
    return f"{icon} {rating:g}"


RATING_CHOICES = [
    app_commands.Choice(name=f"{format_stars_select(r)}/5", value=r)
    for r in VALID_RATINGS
]

_MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)
_DATE_DMY = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$")
_DATE_YMD = re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$")


def experienced_verb(media_type: str) -> str:
    return {
        "movie": "Vu",
        "tv": "Vu",
        "game": "Joué",
        "album": "Écouté",
        "track": "Écouté",
        "book": "Lu",
    }.get(media_type, "Vu")


def format_experienced_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        year, month, day = (int(part) for part in raw.split("-"))
        return f"{day} {_MONTHS_FR[month - 1]} {year}"
    except (ValueError, IndexError):
        return raw


def experienced_line(media_type: str, raw: str | None) -> str:
    if not raw:
        return ""
    return f"-# {experienced_verb(media_type)} le {format_experienced_date(raw)}"


def experienced_from_row(row: Any) -> str:
    if row is None:
        return ""
    if isinstance(row, dict):
        return str(row.get("experienced_at") or "")
    try:
        return str(row["experienced_at"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def parse_experienced_date(raw: str) -> tuple[str | None, str | None]:
    text = (raw or "").strip()
    if not text:
        return "", None
    match = _DATE_DMY.match(text) or _DATE_YMD.match(text)
    if match is None:
        return None, "Indique une date du type `12/03/2024` ou `2024-03-12`."
    if match.re is _DATE_YMD:
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
    else:
        day, month, year = (int(match.group(i)) for i in (1, 2, 3))
    try:
        date(year, month, day)
    except ValueError:
        return None, "Cette date n'existe pas."
    return f"{year:04d}-{month:02d}-{day:02d}", None


def experienced_to_input(raw: str) -> str:
    if not raw:
        return ""
    parts = raw.split("-")
    if len(parts) != 3:
        return raw
    try:
        return f"{int(parts[2]):02d}/{parts[1]}/{parts[0]}"
    except ValueError:
        return raw


def parse_rating(raw: str) -> float | None:
    cleaned = raw.strip().replace(",", ".").replace("/5", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    snapped = round(value * 2) / 2
    if 0 <= snapped <= 5:
        return snapped
    return None


def type_label(media_type: str) -> str:
    return TYPE_META.get(media_type, ("", "Média"))[1]


def type_emoji(media_type: str) -> str:
    return TYPE_META.get(media_type, ("", "Média"))[0]


def select_emoji(media_type: str) -> discord.PartialEmoji | None:
    raw = type_emoji(media_type)
    if not raw:
        return None
    return discord.PartialEmoji.from_str(raw)


def select_hit_description(hit: MediaHit) -> str:
    year = f"{hit.year}" if hit.year else "—"
    parts = [type_label(hit.media_type), year]
    by = hit.subtitle or hit.extra.get("director") or ""
    if not by:
        created = hit.extra.get("created_by") or []
        by = created[0] if created else ""
    if by:
        parts.append(by)
    return pretty.shorten_text(" · ".join(parts), 95)


def section_with_thumbnail(text: str, url: str | None) -> discord.ui.Item:
    body = discord.ui.TextDisplay(text)
    if not url:
        return body
    try:
        return discord.ui.Section(body, accessory=discord.ui.Thumbnail(url))
    except Exception:
        return body


def hit_from_row(row: Any) -> MediaHit:
    extra: dict[str, Any] = {}
    try:
        extra = json.loads(row["extra_json"] or "{}")
    except json.JSONDecodeError:
        extra = {}
    genres = [part for part in (row["genres"] or "").split("|") if part]
    year = row["year"]
    return MediaHit(
        source=row["source"],
        source_id=row["source_id"],
        media_type=row["media_type"],
        title=row["title"],
        subtitle=row["subtitle"] or "",
        year=int(year) if year else None,
        poster_url=row["poster_url"] or None,
        url=row["url"] or "",
        overview=row["overview"] or "",
        genres=genres,
        extra=extra,
    )


def _user_display(guild: discord.Guild, bot: commands.Bot, user_id: int) -> tuple[str, str | None]:
    member = guild.get_member(user_id)
    if member:
        return member.display_name, member.display_avatar.url
    user = bot.get_user(user_id)
    if user:
        return user.display_name, user.display_avatar.url
    return f"Utilisateur {user_id}", None


def _mention(guild: discord.Guild, bot: commands.Bot, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member:
        return member.mention
    name, _avatar = _user_display(guild, bot, user_id)
    return f"**{name}**"


def _titled(mention: str, title: str) -> str:
    return f"{mention}\n-# {title}"


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _official_line(hit: MediaHit) -> str:
    if hit.source == "tmdb":
        rating = float(hit.extra.get("vote_average") or 0)
        count = int(hit.extra.get("vote_count") or 0)
        if rating:
            stars = format_stars(rating / 2)
            votes = f"  ·  {_fmt_int(count)} votes" if count else ""
            return f"{stars}  **{rating:.1f}/10**{votes}"
    if hit.source == "steam":
        label = hit.extra.get("review_label") or ""
        emoji = hit.extra.get("review_emoji") or ""
        if label:
            return f"{emoji} {label}".strip()
    popularity = hit.extra.get("popularity")
    if hit.media_type == "track" and popularity:
        return f"Popularité Spotify **{popularity}/100**"
    return ""


def _price_line(hit: MediaHit) -> str:
    if hit.media_type != "game":
        return ""
    if hit.extra.get("is_free"):
        return "**Gratuit**"
    final = hit.extra.get("price_final")
    if not isinstance(final, int):
        return ""
    text = f"**{final / 100:.2f} €**"
    discount = hit.extra.get("discount") or 0
    initial = hit.extra.get("price_initial")
    if discount and isinstance(initial, int):
        text += f"  ~~{initial / 100:.2f} €~~  {SALE} **-{discount}%**"
    return text


def _runtime_label(minutes: int) -> str:
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h{mins:02d}" if hours else f"{mins} min"


def _title_line(hit: MediaHit) -> str:
    line = f"## {type_emoji(hit.media_type)} {hit.title}"
    if hit.year:
        line += f"  ·  {hit.year}"
    return line


def _meta_line(hit: MediaHit) -> str:
    parts = [type_label(hit.media_type)]
    if hit.subtitle:
        parts.append(hit.subtitle)
    parts.extend(hit.genres[:3])
    return "  ·  ".join(parts)


def _footer_line(hit: MediaHit) -> str:
    parts: list[str] = []
    extra = hit.extra
    if extra.get("runtime"):
        parts.append(_runtime_label(extra["runtime"]))
    if extra.get("seasons"):
        seasons = extra["seasons"]
        parts.append(f"{seasons} saison{'s' if seasons > 1 else ''}")
    if extra.get("director"):
        parts.append(extra["director"])
    created = extra.get("created_by") or []
    if created and not extra.get("director"):
        parts.append(", ".join(created[:2]))
    cast = extra.get("cast") or []
    if cast:
        parts.append(", ".join(cast))
    if extra.get("album"):
        parts.append(extra["album"])
    if extra.get("duration"):
        parts.append(extra["duration"])
    if extra.get("explicit"):
        parts.append(EXPLICIT)
    if extra.get("total_tracks"):
        tracks = extra["total_tracks"]
        parts.append(f"{tracks} piste{'s' if tracks > 1 else ''}")
    lang = extra.get("original_language") or ""
    if lang and lang != "fr":
        parts.append(lang.upper())
    source_names = {"tmdb": "TMDB", "steam": "Steam", "spotify": "Spotify", "openlibrary": "Open Library"}
    if hit.url:
        parts.append(f"[{source_names.get(hit.source, hit.source)}]({hit.url})")
    elif hit.source:
        parts.append(source_names.get(hit.source, hit.source))
    return "  ·  ".join(parts)


def append_fiche_sections(
    body: list[discord.ui.Item],
    hit: MediaHit,
    *,
    avg: float | None,
    count: int,
    my_review: dict | None,
    social_line: str = "",
) -> None:
    head: list[str] = []
    official = _official_line(hit)
    if official:
        head.append(official)
    if count:
        stars = format_stars(avg or 0)
        head.append(
            f"**Serveur** · {stars}  **{(avg or 0):.1f}/5**  ·  "
            f"{count} critique{'s' if count > 1 else ''}"
        )
    else:
        head.append("*Aucune note sur ce serveur pour l'instant.*")
    if my_review:
        comment = pretty.shorten_text(my_review["comment"], 180) if my_review["comment"] else ""
        mine = f"Ta note · {format_stars(my_review['rating'])}  **{my_review['rating']:g}/5**"
        if comment:
            mine += f"\n*{comment}*"
        seen = experienced_line(hit.media_type, experienced_from_row(my_review))
        if seen:
            mine += f"\n{seen}"
        head.append(mine)
    price = _price_line(hit)
    if price:
        head.append(price)

    tail: list[str] = []
    if social_line:
        tail.append(f"-# {social_line}")
    overview = pretty.shorten_text(hit.overview, 380) if hit.overview else ""
    if overview:
        tail.append(overview)
    elif not official and not count:
        tail.append("-# Aucune description disponible.")

    body.append(section_with_thumbnail("\n".join(head), hit.poster_url))
    if tail:
        body.append(discord.ui.Separator())
        body.append(discord.ui.TextDisplay("\n".join(tail)))


def fiche_intro(hit: MediaHit) -> list[discord.ui.Item]:
    items: list[discord.ui.Item] = [
        discord.ui.TextDisplay(f"{_title_line(hit)}\n-# {_meta_line(hit)}"),
        discord.ui.Separator(),
    ]
    backdrop = hit.extra.get("backdrop_url")
    if backdrop:
        try:
            items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(backdrop)))
        except Exception:
            pass
    return items


def _link_label(hit: MediaHit) -> str:
    return {
        "tmdb": "TMDB",
        "steam": "Steam",
        "spotify": "Spotify",
        "openlibrary": "Open Library",
    }.get(hit.source, "Fiche")


def render_published_fiche(
    hit: MediaHit,
    *,
    avg: float | None,
    count: int,
    social: str,
    wid: str,
    live: bool,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    body: list[discord.ui.Item] = []
    body.extend(fiche_intro(hit))
    append_fiche_sections(body, hit, avg=avg, count=count, my_review=None, social_line=social)
    footer = _footer_line(hit)
    if footer:
        body.append(discord.ui.Separator())
        body.append(discord.ui.TextDisplay(f"-# {footer}"))
    if live:
        body.append(discord.ui.ActionRow(
            FicheDynButton(wid, "critiques", label=f"Critiques ({count})"),
            FicheDynButton(wid, "noter", label="Ma note", style=discord.ButtonStyle.green),
        ))
    view.add_item(discord.ui.Container(*body))
    return view


def render_published_record(rec: FicheRecord, *, live: bool) -> discord.ui.LayoutView | None:
    raw = rec.payload.get("hit")
    if not isinstance(raw, dict):
        return None
    return render_published_fiche(
        hit_from_dict(raw),
        avg=rec.payload.get("avg"),
        count=int(rec.payload.get("count") or 0),
        social=str(rec.payload.get("social") or ""),
        wid=rec.id,
        live=live,
    )


async def send_published_fiche(
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    interaction: discord.Interaction,
    *,
    followup: bool = True,
) -> None:
    if cog.catalog is not None:
        try:
            hit = await cog.catalog.enrich(hit)
        except Exception:
            logger.exception("Enrichissement de fiche publiée impossible")
    media_id = await cog.lookup_media_id(guild, hit)
    avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
    reviews = await cog.list_reviews(guild, media_id) if media_id else []
    social = cog.social_line_for_reviews(guild, reviews, viewer_id=None)
    wid = create_record({
        "guild_id": guild.id,
        "hit": hit_to_dict(hit),
        "avg": avg,
        "count": count,
        "social": social,
    })
    view = render_published_fiche(hit, avg=avg, count=count, social=social, wid=wid, live=True)
    if followup:
        message = await interaction.followup.send(view=view)
    else:
        await interaction.edit_original_response(view=view)
        message = await interaction.original_response()
    bind_record(wid, message.channel.id, message.id)


async def sync_published_fiche(cog: "Reviews", guild: discord.Guild, wid: str, hit: MediaHit) -> None:
    rec = get_record(wid)
    if not is_live(rec) or rec is None:
        return
    media_id = await cog.lookup_media_id(guild, hit)
    avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
    reviews = await cog.list_reviews(guild, media_id) if media_id else []
    social = cog.social_line_for_reviews(guild, reviews, viewer_id=None)
    rec.payload.update({"hit": hit_to_dict(hit), "avg": avg, "count": count, "social": social})
    update_payload(wid, rec.payload)
    view = render_published_fiche(hit, avg=avg, count=count, social=social, wid=wid, live=True)
    if not rec.channel_id or not rec.message_id:
        return
    try:
        channel = cog.bot.get_channel(rec.channel_id) or await cog.bot.fetch_channel(rec.channel_id)
        message = await channel.fetch_message(rec.message_id)
        await message.edit(view=view)
    except Exception as exc:
        logger.info("Maj fiche publiée %s : %s", wid, exc)


async def send_ephemeral_menu(interaction: discord.Interaction, view: ReviewsLayout) -> None:
    """Nouveau message éphémère — ne jamais éditer la fiche publique."""
    await interaction.response.send_message(view=view, ephemeral=True)
    view._interaction = interaction


async def handle_published_fiche_click(
    interaction: discord.Interaction,
    wid: str,
    action: str,
) -> None:
    rec = get_record(wid)
    if not is_live(rec):
        if rec is not None:
            view = render_published_record(rec, live=False)
            if view is not None:
                try:
                    await interaction.response.edit_message(view=view)
                except discord.HTTPException:
                    pass
            mark_stripped(rec.id)
        if not interaction.response.is_done():
            await interaction.response.send_message("Les boutons ont expiré.", ephemeral=True)
        return
    cog = interaction.client.get_cog("Reviews")
    guild = interaction.guild
    raw = rec.payload.get("hit") if rec else None
    if cog is None or not isinstance(guild, discord.Guild) or not isinstance(raw, dict):
        await interaction.response.send_message(
            "**Erreur ·** Impossible d'ouvrir ce menu.",
            ephemeral=True,
        )
        return
    hit = hit_from_dict(raw)
    if action == "noter":
        menu = await MyNoteView.create(cog, guild, hit, interaction.user.id, published_wid=wid)
    elif action == "critiques":
        menu = await PublicCritiquesView.create(cog, guild, hit, interaction.user.id)
    else:
        menu = await PublicFichePeekView.create(cog, guild, hit)
    await send_ephemeral_menu(interaction, menu)


async def open_public_fiche(
    cog: "Reviews",
    guild: discord.Guild,
    interaction: discord.Interaction,
    hit: MediaHit,
) -> None:
    await send_published_fiche(cog, guild, hit, interaction, followup=True)


# ---------------------------------------------------------------------------
# Annonce publique (présentation seule)
# ---------------------------------------------------------------------------

def build_announce_view(
    hit: MediaHit,
    *,
    mention: str,
    title: str,
    avatar_url: str | None,
    rating: float,
    comment: str,
    updated: bool,
    experienced_at: str = "",
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()
    verb = "a mis à jour sa note" if updated else "a noté"
    header = f"{_titled(mention, title)}\n{verb}\n{_title_line(hit)}\n{format_stars(rating)}  **{rating:g}/5**"
    if comment:
        header += f"\n*{pretty.shorten_text(comment, 240)}*"
    seen = experienced_line(hit.media_type, experienced_at)
    if seen:
        header += f"\n{seen}"
    container.add_item(section_with_thumbnail(header, avatar_url or hit.poster_url))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(f"-# {_meta_line(hit)}" + (f"  ·  [{_link_label(hit)}]({hit.url})" if hit.url else "")))
    view.add_item(container)
    return view


# ---------------------------------------------------------------------------
# Modal de notation
# ---------------------------------------------------------------------------

def _review_saved_lines(hit: MediaHit, rating: float, created: bool, award: XpAward) -> list[str]:
    verb = "enregistrée" if created else "mise à jour"
    parts = [f"**Critique {verb} ·** {format_stars(rating)}  **{rating:g}/5** — {hit.title}."]
    if award.gained:
        parts.append(f"{XP} +{award.gained} · niveau {award.level}")
        if award.capped:
            parts.append("(plafond quotidien atteint)")
    elif award.capped:
        parts.append("Plafond d'XP quotidien atteint.")
    if award.leveled_up:
        new_title = title_for_level(award.level)
        old_title = title_for_level(award.previous_level)
        parts.append(f"Nouveau titre · {new_title}" if new_title != old_title else f"Niveau {award.level}")
    return parts


class RateModal(discord.ui.Modal, title="Noter cette œuvre"):
    def __init__(
        self,
        parent: Any,
        *,
        max_comment: int,
        default_rating: float | None,
        default_comment: str,
        default_experienced: str = "",
    ):
        super().__init__()
        self._hub = parent
        self.rating_input = discord.ui.TextInput(
            label="Note (0 à 5, demies autorisées)",
            placeholder="Ex. 4.5",
            default="" if default_rating is None else f"{default_rating:g}",
            max_length=4,
            required=True,
        )
        self.comment_input = discord.ui.TextInput(
            label="Commentaire (optionnel)",
            style=discord.TextStyle.paragraph,
            placeholder="Un avis court…",
            default=default_comment[:max_comment] if default_comment else None,
            max_length=max_comment,
            required=False,
        )
        self.date_input = discord.ui.TextInput(
            label="Date (optionnel)",
            placeholder="Ex. 12/03/2024 — vu, joué, écouté ou lu",
            default=experienced_to_input(default_experienced) or None,
            max_length=12,
            required=False,
        )
        self.add_item(self.rating_input)
        self.add_item(self.comment_input)
        self.add_item(self.date_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rating = parse_rating(self.rating_input.value)
        if rating is None:
            await interaction.response.send_message(
                "**Erreur ·** La note doit être comprise entre 0 et 5 (demies étoiles acceptées, ex. `3.5`).",
                ephemeral=True,
            )
            return
        experienced_at, date_error = parse_experienced_date(str(self.date_input.value or ""))
        if date_error:
            await interaction.response.send_message(f"**Erreur ·** {date_error}", ephemeral=True)
            return
        from_public = bool(self._hub.from_published_modal)
        orphan = from_public or (self._hub._interaction is None and self._hub._message is None)
        await interaction.response.defer(ephemeral=orphan or from_public)
        if orphan and not from_public:
            self._hub._interaction = interaction
        await self._hub.save_review(
            interaction,
            rating,
            str(self.comment_input.value or "").strip(),
            experienced_at or "",
        )


class MyNoteEditButton(discord.ui.Button):
    def __init__(self, parent: "MyNoteView"):
        super().__init__(
            label="Modifier" if parent.my_review else "Ajouter une note",
            style=discord.ButtonStyle.secondary if parent.my_review else discord.ButtonStyle.green,
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        existing = self._hub.my_review or {}
        await interaction.response.send_modal(
            RateModal(
                self._hub,
                max_comment=await self._hub.cog.get_comment_max(self._hub.guild),
                default_rating=existing.get("rating"),
                default_comment=existing.get("comment") or "",
                default_experienced=existing.get("experienced_at") or "",
            )
        )


class MyNoteDeleteButton(discord.ui.Button):
    def __init__(self, parent: "MyNoteView"):
        super().__init__(label="Supprimer", style=discord.ButtonStyle.red)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.delete_review(interaction)


class MyNoteView(ReviewsLayout):
    """Menu éphémère : uniquement la note du clicqueur."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        *,
        author_id: int,
        my_review: dict | None,
        published_wid: str | None,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.hit = hit
        self.author_id = author_id
        self.my_review = my_review
        self.published_wid = published_wid
        self.from_published_modal = False
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self._build()

    @classmethod
    async def create(
        cls,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        author_id: int,
        *,
        published_wid: str | None,
    ) -> "MyNoteView":
        media_id = await cog.lookup_media_id(guild, hit)
        mine = await cog.get_review(guild, author_id, media_id) if media_id else None
        return cls(cog, guild, hit, author_id=author_id, my_review=mine, published_wid=published_wid)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne concerne que ta note.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _build(self) -> None:
        hit = self.hit
        mine = self.my_review
        if mine:
            text = (
                f"{_title_line(hit)}\n"
                f"{format_stars(mine['rating'])}  **{mine['rating']:g}/5**\n"
            )
            if mine.get("comment"):
                text += f"*{pretty.shorten_text(mine['comment'], 240)}*\n"
            seen = experienced_line(hit.media_type, mine.get("experienced_at"))
            if seen:
                text += f"{seen}\n"
            text += f"-# Ta note · {_meta_line(hit)}"
        else:
            text = (
                f"{_title_line(hit)}\n"
                f"*Tu n'as pas encore noté cette œuvre.*\n"
                f"-# {_meta_line(hit)}"
            )
        actions: list[discord.ui.Item] = [MyNoteEditButton(self)]
        if mine:
            actions.append(MyNoteDeleteButton(self))
        self.set_layout([section_with_thumbnail(text, hit.poster_url)], discord.ui.ActionRow(*actions))

    async def save_review(
        self, interaction: discord.Interaction, rating: float, comment: str, experienced_at: str = "",
    ) -> None:
        created, award = await self.cog.upsert_review(
            self.guild, interaction.user, self.hit, rating, comment, experienced_at=experienced_at,
        )
        media_id = await self.cog.lookup_media_id(self.guild, self.hit)
        self.my_review = await self.cog.get_review(self.guild, self.author_id, media_id) if media_id else None
        self._build()
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        await apply_view(interaction, self)
        await interaction.followup.send("\n".join(_review_saved_lines(self.hit, rating, created, award)), ephemeral=True)
        await self.cog.announce_review(
            self.guild, interaction.user, self.hit, rating, comment,
            updated=not created, experienced_at=experienced_at,
        )

    async def delete_review(self, interaction: discord.Interaction) -> None:
        await self.cog.delete_review(self.guild, self.author_id, self.hit)
        self.my_review = None
        self._build()
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        await apply_view(interaction, self)
        await interaction.followup.send("**Critique supprimée ·** Ta note a été retirée.", ephemeral=True)


class PublicFichePeekView(ReviewsLayout):
    """Menu éphémère lecture seule — ouvert depuis le bouton Fiche public."""

    def __init__(self, hit: MediaHit, *, avg: float | None, count: int, social: str):
        super().__init__(timeout=300)
        self._interaction: discord.Interaction | None = None
        body: list[discord.ui.Item] = []
        body.extend(fiche_intro(hit))
        append_fiche_sections(body, hit, avg=avg, count=count, my_review=None, social_line=social)
        footer = _footer_line(hit)
        if footer:
            body.append(discord.ui.Separator())
            body.append(discord.ui.TextDisplay(f"-# {footer}"))
        self.set_layout(body)

    @classmethod
    async def create(cls, cog: "Reviews", guild: discord.Guild, hit: MediaHit) -> "PublicFichePeekView":
        media_id = await cog.lookup_media_id(guild, hit)
        avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
        reviews = await cog.list_reviews(guild, media_id) if media_id else []
        social = cog.social_line_for_reviews(guild, reviews, viewer_id=None)
        return cls(hit, avg=avg, count=count, social=social)


class PublicCritiquesPageButton(discord.ui.Button):
    def __init__(self, parent: "PublicCritiquesView", delta: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._hub = parent
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.page = max(0, self._hub.page + self._delta)
        self._hub._build()
        await apply_view(interaction, self._hub)


class PublicCritiquesView(ReviewsLayout):
    """Menu éphémère lecture seule — ouvert depuis le bouton Critiques public."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        *,
        author_id: int,
        reviews: list[Any],
        titles: dict[int, str],
        avg: float | None,
        count: int,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.hit = hit
        self.author_id = author_id
        self.reviews = reviews
        self.titles = titles
        self.avg = avg
        self.count = count
        self.page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    @classmethod
    async def create(
        cls,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        author_id: int,
    ) -> "PublicCritiquesView":
        media_id = await cog.lookup_media_id(guild, hit)
        avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
        reviews = await cog.list_reviews(guild, media_id) if media_id else []
        titles = await cog.get_titles(guild, [int(row["user_id"]) for row in reviews])
        return cls(cog, guild, hit, author_id=author_id, reviews=reviews, titles=titles, avg=avg, count=count)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _build(self) -> None:
        hit = self.hit
        body: list[discord.ui.Item] = list(fiche_intro(hit)[:2])
        rows: list[discord.ui.ActionRow] = []
        if not self.reviews:
            empty = "*Pas encore de critique sur ce serveur.*"
            if hit.url:
                empty += f"\n-# [{_link_label(hit)}]({hit.url})"
            body.append(discord.ui.TextDisplay(empty))
            self.set_layout(body)
            return
        max_page = max(0, (len(self.reviews) - 1) // REVIEWS_PAGE)
        self.page = min(self.page, max_page)
        start = self.page * REVIEWS_PAGE
        for row in self.reviews[start:start + REVIEWS_PAGE]:
            user_id = int(row["user_id"])
            _name, avatar = _user_display(self.guild, self.cog.bot, user_id)
            title = self.titles.get(user_id, title_for_level(1))
            text = (
                f"{_titled(_mention(self.guild, self.cog.bot, user_id), title)}\n"
                f"{format_stars(row['rating'])}  **{row['rating']:g}/5** · <t:{row['updated_at']}:R>"
            )
            if row["comment"]:
                text += f"\n{pretty.shorten_text(row['comment'], 220)}"
            seen = experienced_line(hit.media_type, experienced_from_row(row))
            if seen:
                text += f"\n{seen}"
            body.append(section_with_thumbnail(text, avatar))
        total_pages = max(1, (len(self.reviews) + REVIEWS_PAGE - 1) // REVIEWS_PAGE)
        page_note = (
            f"-# {self.count} critique(s) · moyenne {format_stars(self.avg or 0)} "
            f"{(self.avg or 0):.1f}/5 · page {self.page + 1}/{total_pages}"
        )
        if hit.url:
            page_note += f"  ·  [{_link_label(hit)}]({hit.url})"
        body.append(discord.ui.TextDisplay(page_note))
        if max_page > 0:
            prev_btn = PublicCritiquesPageButton(self, -1, "← Précédent")
            next_btn = PublicCritiquesPageButton(self, 1, "Suivant →")
            prev_btn.disabled = self.page <= 0
            next_btn.disabled = self.page >= max_page
            rows.append(discord.ui.ActionRow(prev_btn, next_btn))
        self.set_layout(body, *rows)


# ---------------------------------------------------------------------------
# Vue session (sélection + fiche + critiques)
# ---------------------------------------------------------------------------

class MediaSelect(discord.ui.Select):
    def __init__(self, parent: "MediaSessionView", hits: list[MediaHit], selected: int):
        options = []
        for index, hit in enumerate(hits[:25]):
            options.append(
                discord.SelectOption(
                    label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                    value=str(index),
                    description=select_hit_description(hit),
                    emoji=select_emoji(hit.media_type),
                    default=index == selected,
                )
            )
        super().__init__(placeholder="Choisir une œuvre", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._hub.selected = int(self.values[0])
        await self._hub.enrich_selected()
        await self._hub.refresh(interaction)


class TabButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView", tab: str, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary if parent.tab == tab else discord.ButtonStyle.secondary,
        )
        self._hub = parent
        self._tab = tab

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.tab = self._tab
        self._hub.review_page = 0
        self._hub._build()
        await apply_view(interaction, self._hub)


class RateButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        super().__init__(label="Noter", style=discord.ButtonStyle.green)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._hub
        media_id = await parent.cog.lookup_media_id(parent.guild, parent.hit)
        existing = await parent.cog.get_review(parent.guild, interaction.user.id, media_id) if media_id else None
        pending = parent.pending_rating
        if pending is not None and existing is None and interaction.user.id == parent.author_id:
            await interaction.response.defer()
            await parent.save_review(interaction, pending, parent.pending_comment)
            return
        max_comment = await parent.cog.get_comment_max(parent.guild)
        existing = existing or {}
        await interaction.response.send_modal(
            RateModal(
                parent,
                max_comment=max_comment,
                default_rating=existing.get("rating", parent.pending_rating if interaction.user.id == parent.author_id else None),
                default_comment=existing.get("comment") or (parent.pending_comment if interaction.user.id == parent.author_id else ""),
                default_experienced=existing.get("experienced_at") or "",
            )
        )


class DeleteReviewButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        super().__init__(label="Supprimer ma note", style=discord.ButtonStyle.red)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.cog.delete_review(self._hub.guild, interaction.user.id, self._hub.hit)
        self._hub.my_review = None
        await self._hub.reload_stats()
        if self._hub.published_wid:
            await sync_published_fiche(self._hub.cog, self._hub.guild, self._hub.published_wid, self._hub.hit)
        await self._hub.refresh(interaction)
        await interaction.followup.send("**Critique supprimée ·** Ta note a été retirée.", ephemeral=True)


class PublishFicheButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        super().__init__(label="Publier la fiche", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await send_published_fiche(
            self._hub.cog,
            self._hub.guild,
            self._hub.hit,
            interaction,
            followup=True,
        )


class ProfileShareButton(discord.ui.Button):
    def __init__(self, parent: "ProfileView"):
        super().__init__(label="Partager le profil", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        was_editable = self._hub.editable
        self._hub.editable = False
        body, _rows = self._hub._profil_layout()
        self._hub.editable = was_editable
        view = discord.ui.LayoutView(timeout=None)
        if body:
            view.add_item(discord.ui.Container(*body))
        try:
            await interaction.followup.send(view=view, allowed_mentions=NO_PINGS)
        except discord.HTTPException as exc:
            logger.warning("Impossible de partager le profil : %s", exc)
            await interaction.followup.send("**Erreur ·** Impossible de publier ce profil.", ephemeral=True)


class HubTabButton(discord.ui.Button):
    def __init__(self, parent: Any, tab: str, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary if parent.tab == tab else discord.ButtonStyle.secondary,
        )
        self._hub = parent
        self._tab = tab

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.tab = self._tab
        self._hub._build()
        await apply_view(interaction, self._hub)


class HubPageButton(discord.ui.Button):
    def __init__(self, parent: Any, attr: str, delta: int, label: str, max_page: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._hub = parent
        self._attr = attr
        self._delta = delta
        self._max_page = max_page

    async def callback(self, interaction: discord.Interaction) -> None:
        current = getattr(self._hub, self._attr)
        setattr(self._hub, self._attr, max(0, min(self._max_page, current + self._delta)))
        self._hub._build()
        await apply_view(interaction, self._hub)


class MediaSessionView(ReviewsLayout):
    """Recherche → fiche → critiques, dans une seule vue interactive."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hits: list[MediaHit],
        *,
        author_id: int,
        ephemeral: bool,
        pending_rating: float | None = None,
        pending_comment: str = "",
        selected: int = 0,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.hits = hits
        self.author_id = author_id
        self.ephemeral = ephemeral
        self.pending_rating = pending_rating
        self.pending_comment = pending_comment
        self.selected = selected
        self.tab = "fiche"
        self.review_page = 0
        self.avg: float | None = None
        self.count = 0
        self.my_review: dict | None = None
        self.reviews: list[Any] = []
        self.social_line = ""
        self.titles: dict[int, str] = {}
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self.published_wid: str | None = None
        self.from_published_modal = False

    @property
    def hit(self) -> MediaHit:
        return self.hits[self.selected]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.ephemeral and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul l'auteur de la commande peut utiliser ce menu.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    async def prepare(self) -> None:
        await self.enrich_selected()
        await self.reload_stats()
        self._build()

    async def enrich_selected(self) -> None:
        if self.cog.catalog is None:
            return
        try:
            self.hits[self.selected] = await self.cog.catalog.enrich(self.hit)
        except Exception:
            logger.exception("Enrichissement de fiche impossible")

    async def reload_stats(self) -> None:
        media_id = await self.cog.lookup_media_id(self.guild, self.hit)
        self.avg, self.count = await self.cog.media_stats(self.guild, media_id) if media_id else (None, 0)
        self.reviews = await self.cog.list_reviews(self.guild, media_id) if media_id else []
        self.my_review = None
        if media_id and self.ephemeral:
            self.my_review = await self.cog.get_review(self.guild, self.author_id, media_id)
        self.social_line = self.cog.social_line_for_reviews(
            self.guild,
            self.reviews,
            viewer_id=self.author_id if self.ephemeral else None,
        )
        self.titles = await self.cog.get_titles(self.guild, [int(row["user_id"]) for row in self.reviews])

    async def save_review(
        self, interaction: discord.Interaction, rating: float, comment: str, experienced_at: str = "",
    ) -> None:
        created, award = await self.cog.upsert_review(
            self.guild, interaction.user, self.hit, rating, comment, experienced_at=experienced_at,
        )
        await self.reload_stats()
        self.pending_rating = None
        self.tab = "fiche"
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        if not self.from_published_modal:
            await self.refresh(interaction)
        await interaction.followup.send("\n".join(_review_saved_lines(self.hit, rating, created, award)), ephemeral=True)
        await self.cog.announce_review(
            self.guild, interaction.user, self.hit, rating, comment,
            updated=not created, experienced_at=experienced_at,
        )

    def _build(self) -> None:
        hit = self.hit
        body: list[discord.ui.Item] = []
        rows: list[discord.ui.ActionRow] = []

        if len(self.hits) > 1:
            body.append(discord.ui.TextDisplay(f"### Résultats · {len(self.hits)} œuvre(s)"))
            if not any(item.source == "tmdb" for item in self.hits) and any(
                item.source == "spotify" for item in self.hits
            ):
                if self.cog.catalog is not None and not self.cog.catalog.tmdb.available:
                    body.append(discord.ui.TextDisplay("-# Films et séries absents · clé TMDB manquante."))
                else:
                    body.append(discord.ui.TextDisplay(
                        "-# Aucun film ou série trouvé — précise le type si besoin."
                    ))
            body.append(discord.ui.ActionRow(MediaSelect(self, self.hits, self.selected)))
            body.append(discord.ui.Separator())

        if self.tab == "fiche":
            body.extend(fiche_intro(hit))
            append_fiche_sections(
                body,
                hit,
                avg=self.avg,
                count=self.count,
                my_review=self.my_review if self.ephemeral and not self.published_wid else None,
                social_line=self.social_line,
            )
            footer = _footer_line(hit)
            if footer:
                body.append(discord.ui.Separator())
                body.append(discord.ui.TextDisplay(f"-# {footer}"))
        else:
            body.extend(fiche_intro(hit)[:2])
            if not self.reviews:
                empty = "*Pas encore de critique sur ce serveur.*"
                if hit.url:
                    empty += f"\n-# [{_link_label(hit)}]({hit.url})"
                body.append(discord.ui.TextDisplay(empty))
            else:
                start = self.review_page * REVIEWS_PAGE
                page_rows = self.reviews[start:start + REVIEWS_PAGE]
                for row in page_rows:
                    user_id = int(row["user_id"])
                    _name, avatar = _user_display(self.guild, self.cog.bot, user_id)
                    title = self.titles.get(user_id, title_for_level(1))
                    text = (
                        f"{_titled(_mention(self.guild, self.cog.bot, user_id), title)}\n"
                        f"{format_stars(row['rating'])}  **{row['rating']:g}/5** · <t:{row['updated_at']}:R>"
                    )
                    if row["comment"]:
                        text += f"\n{pretty.shorten_text(row['comment'], 220)}"
                    seen = experienced_line(hit.media_type, experienced_from_row(row))
                    if seen:
                        text += f"\n{seen}"
                    body.append(section_with_thumbnail(text, avatar))
                total_pages = max(1, (len(self.reviews) + REVIEWS_PAGE - 1) // REVIEWS_PAGE)
                page_note = (
                    f"-# {self.count} critique(s) · moyenne {format_stars(self.avg or 0)} "
                    f"{(self.avg or 0):.1f}/5 · page {self.review_page + 1}/{total_pages}"
                )
                if hit.url:
                    page_note += f"  ·  [{_link_label(hit)}]({hit.url})"
                body.append(discord.ui.TextDisplay(page_note))

        rows.append(discord.ui.ActionRow(
            TabButton(self, "fiche", "Fiche"),
            TabButton(self, "critiques", f"Critiques ({self.count})"),
        ))
        if not self.published_wid:
            rate_label = "Noter"
            if self.ephemeral and self.pending_rating is not None and self.my_review is None:
                rate_label = f"Noter {format_stars_compact(self.pending_rating)}"
            elif self.ephemeral and self.my_review:
                rate_label = "Modifier ma note"
            rate_btn = RateButton(self)
            rate_btn.label = rate_label
            actions: list[discord.ui.Item] = [rate_btn]
            if self.ephemeral and self.my_review:
                actions.append(DeleteReviewButton(self))
            if self.ephemeral:
                actions.append(PublishFicheButton(self))
            rows.append(discord.ui.ActionRow(*actions))
        if self.tab == "critiques" and len(self.reviews) > REVIEWS_PAGE:
            nav_btns: list[discord.ui.Item] = []
            if self.review_page > 0:
                nav_btns.append(_ReviewPageButton(self, -1, "← Précédent"))
            if (self.review_page + 1) * REVIEWS_PAGE < len(self.reviews):
                nav_btns.append(_ReviewPageButton(self, 1, "Suivant →"))
            if nav_btns:
                rows.append(discord.ui.ActionRow(*nav_btns))
        self.set_layout(body, *rows)

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        await self.reload_stats()
        self._build()
        try:
            if interaction is not None:
                await apply_view(interaction, self)
            elif self._message is not None:
                await self._message.edit(view=self)
            elif self._interaction is not None:
                await self._interaction.edit_original_response(view=self)
        except discord.HTTPException as exc:
            logger.warning("Impossible de rafraîchir la fiche « %s » : %s", self.hit.title, exc)

    async def start(self, interaction: discord.Interaction, *, deferred: bool = False) -> None:
        self._interaction = interaction
        await self.prepare()
        if deferred:
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.send_message(view=self, ephemeral=self.ephemeral)


class _ReviewPageButton(discord.ui.Button):
    def __init__(self, parent: MediaSessionView, delta: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._hub = parent
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.review_page = max(0, self._hub.review_page + self._delta)
        self._hub._build()
        await apply_view(interaction, self._hub)


# ---------------------------------------------------------------------------
# Sélections partagées (profil / explorateur)
# ---------------------------------------------------------------------------

class JournalOpenSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(f"{format_stars_select(row['rating'])}  {hit.title}", 95),
                value=str(index),
                description=pretty.shorten_text(f"{type_label(hit.media_type)} · {hit.year or '—'} · {row['rating']:g}/5", 95),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _row = self._items[int(self.values[0])]
        await interaction.response.defer()
        await open_public_fiche(self._hub.cog, self._hub.guild, interaction, hit)


class CatalogOpenSelect(discord.ui.Select):
    def __init__(self, parent: "ServerHubView", page_items: list[tuple[MediaHit, float, int]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95),
                value=str(index),
                description=pretty.shorten_text(
                    f"{type_label(hit.media_type)} · {format_stars_select(avg)}/5 · {count} note{'s' if count > 1 else ''}",
                    95,
                ),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, avg, count) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _avg, _count = self._items[int(self.values[0])]
        await interaction.response.defer()
        await open_public_fiche(self._hub.cog, self._hub.guild, interaction, hit)


class RecentOpenSelect(discord.ui.Select):
    def __init__(self, parent: "ServerHubView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95),
                value=str(index),
                description=pretty.shorten_text(f"{format_stars_select(row['rating'])}/5 · {type_label(hit.media_type)}", 95),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _row = self._items[int(self.values[0])]
        await interaction.response.defer()
        await open_public_fiche(self._hub.cog, self._hub.guild, interaction, hit)


class AffinityCompareSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView"):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(parent._name(item.user_id), 95),
                value=str(item.user_id),
                description=pretty.shorten_text(
                    f"{item.percent:.0f} % · {item.overlap} œuvre{'s' if item.overlap > 1 else ''} en commun",
                    95,
                ),
            )
            for item in parent.affinities[:25]
        ]
        super().__init__(placeholder="Comparer avec…", options=options)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        other_id = int(self.values[0])
        view = AffinityCompareView(
            self._hub.cog,
            self._hub.guild,
            self._hub.member.id,
            other_id,
            affinity=next(a for a in self._hub.affinities if a.user_id == other_id),
            titles=self._hub.titles,
        )
        view._interaction = interaction
        view._message = await interaction.followup.send(view=view)


class AffinityCompareView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        left_id: int,
        right_id: int,
        *,
        affinity: Affinity,
        titles: dict[int, str],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.left_id = left_id
        self.right_id = right_id
        self.affinity = affinity
        self.titles = titles
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self._build()

    def _person(self, user_id: int) -> str:
        return _titled(
            _mention(self.guild, self.cog.bot, user_id),
            self.titles.get(user_id, title_for_level(1)),
        )

    def _build(self) -> None:
        _left_name, left_avatar = _user_display(self.guild, self.cog.bot, self.left_id)
        body: list[discord.ui.Item] = [
            section_with_thumbnail(
                f"{self._person(self.left_id)}\n{self._person(self.right_id)}\n"
                f"-# {self.affinity.percent:.0f} % d'accord  ·  {self.affinity.overlap} œuvre(s) en commun",
                left_avatar,
            ),
        ]
        if self.affinity.agreements:
            lines = ["**D'accord**"]
            for title, left, right in self.affinity.agreements:
                lines.append(f"{format_stars_compact(left)} / {format_stars_compact(right)}  ·  {title}")
            body.append(discord.ui.Separator())
            body.append(discord.ui.TextDisplay("\n".join(lines)))
        if self.affinity.disagreements:
            lines = ["**Désaccord**"]
            for title, left, right in self.affinity.disagreements:
                lines.append(f"{format_stars_compact(left)} / {format_stars_compact(right)}  ·  {title}")
            body.append(discord.ui.Separator())
            body.append(discord.ui.TextDisplay("\n".join(lines)))
        self.set_layout(body)


class FavoriteSlotButton(discord.ui.Button):
    def __init__(self, parent: "ProfileView", slot: int, filled: bool):
        label = FAVORITE_LABELS[slot]
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary if filled else discord.ButtonStyle.green,
        )
        self._hub = parent
        self._slot = slot

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._hub.member.id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul le propriétaire du profil peut modifier ses préférées.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.send_modal(FavoriteSearchModal(self._hub, self._slot))


class FavoriteClearSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView", filled: list[int]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(f"Retirer · {FAVORITE_LABELS[slot]}", 95),
                value=str(slot),
                description="Enlever cette œuvre du profil",
            )
            for slot in filled
        ]
        super().__init__(placeholder="Retirer une préférée…", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._hub.member.id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul le propriétaire du profil peut modifier ses préférées.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.defer()
        slot = int(self.values[0])
        await self._hub.cog.clear_favorite(self._hub.guild, self._hub.member.id, slot)
        await self._hub.refresh(interaction)
        await interaction.followup.send(
            f"**Préférée retirée ·** {FAVORITE_LABELS[slot]} est de nouveau vide.",
            ephemeral=True,
        )


class FavoriteSearchModal(discord.ui.Modal):
    def __init__(self, profile: "ProfileView", slot: int):
        super().__init__(title=pretty.shorten_text(f"Choisir · {FAVORITE_LABELS[slot]}", 45))
        self._profile = profile
        self._slot = slot
        self.query_input = discord.ui.TextInput(
            label="Titre de l'œuvre",
            placeholder="Ex. Inception, Dune 2021, Disco Elysium…",
            min_length=2,
            max_length=80,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._profile.member.id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul le propriétaire du profil peut modifier ses préférées.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        catalog = self._profile.cog.catalog
        if catalog is None:
            await interaction.followup.send("**Erreur ·** Catalogue média indisponible.", ephemeral=True)
            return
        query = str(self.query_input.value).strip()
        try:
            hits = await catalog.search(query, "all")
        except Exception:
            logger.exception("Recherche de préférée impossible")
            await interaction.followup.send("**Erreur ·** Recherche impossible pour le moment.", ephemeral=True)
            return
        if not hits:
            await interaction.followup.send(
                f"**Erreur ·** Aucun résultat pour « {pretty.shorten_text(query, 80)} ».",
                ephemeral=True,
            )
            return
        if len(hits) == 1:
            await self._profile.apply_favorite(self._slot, hits[0])
            await interaction.followup.send(
                f"**{FAVORITE_LABELS[self._slot]} ·** {hits[0].title}",
                ephemeral=True,
            )
            return
        view = FavoritePickView(self._profile, self._slot, hits)
        await interaction.followup.send(view=view, ephemeral=True)


class FavoriteHitSelect(discord.ui.Select):
    def __init__(self, parent: "FavoritePickView", hits: list[MediaHit]):
        options = []
        for index, hit in enumerate(hits[:25]):
            options.append(
                discord.SelectOption(
                    label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                    value=str(index),
                    description=select_hit_description(hit),
                    emoji=select_emoji(hit.media_type),
                )
            )
        super().__init__(placeholder="Choisir une œuvre", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        hit = self._hub.hits[int(self.values[0])]
        await self._hub.profile.apply_favorite(self._hub.slot, hit)
        done = discord.ui.LayoutView(timeout=30)
        box = discord.ui.Container()
        box.add_item(discord.ui.TextDisplay(
            f"**{FAVORITE_LABELS[self._hub.slot]} ·** {hit.title}"
        ))
        done.add_item(box)
        await interaction.edit_original_response(view=done)


class FavoritePickView(ReviewsLayout):
    def __init__(self, profile: "ProfileView", slot: int, hits: list[MediaHit]):
        super().__init__(timeout=180)
        self.profile = profile
        self.slot = slot
        self.hits = hits
        self.set_layout(
            [discord.ui.TextDisplay(
                f"## {FAVORITE_LABELS[slot]}\n-# {len(hits)} résultat(s) — choisis l'œuvre à épingler"
            )],
            discord.ui.ActionRow(FavoriteHitSelect(self, hits)),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.profile.member.id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul le propriétaire du profil peut modifier ses préférées.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True


class ProfileView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        member: discord.Member | discord.User,
        *,
        xp: int,
        review_count: int,
        average: float | None,
        twin: Affinity | None,
        rival: Affinity | None,
        titles: dict[int, str],
        favorites: list[tuple[MediaHit, float | None] | None],
        journal_entries: list[tuple[MediaHit, Any]],
        affinities: list[Affinity],
        viewer_id: int,
        tab: str = "profil",
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.member = member
        self.xp = xp
        self.review_count = review_count
        self.average = average
        self.twin = twin
        self.rival = rival
        self.titles = titles
        self.favorites = favorites
        self.journal_entries = journal_entries
        self.affinities = sorted(affinities, key=lambda a: (-a.percent, -a.overlap))
        self.viewer_id = viewer_id
        self.editable = viewer_id == member.id
        self.tab = tab
        self.journal_page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce profil ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _name(self, user_id: int) -> str:
        name, _avatar = _user_display(self.guild, self.cog.bot, user_id)
        return name

    def _person(self, user_id: int) -> str:
        return _titled(
            _mention(self.guild, self.cog.bot, user_id),
            self.titles.get(user_id, title_for_level(1)),
        )

    def _profile_header(self, extra: str = "") -> str:
        level, into, need, total = level_progress(self.xp)
        title = self.titles.get(self.member.id, title_for_level(level))
        mention = _mention(self.guild, self.cog.bot, self.member.id)
        notes = f"**{self.review_count}** note{'s' if self.review_count != 1 else ''}"
        if self.average is not None:
            notes += f"  ·  moyenne **{self.average:.1f}/5**"
        lines = [
            f"## {mention}",
            f"{XP} **{total} XP** · niveau **{level}**  ·  {notes}",
            f"-# {into}/{need} vers le niveau {level + 1}",
        ]
        if extra:
            lines.append(extra)
        lines.append(f"-# {title}")
        return "\n".join(lines)

    def _tabs_row(self) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            HubTabButton(self, "profil", "Profil"),
            HubTabButton(self, "journal", f"Journal ({self.review_count})"),
            HubTabButton(self, "affinites", "Affinités"),
            ProfileShareButton(self),
        )

    def _page_nav(self, attr: str, max_page: int) -> discord.ui.ActionRow | None:
        if max_page <= 0:
            return None
        page = getattr(self, attr)
        prev_btn = HubPageButton(self, attr, -1, "← Précédent", max_page)
        next_btn = HubPageButton(self, attr, 1, "Suivant →", max_page)
        prev_btn.disabled = page <= 0
        next_btn.disabled = page >= max_page
        return discord.ui.ActionRow(prev_btn, next_btn)

    def _profil_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        avatar = self.member.display_avatar.url if hasattr(self.member, "display_avatar") else None
        body: list[discord.ui.Item] = [section_with_thumbnail(self._profile_header(), avatar)]
        for index, (slot, label) in enumerate(FAVORITE_LABELS.items()):
            body.append(discord.ui.Separator())
            entry = self.favorites[slot - 1] if slot - 1 < len(self.favorites) else None
            if entry is None:
                hint = "*Pas encore choisi.*"
                if self.editable:
                    hint += "\n-# Appuie sur le bouton pour en épingler une."
                body.append(discord.ui.TextDisplay(f"### {label}\n{hint}"))
            else:
                hit, rating = entry
                year = f"  ·  {hit.year}" if hit.year else ""
                lines = [
                    f"### {label}",
                    f"{type_emoji(hit.media_type)} **{hit.title}**{year}",
                    f"-# {type_label(hit.media_type)}"
                    + (f"  ·  {hit.subtitle}" if hit.subtitle else ""),
                ]
                if rating is not None:
                    lines.append(f"{format_stars(rating)}  **{rating:g}/5**")
                body.append(section_with_thumbnail("\n".join(lines), hit.poster_url))
        rows: list[discord.ui.ActionRow] = []
        if self.editable:
            rows.append(discord.ui.ActionRow(*[
                FavoriteSlotButton(self, slot, bool(self.favorites[slot - 1] if slot - 1 < len(self.favorites) else None))
                for slot in FAVORITE_LABELS
            ]))
            filled_slots = [
                slot for slot in FAVORITE_LABELS
                if slot - 1 < len(self.favorites) and self.favorites[slot - 1] is not None
            ]
            if filled_slots:
                rows.append(discord.ui.ActionRow(FavoriteClearSelect(self, filled_slots)))
        return body, rows

    def _journal_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        extra = ""
        types: dict[str, int] = {}
        for hit, _row in self.journal_entries:
            types[hit.media_type] = types.get(hit.media_type, 0) + 1
        if types:
            top_type = max(types, key=types.get)
            extra = f"-# {types[top_type]} {type_label(top_type).lower()}{'s' if types[top_type] > 1 else ''}"
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(self._profile_header(extra))]
        rows: list[discord.ui.ActionRow] = []
        if not self.journal_entries:
            body.append(discord.ui.TextDisplay("*Aucune œuvre notée pour l'instant.*"))
            return body, rows
        max_page = max(0, (len(self.journal_entries) - 1) // JOURNAL_PAGE)
        self.journal_page = min(self.journal_page, max_page)
        start = self.journal_page * JOURNAL_PAGE
        page_items = self.journal_entries[start:start + JOURNAL_PAGE]
        for hit, row in page_items:
            year = f" ({hit.year})" if hit.year else ""
            text = f"{format_stars(row['rating'])}  **{hit.title}**{year}\n-# {type_label(hit.media_type)}"
            if row["comment"]:
                text += f"\n{pretty.shorten_text(row['comment'], 180)}"
            seen = experienced_line(hit.media_type, experienced_from_row(row))
            if seen:
                text += f"\n{seen}"
            body.append(section_with_thumbnail(text, hit.poster_url))
        rows.append(discord.ui.ActionRow(JournalOpenSelect(self, page_items)))
        nav = self._page_nav("journal_page", max_page)
        if nav:
            rows.append(nav)
        return body, rows

    def _affinites_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        _me, avatar = _user_display(self.guild, self.cog.bot, self.member.id)
        rows: list[discord.ui.ActionRow] = []
        extra = (
            f"-# {len(self.affinities)} affinité(s) · min. {MIN_AFFINITY_OVERLAP} en commun"
            if self.affinities else ""
        )
        if not self.affinities:
            body = [section_with_thumbnail(
                f"{self._profile_header()}\n"
                f"*Pas encore assez d'œuvres en commun avec quelqu'un "
                f"(minimum {MIN_AFFINITY_OVERLAP}).*",
                avatar,
            )]
            return body, rows
        twins = self.affinities[:3]
        rival = min(self.affinities, key=lambda a: (a.percent, -a.overlap))
        lines = [
            self._profile_header(extra),
            "",
            f"### {TWIN} Jumeaux",
        ]
        for twin in twins:
            lines.append(self._person(twin.user_id))
            lines.append(f"-# {twin.percent:.0f} % · {twin.overlap} en commun")
        if rival.user_id not in {t.user_id for t in twins}:
            lines.append("")
            lines.append(f"### {RIVAL} Rival")
            lines.append(self._person(rival.user_id))
            lines.append(f"-# {rival.percent:.0f} %")
        body = [section_with_thumbnail("\n".join(lines), avatar)]
        rows.append(discord.ui.ActionRow(AffinityCompareSelect(self)))
        return body, rows

    def _build(self) -> None:
        if self.tab == "journal":
            body, rows = self._journal_layout()
        elif self.tab == "affinites":
            body, rows = self._affinites_layout()
        else:
            body, rows = self._profil_layout()
        self.set_layout(body, *rows, self._tabs_row())

    async def apply_favorite(self, slot: int, hit: MediaHit) -> None:
        if not self.editable:
            return
        if self.cog.catalog is not None:
            try:
                hit = await self.cog.catalog.enrich(hit)
            except Exception:
                logger.exception("Enrichissement de préférée impossible")
        await self.cog.set_favorite(self.guild, self.member.id, slot, hit)
        await self.refresh()

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        self.favorites = await self.cog.get_favorites(self.guild, self.member.id)
        self._build()
        editor = interaction or self._interaction
        if editor is None:
            return
        try:
            await apply_view(editor, self)
        except discord.HTTPException as exc:
            logger.warning("Impossible de rafraîchir le profil : %s", exc)


class TopPeriodSelect(discord.ui.Select):
    def __init__(self, parent: "ServerHubView"):
        options = [
            discord.SelectOption(label="Toutes périodes", value="all", default=parent.period == "all"),
            discord.SelectOption(label="Cette semaine", value="semaine", default=parent.period == "semaine"),
            discord.SelectOption(label="Ce mois", value="mois", default=parent.period == "mois"),
        ]
        super().__init__(placeholder="Période du top", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._hub.period = self.values[0]
        self._hub.top_page = 0
        self._hub.top_items = await self._hub.cog.load_top(
            self._hub.guild,
            media_type=self._hub.media_type,
            period=self._hub.period,
        )
        await self._hub.refresh(interaction)


class ServerHubView(ReviewsLayout):
    """Récentes, catalogue et top du serveur, dans une seule vue à onglets."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        recent: list[tuple[MediaHit, Any]],
        catalog: list[tuple[MediaHit, float, int]],
        top: list[tuple[MediaHit, float, int]],
        titles: dict[int, str],
        catalog_subtitle: str,
        media_type: str = "all",
        period: str = "all",
        tab: str = "recentes",
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.recent = recent
        self.catalog = catalog
        self.top_items = top
        self.titles = titles
        self.catalog_subtitle = catalog_subtitle
        self.media_type = media_type
        self.period = period
        self.tab = tab
        self.recent_page = 0
        self.catalog_page = 0
        self.top_page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    def _tabs_row(self) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            HubTabButton(self, "recentes", "Récentes"),
            HubTabButton(self, "catalogue", f"Catalogue ({len(self.catalog)})"),
            HubTabButton(self, "top", "Top"),
        )

    def _page_nav(self, attr: str, max_page: int) -> discord.ui.ActionRow | None:
        if max_page <= 0:
            return None
        page = getattr(self, attr)
        prev_btn = HubPageButton(self, attr, -1, "← Précédent", max_page)
        next_btn = HubPageButton(self, attr, 1, "Suivant →", max_page)
        prev_btn.disabled = page <= 0
        next_btn.disabled = page >= max_page
        return discord.ui.ActionRow(prev_btn, next_btn)

    def _ranked_layout(
        self,
        *,
        title: str,
        subtitle: str,
        items: list[tuple[MediaHit, float, int]],
        page_attr: str,
        extra_row: discord.ui.ActionRow | None = None,
    ) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(f"## {title}\n-# {subtitle}")]
        rows: list[discord.ui.ActionRow] = []
        if extra_row:
            rows.append(extra_row)
        if not items:
            body.append(discord.ui.TextDisplay("*Aucune œuvre ne correspond à cette recherche.*"))
            return body, rows
        max_page = max(0, (len(items) - 1) // CATALOG_PAGE)
        page = min(getattr(self, page_attr), max_page)
        setattr(self, page_attr, page)
        start = page * CATALOG_PAGE
        page_items = items[start:start + CATALOG_PAGE]
        lines = []
        for index, (hit, avg, count) in enumerate(page_items, start=start + 1):
            year = f" ({hit.year})" if hit.year else ""
            lines.append(
                f"**{index}.** {format_stars(avg)}  **{hit.title}**{year}  ·  "
                f"{type_label(hit.media_type)}  ·  {count} note{'s' if count > 1 else ''}"
            )
        body.append(discord.ui.TextDisplay("\n".join(lines)))
        rows.append(discord.ui.ActionRow(CatalogOpenSelect(self, page_items)))
        nav = self._page_nav(page_attr, max_page)
        if nav:
            rows.append(nav)
        return body, rows

    def _recentes_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(
            f"## Dernières critiques\n-# {len(self.recent)} récente(s) sur ce serveur"
        )]
        rows: list[discord.ui.ActionRow] = []
        if not self.recent:
            body.append(discord.ui.TextDisplay("*Personne n'a encore noté d'œuvre ici.*"))
            return body, rows
        max_page = max(0, (len(self.recent) - 1) // JOURNAL_PAGE)
        self.recent_page = min(self.recent_page, max_page)
        start = self.recent_page * JOURNAL_PAGE
        page_items = self.recent[start:start + JOURNAL_PAGE]
        for hit, row in page_items:
            user_id = int(row["user_id"])
            _name, avatar = _user_display(self.guild, self.cog.bot, user_id)
            year = f" ({hit.year})" if hit.year else ""
            text = (
                f"{_titled(_mention(self.guild, self.cog.bot, user_id), self.titles.get(user_id, title_for_level(1)))}\n"
                f"{format_stars(row['rating'])}  **{row['rating']:g}/5**\n"
                f"**{hit.title}**{year} · {type_label(hit.media_type)} · <t:{row['updated_at']}:R>"
            )
            if row["comment"]:
                text += f"\n*{pretty.shorten_text(row['comment'], 180)}*"
            seen = experienced_line(hit.media_type, experienced_from_row(row))
            if seen:
                text += f"\n{seen}"
            body.append(section_with_thumbnail(text, avatar or hit.poster_url))
        rows.append(discord.ui.ActionRow(RecentOpenSelect(self, page_items)))
        nav = self._page_nav("recent_page", max_page)
        if nav:
            rows.append(nav)
        return body, rows

    def _build(self) -> None:
        if self.tab == "catalogue":
            body, rows = self._ranked_layout(
                title="Catalogue du serveur",
                subtitle=self.catalog_subtitle,
                items=self.catalog,
                page_attr="catalog_page",
            )
        elif self.tab == "top":
            period_label = {"all": "toutes périodes", "semaine": "cette semaine", "mois": "ce mois"}.get(
                self.period, self.period
            )
            type_part = type_label(self.media_type) if self.media_type != "all" else "Tous types"
            body, rows = self._ranked_layout(
                title="Top du serveur",
                subtitle=f"{type_part}  ·  {period_label}",
                items=self.top_items,
                page_attr="top_page",
                extra_row=discord.ui.ActionRow(TopPeriodSelect(self)),
            )
        else:
            body, rows = self._recentes_layout()
        self.set_layout(body, *rows, self._tabs_row())

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        self._build()
        editor = interaction or self._interaction
        if editor is None:
            return
        try:
            await apply_view(editor, self)
        except discord.HTTPException as exc:
            logger.warning("Impossible de rafraîchir l'explorateur : %s", exc)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class CommentMaxModal(discord.ui.Modal, title="Longueur max. du commentaire"):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__()
        self._view_ref = view_ref
        self.length_input = discord.ui.TextInput(
            label=f"Caractères ({MIN_COMMENT_MAX}-{MAX_COMMENT_MAX})",
            placeholder=str(DEFAULT_COMMENT_MAX),
            default=str(view_ref.comment_max),
            max_length=3,
        )
        self.add_item(self.length_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        raw = self.length_input.value.strip()
        if not raw.isdigit() or not (MIN_COMMENT_MAX <= int(raw) <= MAX_COMMENT_MAX):
            await interaction.followup.send(
                f"**Erreur ·** Indique un entier entre {MIN_COMMENT_MAX} et {MAX_COMMENT_MAX}.",
                ephemeral=True,
            )
            return
        await self._view_ref.cog.data.get(self._view_ref.guild).set_dict_value("settings", "MaxCommentLength", int(raw))
        await self._view_ref.refresh(interaction)


class EditCommentMaxButton(discord.ui.Button):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__(label="Modifier", style=discord.ButtonStyle.secondary)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CommentMaxModal(self._view_ref))


class ToggleAnnounceButton(discord.ui.Button):
    def __init__(self, view_ref: "ReviewsConfigView"):
        active = view_ref.announce_channel is not None
        super().__init__(
            label="Désactiver" if active else "Activer",
            style=discord.ButtonStyle.red if active else discord.ButtonStyle.green,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        cog, guild = self._view_ref.cog, self._view_ref.guild
        settings = cog.data.get(guild)
        if self._view_ref.announce_channel is not None:
            await settings.set_dict_value("settings", "LastAnnounceChannelID", self._view_ref.announce_channel.id)
            await settings.set_dict_value("settings", "AnnounceChannelID", 0)
            await self._view_ref.refresh(interaction)
            return
        last_id = await settings.get_dict_value("settings", "LastAnnounceChannelID", cast=int)
        channel = guild.get_channel(last_id) if last_id else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "**Erreur ·** Sélectionnez d'abord un salon via le menu ci-dessous.", ephemeral=True
            )
            return
        if not channel.permissions_for(guild.me).send_messages:
            await interaction.followup.send(
                "**Erreur ·** Je n'ai pas la permission d'envoyer des messages sur ce salon.", ephemeral=True
            )
            return
        await settings.set_dict_value("settings", "AnnounceChannelID", channel.id)
        await self._view_ref.refresh(interaction)


class AnnounceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Sélectionner le salon d'annonce",
            min_values=0,
            max_values=1,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        cog, guild = self._view_ref.cog, self._view_ref.guild
        if not self.values:
            if self._view_ref.announce_channel is not None:
                await cog.data.get(guild).set_dict_value(
                    "settings", "LastAnnounceChannelID", self._view_ref.announce_channel.id
                )
            await cog.data.get(guild).set_dict_value("settings", "AnnounceChannelID", 0)
            await self._view_ref.refresh(interaction)
            return
        channel = self.values[0].resolve()
        if channel is None:
            try:
                channel = await self.values[0].fetch()
            except discord.HTTPException:
                await interaction.followup.send("**Erreur ·** Salon introuvable.", ephemeral=True)
                return
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "**Erreur ·** Seuls les salons textuels sont pris en charge.", ephemeral=True
            )
            return
        if not channel.permissions_for(guild.me).send_messages:
            await interaction.followup.send(
                "**Erreur ·** Je n'ai pas la permission d'envoyer des messages sur ce salon.", ephemeral=True
            )
            return
        await cog.data.get(guild).set_dict_value("settings", "AnnounceChannelID", channel.id)
        await self._view_ref.refresh(interaction)


class ReviewsConfigView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        announce_channel: discord.TextChannel | None,
        comment_max: int,
        review_count: int,
        media_count: int,
        api_status: dict[str, bool],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.announce_channel = announce_channel
        self.comment_max = comment_max
        self.review_count = review_count
        self.media_count = media_count
        self.api_status = api_status
        self._interaction: discord.Interaction | None = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "**Action impossible ·** La permission `Gérer le serveur` est requise.", ephemeral=True
            )
            return False
        return True

    def _build(self) -> None:
        apis = "  ·  ".join(f"{name} {'ok' if ok else 'manquant'}" for name, ok in self.api_status.items())
        self.set_layout(
            [
                discord.ui.TextDisplay(f"## Configuration des critiques — {self.guild.name}"),
                discord.ui.Separator(),
                discord.ui.Section(
                    f"**Salon d'annonce**\n{self.announce_channel.mention if self.announce_channel else '*Non configuré*'}",
                    accessory=ToggleAnnounceButton(self),
                ),
                discord.ui.Separator(),
                discord.ui.Section(
                    f"**Commentaire max.**\n{self.comment_max} caractères",
                    accessory=EditCommentMaxButton(self),
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"-# {self.review_count} critique(s) · {self.media_count} œuvre(s)\n-# APIs · {apis}"
                ),
            ],
            discord.ui.ActionRow(AnnounceChannelSelect(self)),
        )

    async def _reload(self) -> None:
        self.announce_channel = await self.cog.get_announce_channel(self.guild)
        self.comment_max = await self.cog.get_comment_max(self.guild)
        self.review_count, self.media_count = await self.cog.counts(self.guild)
        self.api_status = self.cog.catalog.status() if self.cog.catalog else {}

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        await self._reload()
        self._build()
        editor = interaction or self._interaction
        if editor is not None:
            await apply_view(editor, self)

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.response.send_message(view=self, ephemeral=True)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Reviews(commands.Cog):
    """Carnet de critiques interne au serveur (films, séries, jeux, musique, livres)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)
        self._http: aiohttp.ClientSession | None = None
        self.catalog: MediaCatalog | None = None  # type: ignore[assignment]
        self._schema_ready: set[int] = set()

        settings = dataio.DictTableBuilder(
            "settings",
            {
                "AnnounceChannelID": 0,
                "LastAnnounceChannelID": 0,
                "MaxCommentLength": DEFAULT_COMMENT_MAX,
                "BackfilledXP": "0",
            },
        )
        media_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                title TEXT NOT NULL,
                subtitle TEXT NOT NULL DEFAULT '',
                year INTEGER,
                poster_url TEXT,
                url TEXT,
                overview TEXT NOT NULL DEFAULT '',
                genres TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(source, source_id, media_type)
            )"""
        )
        reviews_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                rating REAL NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                experienced_at TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(user_id, media_id)
            )"""
        )
        profiles_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                xp INTEGER NOT NULL DEFAULT 0,
                daily_xp INTEGER NOT NULL DEFAULT 0,
                daily_date TEXT NOT NULL DEFAULT '',
                daily_awards INTEGER NOT NULL DEFAULT 0,
                last_level INTEGER NOT NULL DEFAULT 1
            )"""
        )
        favorites_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, slot)
            )"""
        )
        self.data.link(discord.Guild, settings, media_table, reviews_table, profiles_table, favorites_table)

    async def cog_load(self) -> None:
        config = dict(getattr(self.bot, "config", {}) or {})
        try:
            from dotenv import dotenv_values
            for key, value in dotenv_values(".env").items():
                if value:
                    config[key] = value
        except Exception:
            pass
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "ACK-BOT/1.0 (Discord reviews)"},
        )
        self.catalog = MediaCatalog(
            self._http,
            tmdb_key=str(config.get("TMDB_API_KEY") or "").strip(),
            spotify_id=str(config.get("SPOTIFY_CLIENT_ID") or "").strip(),
            spotify_secret=str(config.get("SPOTIFY_CLIENT_SECRET") or "").strip(),
        )
        status = self.catalog.status()
        missing = [name for name, ok in status.items() if not ok]
        if missing:
            logger.warning("Fournisseurs incomplets : %s", ", ".join(missing))
        self.bot.add_dynamic_items(FicheDynButton)
        self._sweep_fiches.start()

    async def cog_unload(self) -> None:
        self._sweep_fiches.cancel()
        self.bot.remove_dynamic_items(FicheDynButton)
        if self._http is not None:
            await self._http.close()
            self._http = None
        await self.data.close_all()

    @tasks.loop(seconds=30)
    async def _sweep_fiches(self) -> None:
        try:
            await sweep_expired(self.bot, render_published_record)
        except Exception:
            logger.exception("sweep fiches publiées")

    @_sweep_fiches.before_loop
    async def _before_sweep_fiches(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Paramètres
    # ------------------------------------------------------------------

    async def get_announce_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = await self.data.get(guild).get_dict_value("settings", "AnnounceChannelID", cast=int)
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def get_comment_max(self, guild: discord.Guild) -> int:
        value = await self.data.get(guild).get_dict_value("settings", "MaxCommentLength", cast=int)
        if not value:
            return DEFAULT_COMMENT_MAX
        return max(MIN_COMMENT_MAX, min(MAX_COMMENT_MAX, int(value)))

    async def counts(self, guild: discord.Guild) -> tuple[int, int]:
        db = self.data.get(guild)
        reviews = await db.fetchone("SELECT COUNT(*) AS n FROM reviews")
        media = await db.fetchone("SELECT COUNT(*) AS n FROM media")
        return int(reviews["n"] if reviews else 0), int(media["n"] if media else 0)

    # ------------------------------------------------------------------
    # XP / affinités
    # ------------------------------------------------------------------

    async def ensure_progress(self, guild: discord.Guild) -> None:
        settings = self.data.get(guild)
        flag = await settings.get_dict_value("settings", "BackfilledXP")
        if flag == "1":
            return
        rows = await settings.fetchall(
            """SELECT user_id, COUNT(*) AS n,
                      SUM(CASE WHEN comment != '' THEN 1 ELSE 0 END) AS comments
               FROM reviews GROUP BY user_id"""
        )
        for row in rows:
            xp = int(row["n"]) * 10 + int(row["comments"] or 0) * 10
            await settings.execute(
                """INSERT OR IGNORE INTO profiles
                   (user_id, xp, daily_xp, daily_date, daily_awards, last_level)
                   VALUES (?, ?, 0, '', 0, ?)""",
                int(row["user_id"]),
                xp,
                level_for_xp(xp),
            )
        await settings.set_dict_value("settings", "BackfilledXP", "1")

    async def get_profile_xp(self, guild: discord.Guild, user_id: int) -> int:
        await self.ensure_progress(guild)
        row = await self.data.get(guild).fetchone("SELECT xp FROM profiles WHERE user_id=?", user_id)
        return int(row["xp"]) if row else 0

    async def get_titles(self, guild: discord.Guild, user_ids: list[int]) -> dict[int, str]:
        await self.ensure_progress(guild)
        unique = list({int(user_id) for user_id in user_ids})
        if not unique:
            return {}
        placeholders = ", ".join("?" for _ in unique)
        rows = await self.data.get(guild).fetchall(
            f"SELECT user_id, xp FROM profiles WHERE user_id IN ({placeholders})",
            *unique,
        )
        xp_by_user = {int(row["user_id"]): int(row["xp"]) for row in rows}
        return {user_id: title_for_level(level_for_xp(xp_by_user.get(user_id, 0))) for user_id in unique}

    async def _ensure_schema(self, guild: discord.Guild) -> None:
        if guild.id in self._schema_ready:
            return
        db = self.data.get(guild)
        columns = await db.column_names("reviews")
        if "experienced_at" not in columns:
            await db.execute("ALTER TABLE reviews ADD COLUMN experienced_at TEXT NOT NULL DEFAULT ''")
        self._schema_ready.add(guild.id)

    async def get_favorites(
        self, guild: discord.Guild, user_id: int
    ) -> list[tuple[MediaHit, float | None] | None]:
        rows = await self.data.get(guild).fetchall(
            """SELECT f.slot, m.*, r.rating AS fav_rating
               FROM favorites f
               JOIN media m ON m.id = f.media_id
               LEFT JOIN reviews r ON r.media_id = f.media_id AND r.user_id = f.user_id
               WHERE f.user_id=?""",
            user_id,
        )
        slots: list[tuple[MediaHit, float | None] | None] = [None, None, None]
        for row in rows:
            slot = int(row["slot"])
            if slot < 1 or slot > 3:
                continue
            rating = row["fav_rating"]
            slots[slot - 1] = (hit_from_row(row), float(rating) if rating is not None else None)
        return slots

    async def set_favorite(
        self, guild: discord.Guild, user_id: int, slot: int, hit: MediaHit
    ) -> None:
        if slot not in FAVORITE_LABELS:
            return
        media_id = await self.upsert_media(guild, hit)
        db = self.data.get(guild)
        await db.execute("DELETE FROM favorites WHERE user_id=? AND media_id=?", user_id, media_id)
        await db.execute(
            """INSERT INTO favorites (user_id, slot, media_id) VALUES (?, ?, ?)
               ON CONFLICT(user_id, slot) DO UPDATE SET media_id=excluded.media_id""",
            user_id,
            slot,
            media_id,
        )

    async def clear_favorite(self, guild: discord.Guild, user_id: int, slot: int) -> None:
        await self.data.get(guild).execute(
            "DELETE FROM favorites WHERE user_id=? AND slot=?",
            user_id,
            slot,
        )

    async def load_journal(
        self, guild: discord.Guild, user_id: int, media_type: str = "all"
    ) -> list[tuple[MediaHit, Any]]:
        await self._ensure_schema(guild)
        db = self.data.get(guild)
        if media_type == "all":
            rows = await db.fetchall(
                """SELECT r.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                          m.poster_url, m.url, m.overview, m.genres, m.extra_json
                   FROM reviews r JOIN media m ON m.id = r.media_id
                   WHERE r.user_id=?
                   ORDER BY r.updated_at DESC""",
                user_id,
            )
        else:
            rows = await db.fetchall(
                """SELECT r.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                          m.poster_url, m.url, m.overview, m.genres, m.extra_json
                   FROM reviews r JOIN media m ON m.id = r.media_id
                   WHERE r.user_id=? AND m.media_type=?
                   ORDER BY r.updated_at DESC""",
                user_id,
                media_type,
            )
        return [(hit_from_row(row), row) for row in rows]

    async def load_recent(self, guild: discord.Guild, *, limit: int = 40) -> list[tuple[MediaHit, Any]]:
        await self._ensure_schema(guild)
        rows = await self.data.get(guild).fetchall(
            """SELECT r.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                      m.poster_url, m.url, m.overview, m.genres, m.extra_json
               FROM reviews r JOIN media m ON m.id = r.media_id
               ORDER BY r.updated_at DESC
               LIMIT ?""",
            limit,
        )
        return [(hit_from_row(row), row) for row in rows]

    async def load_catalog(
        self,
        guild: discord.Guild,
        *,
        query: str | None = None,
        member_id: int | None = None,
        media_type: str = "all",
        min_rating: float | None = None,
    ) -> list[tuple[MediaHit, float, int]]:
        sql = """SELECT m.*, AVG(r.rating) AS avg_rating, COUNT(r.id) AS n
                 FROM media m JOIN reviews r ON r.media_id = m.id"""
        clauses: list[str] = []
        args: list[Any] = []
        if member_id:
            clauses.append("r.user_id=?")
            args.append(member_id)
        if media_type != "all":
            clauses.append("m.media_type=?")
            args.append(media_type)
        if min_rating is not None:
            clauses.append("r.rating>=?")
            args.append(min_rating)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY m.id"
        rows = await self.data.get(guild).fetchall(sql, *args)
        items = [(hit_from_row(row), float(row["avg_rating"]), int(row["n"])) for row in rows]
        if query:
            items = fuzzy.finder(
                query,
                items,
                key=lambda item: f"{item[0].title} {item[0].subtitle} {item[0].year or ''}",
            )
        return items

    async def load_top(
        self,
        guild: discord.Guild,
        *,
        media_type: str = "all",
        period: str = "all",
    ) -> list[tuple[MediaHit, float, int]]:
        sql = """SELECT m.*, AVG(r.rating) AS avg_rating, COUNT(r.id) AS n
                 FROM media m JOIN reviews r ON r.media_id = m.id"""
        clauses: list[str] = []
        args: list[Any] = []
        if media_type != "all":
            clauses.append("m.media_type=?")
            args.append(media_type)
        if period in PERIOD_SECONDS:
            clauses.append("r.updated_at>=?")
            args.append(int(time.time()) - PERIOD_SECONDS[period])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY m.id ORDER BY avg_rating DESC, n DESC LIMIT 25"
        rows = await self.data.get(guild).fetchall(sql, *args)
        return [(hit_from_row(row), float(row["avg_rating"]), int(row["n"])) for row in rows]

    async def grant_review_xp(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        created: bool,
        pioneer: bool,
        new_comment: bool,
    ) -> XpAward:
        await self.ensure_progress(guild)
        db = self.data.get(guild)
        today = time.strftime("%Y-%m-%d")
        row = await db.fetchone("SELECT * FROM profiles WHERE user_id=?", user_id)
        if row is None:
            xp = daily_xp = daily_awards = 0
            daily_date = ""
            last_level = 1
        else:
            xp = int(row["xp"])
            daily_xp = int(row["daily_xp"])
            daily_date = row["daily_date"] or ""
            daily_awards = int(row["daily_awards"])
            last_level = int(row["last_level"])
        if daily_date != today:
            daily_xp = 0
            daily_awards = 0
            daily_date = today
        base = compute_review_xp(created=created, pioneer=pioneer, new_comment=new_comment)
        gained, capped = apply_daily_limits(base, awards_today=daily_awards, daily_xp=daily_xp)
        xp += gained
        daily_xp += gained
        if gained:
            daily_awards += 1
        new_level = level_for_xp(xp)
        await db.execute(
            """INSERT INTO profiles (user_id, xp, daily_xp, daily_date, daily_awards, last_level)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 xp=excluded.xp,
                 daily_xp=excluded.daily_xp,
                 daily_date=excluded.daily_date,
                 daily_awards=excluded.daily_awards,
                 last_level=excluded.last_level""",
            user_id,
            xp,
            daily_xp,
            daily_date,
            daily_awards,
            new_level,
        )
        return XpAward(
            gained=gained,
            total=xp,
            daily=daily_xp,
            level=new_level,
            previous_level=last_level,
            capped=capped,
        )

    def social_line_for_reviews(
        self,
        guild: discord.Guild,
        reviews: list[Any],
        *,
        viewer_id: int | None,
    ) -> str:
        if len(reviews) < 2:
            return ""

        def name(user_id: int) -> str:
            return _user_display(guild, self.bot, user_id)[0]

        entries = [(int(row["user_id"]), float(row["rating"])) for row in reviews]
        if viewer_id is not None:
            mine = next((item for item in entries if item[0] == viewer_id), None)
            others = [item for item in entries if item[0] != viewer_id]
            if mine and others:
                closest = min(others, key=lambda item: (abs(item[1] - mine[1]), item[0]))
                farthest = max(others, key=lambda item: (abs(item[1] - mine[1]), item[0]))
                if closest[0] == farthest[0]:
                    return f"{name(closest[0])} a mis {closest[1]:g}/5"
                return (
                    f"{name(closest[0])} le plus proche ({closest[1]:g})  ·  "
                    f"{name(farthest[0])} le plus loin ({farthest[1]:g})"
                )

        worst: tuple[float, int, float, int, float] | None = None
        for index, (left_id, left_rating) in enumerate(entries):
            for right_id, right_rating in entries[index + 1 :]:
                diff = abs(left_rating - right_rating)
                if worst is None or diff > worst[0]:
                    worst = (diff, left_id, left_rating, right_id, right_rating)
        if worst and worst[0] >= 1.5:
            return (
                f"Désaccord  ·  {name(worst[1])} {worst[2]:g} vs {name(worst[3])} {worst[4]:g}"
            )
        if worst:
            return f"{name(worst[1])} et {name(worst[3])} sont plutôt d'accord"
        return ""

    async def list_affinities(self, guild: discord.Guild, user_id: int) -> list[Affinity]:
        await self.ensure_progress(guild)
        rows = await self.data.get(guild).fetchall(
            """SELECT r2.user_id AS other_id, r1.rating AS left_rating, r2.rating AS right_rating,
                      m.title, m.year
               FROM reviews r1
               JOIN reviews r2 ON r1.media_id = r2.media_id AND r2.user_id != r1.user_id
               JOIN media m ON m.id = r1.media_id
               WHERE r1.user_id=?""",
            user_id,
        )
        grouped: dict[int, list[tuple[str, float, float]]] = {}
        for row in rows:
            other_id = int(row["other_id"])
            if guild.get_member(other_id) is None:
                continue
            title = row["title"] + (f" ({row['year']})" if row["year"] else "")
            grouped.setdefault(other_id, []).append(
                (title, float(row["left_rating"]), float(row["right_rating"]))
            )
        affinities: list[Affinity] = []
        for other_id, pairs in grouped.items():
            if len(pairs) < MIN_AFFINITY_OVERLAP:
                continue
            ranked = sorted(pairs, key=lambda item: abs(item[1] - item[2]))
            affinities.append(
                Affinity(
                    user_id=other_id,
                    overlap=len(pairs),
                    percent=agreement_percent([(left, right) for _title, left, right in pairs]),
                    agreements=[(title, left, right) for title, left, right in ranked[:3] if abs(left - right) <= 1],
                    disagreements=[
                        (title, left, right) for title, left, right in reversed(ranked[-3:]) if abs(left - right) >= 1
                    ],
                )
            )
        affinities.sort(key=lambda item: (-item.percent, -item.overlap))
        return affinities

    async def get_affinity(self, guild: discord.Guild, left_id: int, right_id: int) -> Affinity | None:
        rows = await self.data.get(guild).fetchall(
            """SELECT r1.rating AS left_rating, r2.rating AS right_rating, m.title, m.year
               FROM reviews r1
               JOIN reviews r2 ON r1.media_id = r2.media_id AND r2.user_id=?
               JOIN media m ON m.id = r1.media_id
               WHERE r1.user_id=?""",
            right_id,
            left_id,
        )
        if not rows:
            return None
        pairs = [
            (
                row["title"] + (f" ({row['year']})" if row["year"] else ""),
                float(row["left_rating"]),
                float(row["right_rating"]),
            )
            for row in rows
        ]
        ranked = sorted(pairs, key=lambda item: abs(item[1] - item[2]))
        return Affinity(
            user_id=right_id,
            overlap=len(pairs),
            percent=agreement_percent([(left, right) for _title, left, right in pairs]),
            agreements=[(title, left, right) for title, left, right in ranked[:3] if abs(left - right) <= 1],
            disagreements=[
                (title, left, right) for title, left, right in reversed(ranked[-3:]) if abs(left - right) >= 1
            ],
        )

    # ------------------------------------------------------------------
    # Persistance médias / critiques
    # ------------------------------------------------------------------

    async def upsert_media(self, guild: discord.Guild, hit: MediaHit) -> int:
        db = self.data.get(guild)
        await db.execute(
            """INSERT INTO media (source, source_id, media_type, title, subtitle, year, poster_url, url, overview, genres, extra_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, source_id, media_type) DO UPDATE SET
                 title=excluded.title,
                 subtitle=excluded.subtitle,
                 year=excluded.year,
                 poster_url=excluded.poster_url,
                 url=excluded.url,
                 overview=excluded.overview,
                 genres=excluded.genres,
                 extra_json=excluded.extra_json""",
            hit.source,
            hit.source_id,
            hit.media_type,
            hit.title,
            hit.subtitle,
            hit.year,
            hit.poster_url,
            hit.url,
            hit.overview,
            "|".join(hit.genres),
            json.dumps(hit.extra, ensure_ascii=False),
        )
        row = await db.fetchone(
            "SELECT id FROM media WHERE source=? AND source_id=? AND media_type=?",
            hit.source,
            hit.source_id,
            hit.media_type,
        )
        assert row is not None
        return int(row["id"])

    async def lookup_media_id(self, guild: discord.Guild, hit: MediaHit) -> int | None:
        row = await self.data.get(guild).fetchone(
            "SELECT id FROM media WHERE source=? AND source_id=? AND media_type=?",
            hit.source,
            hit.source_id,
            hit.media_type,
        )
        return int(row["id"]) if row else None

    async def media_stats(self, guild: discord.Guild, media_id: int | None) -> tuple[float | None, int]:
        if not media_id:
            return None, 0
        row = await self.data.get(guild).fetchone(
            "SELECT AVG(rating) AS avg_rating, COUNT(*) AS n FROM reviews WHERE media_id=?",
            media_id,
        )
        if not row or not row["n"]:
            return None, 0
        return float(row["avg_rating"]), int(row["n"])

    async def list_reviews(self, guild: discord.Guild, media_id: int) -> list[Any]:
        await self._ensure_schema(guild)
        return await self.data.get(guild).fetchall(
            "SELECT * FROM reviews WHERE media_id=? ORDER BY updated_at DESC",
            media_id,
        )

    async def get_review(self, guild: discord.Guild, user_id: int, media_id: int) -> dict | None:
        await self._ensure_schema(guild)
        row = await self.data.get(guild).fetchone(
            "SELECT * FROM reviews WHERE user_id=? AND media_id=?",
            user_id,
            media_id,
        )
        if row is None:
            return None
        return {
            "rating": float(row["rating"]),
            "comment": row["comment"] or "",
            "updated_at": row["updated_at"],
            "experienced_at": experienced_from_row(row),
        }

    async def upsert_review(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        hit: MediaHit,
        rating: float,
        comment: str,
        experienced_at: str = "",
    ) -> tuple[bool, XpAward]:
        await self._ensure_schema(guild)
        max_len = await self.get_comment_max(guild)
        comment = comment.strip()[:max_len]
        media_id = await self.upsert_media(guild, hit)
        existing = await self.get_review(guild, user.id, media_id)
        count_row = await self.data.get(guild).fetchone(
            "SELECT COUNT(*) AS n FROM reviews WHERE media_id=?", media_id
        )
        pioneer = int(count_row["n"] if count_row else 0) == 0
        new_comment = bool(comment) and (not existing or not existing["comment"])
        now = int(time.time())
        created = existing is None
        if existing:
            await self.data.get(guild).execute(
                "UPDATE reviews SET rating=?, comment=?, experienced_at=?, updated_at=? WHERE user_id=? AND media_id=?",
                rating,
                comment,
                experienced_at,
                now,
                user.id,
                media_id,
            )
        else:
            await self.data.get(guild).execute(
                "INSERT INTO reviews (user_id, media_id, rating, comment, experienced_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                user.id,
                media_id,
                rating,
                comment,
                experienced_at,
                now,
                now,
            )
        award = await self.grant_review_xp(
            guild,
            user.id,
            created=created,
            pioneer=pioneer,
            new_comment=new_comment,
        )
        return created, award

    async def delete_review(self, guild: discord.Guild, user_id: int, hit: MediaHit) -> None:
        media_id = await self.lookup_media_id(guild, hit)
        if media_id:
            await self.data.get(guild).execute(
                "DELETE FROM reviews WHERE user_id=? AND media_id=?",
                user_id,
                media_id,
            )

    async def announce_review(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        hit: MediaHit,
        rating: float,
        comment: str,
        *,
        updated: bool,
        experienced_at: str = "",
    ) -> None:
        channel = await self.get_announce_channel(guild)
        if channel is None:
            return
        _name, avatar = _user_display(guild, self.bot, user.id)
        titles = await self.get_titles(guild, [user.id])
        view = build_announce_view(
            hit,
            mention=_mention(guild, self.bot, user.id),
            title=titles.get(user.id, title_for_level(1)),
            avatar_url=avatar,
            rating=rating,
            comment=comment,
            updated=updated,
            experienced_at=experienced_at,
        )
        try:
            await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            logger.error("Impossible d'annoncer une critique sur %s : %s", guild.name, exc)

    async def _search_or_reply(
        self,
        interaction: discord.Interaction,
        query: str,
        media_type: str,
    ) -> list[MediaHit] | None:
        spec = parse_search_query(query)
        if not spec.lookup_id and len(spec.query.strip()) < 2:
            await interaction.edit_original_response(content="**Erreur ·** La recherche doit contenir au moins 2 caractères.")
            return None
        if self.catalog is None:
            await interaction.edit_original_response(content="**Erreur ·** Catalogue média indisponible.")
            return None
        wants_tmdb = spec.source in (None, "tmdb") and (
            spec.lookup_id or media_type in ("all", "movie", "tv") or spec.media_type in ("movie", "tv")
        )
        if wants_tmdb and spec.source == "tmdb" and not self.catalog.tmdb.available:
            await interaction.edit_original_response(content="**Erreur ·** Clé TMDB manquante (`TMDB_API_KEY` dans `.env`).")
            return None
        if media_type in ("movie", "tv") and spec.source is None and not self.catalog.tmdb.available:
            await interaction.edit_original_response(content="**Erreur ·** Clé TMDB manquante (`TMDB_API_KEY` dans `.env`).")
            return None
        wants_spotify = spec.source == "spotify" or (spec.source is None and media_type in ("album", "track"))
        if wants_spotify and not self.catalog.spotify.available:
            await interaction.edit_original_response(
                content="**Erreur ·** Clés Spotify manquantes (`SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` dans `.env`)."
            )
            return None
        hits = await self.catalog.search(query.strip(), media_type)
        if not hits:
            await interaction.edit_original_response(
                content=f"**Erreur ·** Aucun résultat pour « {pretty.shorten_text(query, 80)} »."
            )
            return None
        return hits

    # ==================================================================
    # Commandes
    # ==================================================================

    critique_group = app_commands.Group(
        name="critique",
        description="Noter et explorer les œuvres du serveur",
        guild_only=True,
    )

    @critique_group.command(name="note")
    @app_commands.rename(query="recherche", media_type="type", rating="note", comment="commentaire")
    @app_commands.describe(
        query="Titre, année, ou préfixe (tmdb:Dune, tmdb:27205, URL TMDB)",
        media_type="Restreindre la recherche à un type de média",
        rating="Note de 0 à 5 (demies étoiles autorisées)",
        comment="Court commentaire optionnel",
    )
    @app_commands.choices(media_type=TYPE_CHOICES, rating=RATING_CHOICES)
    async def critique_note(
        self,
        interaction: discord.Interaction,
        query: str,
        media_type: str = "all",
        rating: float | None = None,
        comment: str | None = None,
    ) -> None:
        """Recherche une œuvre et enregistre (ou prépare) ta note."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        hits = await self._search_or_reply(interaction, query, media_type)
        if not hits:
            return
        view = MediaSessionView(
            self,
            guild,
            hits,
            author_id=interaction.user.id,
            ephemeral=True,
            pending_rating=rating,
            pending_comment=(comment or "").strip(),
        )
        await view.start(interaction, deferred=True)

    @critique_group.command(name="profil")
    @app_commands.rename(member="membre")
    @app_commands.describe(member="Membre dont afficher le profil, le journal et les affinités")
    async def critique_profil(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        """Profil d'un membre : préférées, journal et affinités."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        target = member or interaction.user
        await interaction.response.defer(ephemeral=True)
        await self.ensure_progress(guild)
        xp = await self.get_profile_xp(guild, target.id)
        journal_entries = await self.load_journal(guild, target.id)
        review_count = len(journal_entries)
        average = (
            sum(float(row["rating"]) for _hit, row in journal_entries) / review_count
            if journal_entries else None
        )
        affinities = await self.list_affinities(guild, target.id)
        twin = affinities[0] if affinities else None
        rival = min(affinities, key=lambda item: (item.percent, -item.overlap)) if affinities else None
        title_ids = [target.id, *[item.user_id for item in affinities]]
        view = ProfileView(
            self,
            guild,
            target,
            xp=xp,
            review_count=review_count,
            average=average,
            twin=twin,
            rival=rival,
            titles=await self.get_titles(guild, title_ids),
            favorites=await self.get_favorites(guild, target.id),
            journal_entries=journal_entries,
            affinities=affinities,
            viewer_id=interaction.user.id,
        )
        view._interaction = interaction
        await interaction.edit_original_response(view=view)

    @critique_group.command(name="search")
    @app_commands.rename(query="recherche", member="membre", media_type="type", min_rating="note_min")
    @app_commands.describe(
        query="Filtrer le catalogue déjà noté du serveur",
        member="Limiter aux notes d'un membre",
        media_type="Filtrer par type",
        min_rating="Note minimale (sur l'œuvre ou la critique)",
    )
    @app_commands.choices(media_type=TYPE_CHOICES, min_rating=RATING_CHOICES)
    async def critique_search(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        member: discord.Member | None = None,
        media_type: str = "all",
        min_rating: float | None = None,
    ) -> None:
        """Explore le serveur : récentes, catalogue et top."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        recent = await self.load_recent(guild)
        catalog = await self.load_catalog(
            guild,
            query=query,
            member_id=member.id if member else None,
            media_type=media_type,
            min_rating=min_rating,
        )
        top = await self.load_top(guild, media_type=media_type)
        titles = await self.get_titles(guild, [int(row["user_id"]) for _hit, row in recent])
        subtitle_parts = []
        if query:
            subtitle_parts.append(f"« {pretty.shorten_text(query, 60)} »")
        if member:
            subtitle_parts.append(member.display_name)
        if media_type != "all":
            subtitle_parts.append(type_label(media_type))
        if min_rating is not None:
            subtitle_parts.append(f"≥ {min_rating:g}/5")
        filtered = bool(query or member or min_rating is not None or media_type != "all")
        view = ServerHubView(
            self,
            guild,
            recent=recent,
            catalog=catalog,
            top=top,
            titles=titles,
            catalog_subtitle="  ·  ".join(subtitle_parts) or "Toutes les œuvres notées",
            media_type=media_type,
            tab="catalogue" if filtered else "recentes",
        )
        view._interaction = interaction
        await interaction.edit_original_response(view=view)

    @app_commands.command(name="critiqueconfig")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def critique_config(self, interaction: discord.Interaction) -> None:
        """Ouvre le panneau de configuration des critiques (annonces, commentaires)."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        review_count, media_count = await self.counts(guild)
        view = ReviewsConfigView(
            self,
            guild,
            announce_channel=await self.get_announce_channel(guild),
            comment_max=await self.get_comment_max(guild),
            review_count=review_count,
            media_count=media_count,
            api_status=self.catalog.status() if self.catalog else {},
        )
        await view.start(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reviews(bot))
