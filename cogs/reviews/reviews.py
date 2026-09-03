"""Cog Critiques — carnet de notes type Senscritique / Letterboxd, par serveur."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

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
from .providers import MediaCatalog, MediaHit
from utils import dataio, fuzzy, pretty

logger = logging.getLogger("ACK.Reviews")

NO_PINGS = discord.AllowedMentions.none()

VALID_RATINGS = tuple(i / 2 for i in range(11))
DEFAULT_COMMENT_MAX = 280
MIN_COMMENT_MAX = 50
MAX_COMMENT_MAX = 500
JOURNAL_PAGE = 5
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
    """Une seule étoile + note, pour labels courts (Select, slash, boutons)."""
    if rating <= 0:
        icon = STAR_EMPTY
    elif rating % 1 >= 0.45:
        icon = STAR_HALF
    else:
        icon = STAR
    return f"{icon} {rating:g}"


RATING_CHOICES = [
    app_commands.Choice(name=f"{format_stars_compact(r)}/5", value=r)
    for r in VALID_RATINGS
]


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
    if hit.subtitle and hit.media_type in ("track", "album", "book", "game"):
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


def _fiche_body(
    hit: MediaHit,
    *,
    avg: float | None,
    count: int,
    my_review: dict | None,
    social_line: str = "",
) -> str:
    lines: list[str] = []
    official = _official_line(hit)
    if official:
        lines.append(official)
    if count:
        stars = format_stars(avg or 0)
        lines.append(f"Serveur · {stars}  **{(avg or 0):.1f}/5**  ·  {count} critique{'s' if count > 1 else ''}")
    else:
        lines.append("*Aucune note sur ce serveur pour l'instant.*")
    if my_review:
        comment = pretty.shorten_text(my_review["comment"], 180) if my_review["comment"] else ""
        mine = f"Ta note · {format_stars(my_review['rating'])}  **{my_review['rating']:g}/5**"
        if comment:
            mine += f"\n*{comment}*"
        lines.append(mine)
    if social_line:
        lines.append(f"-# {social_line}")
    price = _price_line(hit)
    if price:
        lines.append(price)
    overview = pretty.shorten_text(hit.overview, 380) if hit.overview else ""
    if overview:
        lines.append(overview)
    elif not official and not count:
        lines.append("-# Aucune description disponible.")
    return "\n".join(lines)


def add_fiche_header(container: discord.ui.Container, hit: MediaHit) -> None:
    container.add_item(discord.ui.TextDisplay(f"{_title_line(hit)}\n-# {_meta_line(hit)}"))
    container.add_item(discord.ui.Separator())


def add_backdrop(container: discord.ui.Container, hit: MediaHit) -> None:
    backdrop = hit.extra.get("backdrop_url")
    if not backdrop:
        return
    try:
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(backdrop)))
    except Exception:
        pass


def _link_label(hit: MediaHit) -> str:
    return {
        "tmdb": "TMDB",
        "steam": "Steam",
        "spotify": "Spotify",
        "openlibrary": "Open Library",
    }.get(hit.source, "Fiche")


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
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()
    verb = "a mis à jour sa note" if updated else "a noté"
    header = f"{_titled(mention, title)}\n{verb}\n{_title_line(hit)}\n{format_stars(rating)}  **{rating:g}/5**"
    if comment:
        header += f"\n*{pretty.shorten_text(comment, 240)}*"
    container.add_item(section_with_thumbnail(header, avatar_url or hit.poster_url))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(f"-# {_meta_line(hit)}" + (f"  ·  [{_link_label(hit)}]({hit.url})" if hit.url else "")))
    view.add_item(container)
    return view


# ---------------------------------------------------------------------------
# Modal de notation
# ---------------------------------------------------------------------------

class RateModal(discord.ui.Modal, title="Noter cette œuvre"):
    def __init__(self, parent: "MediaSessionView", *, max_comment: int, default_rating: float | None, default_comment: str):
        super().__init__()
        self._parent = parent
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
        self.add_item(self.rating_input)
        self.add_item(self.comment_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rating = parse_rating(self.rating_input.value)
        if rating is None:
            await interaction.response.send_message(
                "**Erreur ·** La note doit être comprise entre 0 et 5 (demies étoiles acceptées, ex. `3.5`).",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self._parent.save_review(interaction, rating, str(self.comment_input.value or "").strip())


# ---------------------------------------------------------------------------
# Vue session (sélection + fiche + critiques)
# ---------------------------------------------------------------------------

class MediaSelect(discord.ui.Select):
    def __init__(self, parent: "MediaSessionView", hits: list[MediaHit], selected: int):
        options = []
        for index, hit in enumerate(hits[:25]):
            year = f"{hit.year}" if hit.year else "—"
            desc_parts = [type_label(hit.media_type), year]
            if hit.subtitle:
                desc_parts.append(hit.subtitle)
            options.append(
                discord.SelectOption(
                    label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                    value=str(index),
                    description=pretty.shorten_text(" · ".join(desc_parts), 95),
                    emoji=select_emoji(hit.media_type),
                    default=index == selected,
                )
            )
        super().__init__(placeholder="Choisir une œuvre", options=options, min_values=1, max_values=1)
        self._parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._parent.selected = int(self.values[0])
        await self._parent.enrich_selected()
        await self._parent.refresh()


class TabButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView", tab: str, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary if parent.tab == tab else discord.ButtonStyle.secondary,
        )
        self._parent = parent
        self._tab = tab

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._parent.tab = self._tab
        self._parent.review_page = 0
        await self._parent.refresh()


class RateButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        super().__init__(label="Noter", style=discord.ButtonStyle.green)
        self._parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._parent
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
            )
        )


class DeleteReviewButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        super().__init__(label="Supprimer ma note", style=discord.ButtonStyle.red)
        self._parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._parent.cog.delete_review(self._parent.guild, interaction.user.id, self._parent.hit)
        self._parent.my_review = None
        await self._parent.reload_stats()
        await self._parent.refresh()
        await interaction.followup.send("**Critique supprimée ·** Ta note a été retirée.", ephemeral=True)


class PublishFicheButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        super().__init__(label="Publier la fiche", style=discord.ButtonStyle.secondary)
        self._parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = MediaSessionView(
            self._parent.cog,
            self._parent.guild,
            [self._parent.hit],
            author_id=interaction.user.id,
            ephemeral=False,
        )
        await view.prepare()
        view._interaction = interaction
        view._message = await interaction.followup.send(view=view, allowed_mentions=NO_PINGS)


class PageButton(discord.ui.Button):
    def __init__(self, parent: "PagedView", delta: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._parent = parent
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._parent.page = max(0, min(self._parent.max_page, self._parent.page + self._delta))
        await self._parent.refresh()


class MediaSessionView(discord.ui.LayoutView):
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

    async def save_review(self, interaction: discord.Interaction, rating: float, comment: str) -> None:
        created, award = await self.cog.upsert_review(self.guild, interaction.user, self.hit, rating, comment)
        await self.reload_stats()
        self.pending_rating = None
        self.tab = "fiche"
        await self.refresh()
        verb = "enregistrée" if created else "mise à jour"
        parts = [f"**Critique {verb} ·** {format_stars(rating)}  **{rating:g}/5** — {self.hit.title}."]
        if award.gained:
            parts.append(f"{XP} +{award.gained} · niveau {award.level}")
            if award.capped:
                parts.append("(plafond quotidien atteint)")
        elif award.capped:
            parts.append("Plafond d'XP quotidien atteint.")
        if award.leveled_up:
            new_title = title_for_level(award.level)
            old_title = title_for_level(award.previous_level)
            if new_title != old_title:
                parts.append(f"Nouveau titre · {new_title}")
            else:
                parts.append(f"Niveau {award.level}")
        await interaction.followup.send("\n".join(parts), ephemeral=True)
        await self.cog.announce_review(self.guild, interaction.user, self.hit, rating, comment, updated=not created)

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container()
        hit = self.hit

        if len(self.hits) > 1:
            container.add_item(discord.ui.TextDisplay(f"### Résultats · {len(self.hits)} œuvre(s)"))
            row = discord.ui.ActionRow()
            row.add_item(MediaSelect(self, self.hits, self.selected))
            container.add_item(row)
            container.add_item(discord.ui.Separator())

        if self.tab == "fiche":
            add_fiche_header(container, hit)
            add_backdrop(container, hit)
            container.add_item(section_with_thumbnail(
                _fiche_body(
                    hit,
                    avg=self.avg,
                    count=self.count,
                    my_review=self.my_review if self.ephemeral else None,
                    social_line=self.social_line,
                ),
                hit.poster_url,
            ))
            footer = _footer_line(hit)
            if footer:
                container.add_item(discord.ui.Separator())
                container.add_item(discord.ui.TextDisplay(f"-# {footer}"))
        else:
            add_fiche_header(container, hit)
            if not self.reviews:
                container.add_item(discord.ui.TextDisplay("*Pas encore de critique sur ce serveur.*"))
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
                    container.add_item(section_with_thumbnail(text, avatar))
                total_pages = max(1, (len(self.reviews) + REVIEWS_PAGE - 1) // REVIEWS_PAGE)
                container.add_item(discord.ui.TextDisplay(
                    f"-# {self.count} critique(s) · moyenne {format_stars(self.avg or 0)} {(self.avg or 0):.1f}/5 · page {self.review_page + 1}/{total_pages}"
                ))

        tabs = discord.ui.ActionRow()
        tabs.add_item(TabButton(self, "fiche", "Fiche"))
        tabs.add_item(TabButton(self, "critiques", f"Critiques ({self.count})"))
        container.add_item(tabs)

        actions = discord.ui.ActionRow()
        rate_label = "Noter"
        if self.ephemeral and self.pending_rating is not None and self.my_review is None:
            rate_label = f"Noter {format_stars_compact(self.pending_rating)}"
        elif self.ephemeral and self.my_review:
            rate_label = "Modifier ma note"
        rate_btn = RateButton(self)
        rate_btn.label = rate_label
        actions.add_item(rate_btn)
        if self.ephemeral and self.my_review:
            actions.add_item(DeleteReviewButton(self))
        if self.ephemeral:
            actions.add_item(PublishFicheButton(self))
        if hit.url:
            actions.add_item(discord.ui.Button(label=_link_label(hit), url=hit.url, style=discord.ButtonStyle.link))
        container.add_item(actions)

        if self.tab == "critiques" and len(self.reviews) > REVIEWS_PAGE:
            nav = discord.ui.ActionRow()
            if self.review_page > 0:
                nav.add_item(_ReviewPageButton(self, -1, "← Précédent"))
            if (self.review_page + 1) * REVIEWS_PAGE < len(self.reviews):
                nav.add_item(_ReviewPageButton(self, 1, "Suivant →"))
            container.add_item(nav)

        self.add_item(container)

    async def refresh(self) -> None:
        await self.reload_stats()
        self._build()
        try:
            if self._message is not None:
                await self._message.edit(view=self, allowed_mentions=NO_PINGS)
            elif self._interaction:
                await self._interaction.edit_original_response(view=self, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            logger.warning("Impossible de rafraîchir la fiche « %s »", self.hit.title)

    async def start(self, interaction: discord.Interaction, *, deferred: bool = False) -> None:
        self._interaction = interaction
        await self.prepare()
        if deferred:
            await interaction.edit_original_response(view=self, allowed_mentions=NO_PINGS)
        else:
            await interaction.response.send_message(view=self, ephemeral=self.ephemeral, allowed_mentions=NO_PINGS)


class _ReviewPageButton(discord.ui.Button):
    def __init__(self, parent: MediaSessionView, delta: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._parent = parent
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._parent.review_page = max(0, self._parent.review_page + self._delta)
        await self._parent.refresh()


# ---------------------------------------------------------------------------
# Listes paginées (journal, catalogue, récentes)
# ---------------------------------------------------------------------------

class PagedView(discord.ui.LayoutView):
    def __init__(self, cog: "Reviews", guild: discord.Guild, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.page = 0
        self._interaction: discord.Interaction | None = None

    @property
    def max_page(self) -> int:
        return 0

    async def refresh(self) -> None:
        self._build()
        if self._interaction:
            await self._interaction.edit_original_response(view=self, allowed_mentions=NO_PINGS)

    def _build(self) -> None:
        raise NotImplementedError

    def _nav_row(self) -> discord.ui.ActionRow | None:
        if self.max_page <= 0:
            return None
        row = discord.ui.ActionRow()
        prev_btn = PageButton(self, -1, "← Précédent")
        next_btn = PageButton(self, 1, "Suivant →")
        prev_btn.disabled = self.page <= 0
        next_btn.disabled = self.page >= self.max_page
        row.add_item(prev_btn)
        row.add_item(next_btn)
        return row

    async def start(self, interaction: discord.Interaction, *, deferred: bool = False, ephemeral: bool = False) -> None:
        self._interaction = interaction
        self._build()
        if deferred:
            await interaction.edit_original_response(view=self, allowed_mentions=NO_PINGS)
        else:
            await interaction.response.send_message(view=self, ephemeral=ephemeral, allowed_mentions=NO_PINGS)


class JournalOpenSelect(discord.ui.Select):
    def __init__(self, parent: "JournalView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(f"{format_stars_compact(row['rating'])}  {hit.title}", 95),
                value=str(index),
                description=pretty.shorten_text(f"{type_label(hit.media_type)} · {hit.year or '—'} · {row['rating']:g}/5", 95),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._parent = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _row = self._items[int(self.values[0])]
        await interaction.response.defer()
        view = MediaSessionView(self._parent.cog, self._parent.guild, [hit], author_id=interaction.user.id, ephemeral=False)
        await view.prepare()
        view._interaction = interaction
        view._message = await interaction.followup.send(view=view, allowed_mentions=NO_PINGS)


class JournalView(PagedView):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        member: discord.Member | discord.User,
        entries: list[tuple[MediaHit, Any]],
        *,
        average: float | None,
        title: str,
    ):
        super().__init__(cog, guild)
        self.member = member
        self.entries = entries
        self.average = average
        self.title = title

    @property
    def max_page(self) -> int:
        return max(0, (len(self.entries) - 1) // JOURNAL_PAGE)

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container()
        stats = f"{len(self.entries)} note{'s' if len(self.entries) != 1 else ''}"
        if self.average is not None:
            stats += f"  ·  moyenne {format_stars(self.average)} {self.average:.1f}/5"
        types = {}
        for hit, _ in self.entries:
            types[hit.media_type] = types.get(hit.media_type, 0) + 1
        if types:
            top_type = max(types, key=types.get)
            stats += f"  ·  {types[top_type]} {type_label(top_type).lower()}{'s' if types[top_type] > 1 else ''}"
        header = f"{_titled(_mention(self.guild, self.cog.bot, self.member.id), self.title)}\n-# {stats}"
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())

        if not self.entries:
            container.add_item(discord.ui.TextDisplay("*Aucune œuvre notée pour l'instant.*"))
        else:
            start = self.page * JOURNAL_PAGE
            page_items = self.entries[start:start + JOURNAL_PAGE]
            for hit, row in page_items:
                year = f" ({hit.year})" if hit.year else ""
                text = f"{format_stars(row['rating'])}  **{hit.title}**{year}\n-# {type_label(hit.media_type)}"
                if row["comment"]:
                    text += f"\n{pretty.shorten_text(row['comment'], 180)}"
                container.add_item(section_with_thumbnail(text, hit.poster_url))
            container.add_item(discord.ui.TextDisplay(f"-# Page {self.page + 1}/{self.max_page + 1}"))
            select_row = discord.ui.ActionRow()
            select_row.add_item(JournalOpenSelect(self, page_items))
            container.add_item(select_row)

        nav = self._nav_row()
        if nav:
            container.add_item(nav)
        self.add_item(container)


class CatalogOpenSelect(discord.ui.Select):
    def __init__(self, parent: "CatalogView", page_items: list[tuple[MediaHit, float, int]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95),
                value=str(index),
                description=pretty.shorten_text(
                    f"{type_label(hit.media_type)} · {format_stars_compact(avg)}/5 · {count} note{'s' if count > 1 else ''}",
                    95,
                ),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, avg, count) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._parent = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _avg, _count = self._items[int(self.values[0])]
        await interaction.response.defer()
        view = MediaSessionView(self._parent.cog, self._parent.guild, [hit], author_id=interaction.user.id, ephemeral=False)
        await view.prepare()
        view._interaction = interaction
        view._message = await interaction.followup.send(view=view, allowed_mentions=NO_PINGS)


class CatalogView(PagedView):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        items: list[tuple[MediaHit, float, int]],
        *,
        title: str,
        subtitle: str,
    ):
        super().__init__(cog, guild)
        self.items = items
        self.title = title
        self.subtitle = subtitle

    @property
    def max_page(self) -> int:
        return max(0, (len(self.items) - 1) // CATALOG_PAGE)

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## {self.title}\n-# {self.subtitle}"))
        container.add_item(discord.ui.Separator())
        if not self.items:
            container.add_item(discord.ui.TextDisplay("*Aucune œuvre ne correspond à cette recherche.*"))
        else:
            start = self.page * CATALOG_PAGE
            page_items = self.items[start:start + CATALOG_PAGE]
            lines = []
            for index, (hit, avg, count) in enumerate(page_items, start=start + 1):
                year = f" ({hit.year})" if hit.year else ""
                lines.append(
                    f"**{index}.** {format_stars(avg)}  **{hit.title}**{year}  ·  {type_label(hit.media_type)}  ·  {count} note{'s' if count > 1 else ''}"
                )
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))
            select_row = discord.ui.ActionRow()
            select_row.add_item(CatalogOpenSelect(self, page_items))
            container.add_item(select_row)
            if self.max_page > 0:
                container.add_item(discord.ui.TextDisplay(f"-# Page {self.page + 1}/{self.max_page + 1}"))
        nav = self._nav_row()
        if nav:
            container.add_item(nav)
        self.add_item(container)


class RecentOpenSelect(discord.ui.Select):
    def __init__(self, parent: "RecentView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95),
                value=str(index),
                description=pretty.shorten_text(f"{format_stars_compact(row['rating'])}/5 · {type_label(hit.media_type)}", 95),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._parent = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _row = self._items[int(self.values[0])]
        await interaction.response.defer()
        view = MediaSessionView(self._parent.cog, self._parent.guild, [hit], author_id=interaction.user.id, ephemeral=False)
        await view.prepare()
        view._interaction = interaction
        view._message = await interaction.followup.send(view=view, allowed_mentions=NO_PINGS)


class RecentView(PagedView):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        entries: list[tuple[MediaHit, Any]],
        *,
        titles: dict[int, str],
    ):
        super().__init__(cog, guild)
        self.entries = entries
        self.titles = titles

    @property
    def max_page(self) -> int:
        return max(0, (len(self.entries) - 1) // JOURNAL_PAGE)

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## Dernières critiques\n-# {len(self.entries)} récente(s) sur ce serveur"))
        container.add_item(discord.ui.Separator())
        if not self.entries:
            container.add_item(discord.ui.TextDisplay("*Personne n'a encore noté d'œuvre ici.*"))
        else:
            start = self.page * JOURNAL_PAGE
            page_items = self.entries[start:start + JOURNAL_PAGE]
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
                container.add_item(section_with_thumbnail(text, avatar or hit.poster_url))
            select_row = discord.ui.ActionRow()
            select_row.add_item(RecentOpenSelect(self, page_items))
            container.add_item(select_row)
        nav = self._nav_row()
        if nav:
            container.add_item(nav)
        self.add_item(container)


# ---------------------------------------------------------------------------
# Affinités & profil
# ---------------------------------------------------------------------------

class AffinityCompareSelect(discord.ui.Select):
    def __init__(self, parent: "AffinityHubView", affinities: list[Affinity]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(parent._name(item.user_id), 95),
                value=str(item.user_id),
                description=pretty.shorten_text(
                    f"{item.percent:.0f} % · {item.overlap} œuvre{'s' if item.overlap > 1 else ''} en commun",
                    95,
                ),
            )
            for item in affinities[:25]
        ]
        super().__init__(placeholder="Comparer avec…", options=options)
        self._parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        other_id = int(self.values[0])
        view = AffinityCompareView(
            self._parent.cog,
            self._parent.guild,
            self._parent.member_id,
            other_id,
            affinity=next(a for a in self._parent.affinities if a.user_id == other_id),
            titles=self._parent.titles,
        )
        view._interaction = interaction
        view._message = await interaction.followup.send(view=view, allowed_mentions=NO_PINGS)


class AffinityHubView(PagedView):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        member_id: int,
        affinities: list[Affinity],
        *,
        titles: dict[int, str],
    ):
        super().__init__(cog, guild)
        self.member_id = member_id
        self.affinities = sorted(affinities, key=lambda a: (-a.percent, -a.overlap))
        self.titles = titles

    def _name(self, user_id: int) -> str:
        name, _avatar = _user_display(self.guild, self.cog.bot, user_id)
        return name

    def _person(self, user_id: int) -> str:
        return _titled(
            _mention(self.guild, self.cog.bot, user_id),
            self.titles.get(user_id, title_for_level(1)),
        )

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container()
        _me, avatar = _user_display(self.guild, self.cog.bot, self.member_id)
        if not self.affinities:
            container.add_item(section_with_thumbnail(
                f"{self._person(self.member_id)}\n*Pas encore assez d'œuvres en commun avec quelqu'un "
                f"(minimum {MIN_AFFINITY_OVERLAP}). Notez les mêmes films, jeux ou albums.*",
                avatar,
            ))
            self.add_item(container)
            return

        twins = self.affinities[:3]
        rival = min(self.affinities, key=lambda a: (a.percent, -a.overlap))
        lines = [
            self._person(self.member_id),
            f"-# {len(self.affinities)} affinité(s) · min. {MIN_AFFINITY_OVERLAP} œuvres en commun",
            "",
            f"{TWIN} **Jumeaux**",
        ]
        for twin in twins:
            lines.append(self._person(twin.user_id))
            lines.append(f"-# {twin.percent:.0f} % · {twin.overlap} en commun")
        if rival.user_id not in {t.user_id for t in twins}:
            lines.append("")
            lines.append(f"{RIVAL} **Rival**")
            lines.append(self._person(rival.user_id))
            lines.append(f"-# {rival.percent:.0f} %")
        container.add_item(section_with_thumbnail("\n".join(lines), avatar))
        select_row = discord.ui.ActionRow()
        select_row.add_item(AffinityCompareSelect(self, self.affinities))
        container.add_item(select_row)
        self.add_item(container)


class AffinityCompareView(discord.ui.LayoutView):
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
        self.clear_items()
        container = discord.ui.Container()
        _left_name, left_avatar = _user_display(self.guild, self.cog.bot, self.left_id)
        header = (
            f"{self._person(self.left_id)}\n{self._person(self.right_id)}\n"
            f"-# {self.affinity.percent:.0f} % d'accord  ·  {self.affinity.overlap} œuvre(s) en commun"
        )
        container.add_item(section_with_thumbnail(header, left_avatar))
        container.add_item(discord.ui.Separator())

        if self.affinity.agreements:
            lines = ["**D'accord**"]
            for title, left, right in self.affinity.agreements:
                lines.append(f"{format_stars_compact(left)} / {format_stars_compact(right)}  ·  {title}")
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        if self.affinity.disagreements:
            lines = ["**Désaccord**"]
            for title, left, right in self.affinity.disagreements:
                lines.append(f"{format_stars_compact(left)} / {format_stars_compact(right)}  ·  {title}")
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        self.add_item(container)


class ProfileView(discord.ui.LayoutView):
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
        self._build()

    def _person(self, user_id: int) -> str:
        return _titled(
            _mention(self.guild, self.cog.bot, user_id),
            self.titles.get(user_id, title_for_level(1)),
        )

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container()
        level, into, need, total = level_progress(self.xp)
        stats = f"{self.review_count} note{'s' if self.review_count != 1 else ''}"
        if self.average is not None:
            stats += f"  ·  moyenne {self.average:.1f}/5"
        title = title_for_level(level)
        body = [
            _titled(_mention(self.guild, self.cog.bot, self.member.id), title),
            f"{XP} {total} XP · niveau {level} · {into}/{need} vers le niveau {level + 1}",
            f"-# {stats}",
        ]
        if self.twin:
            body.append(f"{TWIN} Jumeau")
            body.append(self._person(self.twin.user_id))
            body.append(f"-# {self.twin.percent:.0f} % d'accord")
        if self.rival and (not self.twin or self.rival.user_id != self.twin.user_id):
            body.append(f"{RIVAL} Rival")
            body.append(self._person(self.rival.user_id))
            body.append(f"-# {self.rival.percent:.0f} % d'accord")
        avatar = self.member.display_avatar.url if hasattr(self.member, "display_avatar") else None
        container.add_item(section_with_thumbnail("\n".join(body), avatar))
        self.add_item(container)


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
        await self._view_ref.refresh()


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
            await self._view_ref.refresh()
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
        await self._view_ref.refresh()


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
            await self._view_ref.refresh()
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
        await self._view_ref.refresh()


class ReviewsConfigView(discord.ui.LayoutView):
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
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## Configuration des critiques — {self.guild.name}"))
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.Section(
                f"**Salon d'annonce**\n{self.announce_channel.mention if self.announce_channel else '*Non configuré*'}",
                accessory=ToggleAnnounceButton(self),
            )
        )
        channel_row = discord.ui.ActionRow()
        channel_row.add_item(AnnounceChannelSelect(self))
        container.add_item(channel_row)
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.Section(
                f"**Commentaire max.**\n{self.comment_max} caractères",
                accessory=EditCommentMaxButton(self),
            )
        )
        container.add_item(discord.ui.Separator())
        apis = "  ·  ".join(f"{name} {'ok' if ok else 'manquant'}" for name, ok in self.api_status.items())
        container.add_item(discord.ui.TextDisplay(
            f"-# {self.review_count} critique(s) · {self.media_count} œuvre(s)\n-# APIs · {apis}"
        ))
        self.add_item(container)

    async def _reload(self) -> None:
        self.announce_channel = await self.cog.get_announce_channel(self.guild)
        self.comment_max = await self.cog.get_comment_max(self.guild)
        self.review_count, self.media_count = await self.cog.counts(self.guild)
        self.api_status = self.cog.catalog.status()

    async def refresh(self) -> None:
        await self._reload()
        self._build()
        if self._interaction:
            await self._interaction.edit_original_response(view=self)

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
        self.data.link(discord.Guild, settings, media_table, reviews_table, profiles_table)

    async def cog_load(self) -> None:
        config = getattr(self.bot, "config", {}) or {}
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "ACK-BOT/1.0 (Discord reviews)"},
        )
        self.catalog = MediaCatalog(
            self._http,
            tmdb_key=str(config.get("TMDB_API_KEY") or ""),
            spotify_id=str(config.get("SPOTIFY_CLIENT_ID") or ""),
            spotify_secret=str(config.get("SPOTIFY_CLIENT_SECRET") or ""),
        )
        status = self.catalog.status()
        missing = [name for name, ok in status.items() if not ok]
        if missing:
            logger.warning("Fournisseurs incomplets : %s", ", ".join(missing))

    async def cog_unload(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None
        await self.data.close_all()

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
        return await self.data.get(guild).fetchall(
            "SELECT * FROM reviews WHERE media_id=? ORDER BY updated_at DESC",
            media_id,
        )

    async def get_review(self, guild: discord.Guild, user_id: int, media_id: int) -> dict | None:
        row = await self.data.get(guild).fetchone(
            "SELECT * FROM reviews WHERE user_id=? AND media_id=?",
            user_id,
            media_id,
        )
        if row is None:
            return None
        return {"rating": float(row["rating"]), "comment": row["comment"] or "", "updated_at": row["updated_at"]}

    async def upsert_review(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        hit: MediaHit,
        rating: float,
        comment: str,
    ) -> tuple[bool, XpAward]:
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
                "UPDATE reviews SET rating=?, comment=?, updated_at=? WHERE user_id=? AND media_id=?",
                rating,
                comment,
                now,
                user.id,
                media_id,
            )
        else:
            await self.data.get(guild).execute(
                "INSERT INTO reviews (user_id, media_id, rating, comment, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                user.id,
                media_id,
                rating,
                comment,
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
        if len(query.strip()) < 2:
            await interaction.edit_original_response(content="**Erreur ·** La recherche doit contenir au moins 2 caractères.")
            return None
        if self.catalog is None:
            await interaction.edit_original_response(content="**Erreur ·** Catalogue média indisponible.")
            return None
        if media_type in ("movie", "tv") and not self.catalog.tmdb.available:
            await interaction.edit_original_response(content="**Erreur ·** Clé TMDB manquante (`TMDB_API_KEY` dans `.env`).")
            return None
        if media_type in ("album", "track") and not self.catalog.spotify.available:
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
        query="Titre de l'œuvre (ajoute l'année si besoin, ex. Dune 2021)",
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

    @critique_group.command(name="fiche")
    @app_commands.rename(query="recherche", media_type="type")
    @app_commands.describe(query="Titre de l'œuvre", media_type="Type de média")
    @app_commands.choices(media_type=TYPE_CHOICES)
    async def critique_fiche(
        self,
        interaction: discord.Interaction,
        query: str,
        media_type: str = "all",
    ) -> None:
        """Affiche la fiche d'une œuvre et les critiques du serveur."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer()
        hits = await self._search_or_reply(interaction, query, media_type)
        if not hits:
            return
        view = MediaSessionView(self, guild, hits, author_id=interaction.user.id, ephemeral=False)
        await view.start(interaction, deferred=True)

    @critique_group.command(name="journal")
    @app_commands.rename(member="membre", media_type="type")
    @app_commands.describe(member="Membre dont afficher le journal (toi par défaut)", media_type="Filtrer par type")
    @app_commands.choices(media_type=TYPE_CHOICES)
    async def critique_journal(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        media_type: str = "all",
    ) -> None:
        """Journal des notes d'un membre du serveur."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        target = member or interaction.user
        await interaction.response.defer()
        db = self.data.get(guild)
        if media_type == "all":
            rows = await db.fetchall(
                """SELECT r.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                          m.poster_url, m.url, m.overview, m.genres, m.extra_json
                   FROM reviews r JOIN media m ON m.id = r.media_id
                   WHERE r.user_id=?
                   ORDER BY r.updated_at DESC""",
                target.id,
            )
        else:
            rows = await db.fetchall(
                """SELECT r.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                          m.poster_url, m.url, m.overview, m.genres, m.extra_json
                   FROM reviews r JOIN media m ON m.id = r.media_id
                   WHERE r.user_id=? AND m.media_type=?
                   ORDER BY r.updated_at DESC""",
                target.id,
                media_type,
            )
        entries = [(hit_from_row(row), row) for row in rows]
        average = (sum(float(row["rating"]) for _, row in entries) / len(entries)) if entries else None
        xp = await self.get_profile_xp(guild, target.id)
        view = JournalView(
            self,
            guild,
            target,
            entries,
            average=average,
            title=title_for_level(level_for_xp(xp)),
        )
        await view.start(interaction, deferred=True)

    @critique_group.command(name="affinite")
    @app_commands.rename(member="membre")
    @app_commands.describe(member="Quelqu'un à comparer (sinon tes jumeaux et ton rival)")
    async def critique_affinite(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        """Compare tes notes avec celles d'un autre membre, ou liste tes affinités."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer()
        if member is None or member.id == interaction.user.id:
            affinities = await self.list_affinities(guild, interaction.user.id)
            titles = await self.get_titles(
                guild,
                [interaction.user.id, *[item.user_id for item in affinities]],
            )
            view = AffinityHubView(self, guild, interaction.user.id, affinities, titles=titles)
            await view.start(interaction, deferred=True)
            return
        affinity = await self.get_affinity(guild, interaction.user.id, member.id)
        if affinity is None:
            await interaction.edit_original_response(
                content=f"**Info ·** Aucune œuvre en commun avec {member.display_name}."
            )
            return
        if affinity.overlap < MIN_AFFINITY_OVERLAP:
            await interaction.edit_original_response(
                content=(
                    f"**Info ·** Seulement {affinity.overlap} œuvre(s) en commun avec {member.display_name} "
                    f"(minimum {MIN_AFFINITY_OVERLAP} pour un score fiable)."
                )
            )
            return
        titles = await self.get_titles(guild, [interaction.user.id, member.id])
        view = AffinityCompareView(
            self, guild, interaction.user.id, member.id, affinity=affinity, titles=titles
        )
        await interaction.edit_original_response(view=view, allowed_mentions=NO_PINGS)

    @critique_group.command(name="profil")
    @app_commands.rename(member="membre")
    @app_commands.describe(member="Membre dont afficher le profil critique")
    async def critique_profil(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        """Profil critique : titre, XP, jumeau et rival."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        target = member or interaction.user
        await interaction.response.defer()
        await self.ensure_progress(guild)
        xp = await self.get_profile_xp(guild, target.id)
        db = self.data.get(guild)
        stats = await db.fetchone(
            "SELECT COUNT(*) AS n, AVG(rating) AS avg_rating FROM reviews WHERE user_id=?",
            target.id,
        )
        review_count = int(stats["n"]) if stats else 0
        average = float(stats["avg_rating"]) if stats and stats["avg_rating"] is not None else None
        affinities = await self.list_affinities(guild, target.id)
        twin = affinities[0] if affinities else None
        rival = min(affinities, key=lambda item: (item.percent, -item.overlap)) if affinities else None
        title_ids = [target.id]
        if twin:
            title_ids.append(twin.user_id)
        if rival:
            title_ids.append(rival.user_id)
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
        )
        await interaction.edit_original_response(view=view, allowed_mentions=NO_PINGS)

    @critique_group.command(name="search")
    @app_commands.rename(query="recherche", member="membre", media_type="type", min_rating="note_min")
    @app_commands.describe(
        query="Rechercher dans le catalogue déjà noté du serveur",
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
        """Cherche dans la base critique du serveur, ou affiche les dernières notes."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer()
        db = self.data.get(guild)

        if not query and not member and min_rating is None and media_type == "all":
            rows = await db.fetchall(
                """SELECT r.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                          m.poster_url, m.url, m.overview, m.genres, m.extra_json
                   FROM reviews r JOIN media m ON m.id = r.media_id
                   ORDER BY r.updated_at DESC
                   LIMIT 40"""
            )
            entries = [(hit_from_row(row), row) for row in rows]
            titles = await self.get_titles(guild, [int(row["user_id"]) for _hit, row in entries])
            view = RecentView(self, guild, entries, titles=titles)
            await view.start(interaction, deferred=True)
            return

        sql = """SELECT m.*, AVG(r.rating) AS avg_rating, COUNT(r.id) AS n
                 FROM media m JOIN reviews r ON r.media_id = m.id"""
        clauses: list[str] = []
        args: list[Any] = []
        if member:
            clauses.append("r.user_id=?")
            args.append(member.id)
        if media_type != "all":
            clauses.append("m.media_type=?")
            args.append(media_type)
        if min_rating is not None:
            clauses.append("r.rating>=?")
            args.append(min_rating)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY m.id"
        rows = await db.fetchall(sql, *args)
        items = [(hit_from_row(row), float(row["avg_rating"]), int(row["n"])) for row in rows]
        if query:
            items = fuzzy.finder(query, items, key=lambda item: f"{item[0].title} {item[0].subtitle} {item[0].year or ''}")
        subtitle_parts = []
        if query:
            subtitle_parts.append(f"« {pretty.shorten_text(query, 60)} »")
        if member:
            subtitle_parts.append(member.display_name)
        if media_type != "all":
            subtitle_parts.append(type_label(media_type))
        if min_rating is not None:
            subtitle_parts.append(f"≥ {min_rating:g}/5")
        view = CatalogView(
            self,
            guild,
            items,
            title="Catalogue du serveur",
            subtitle="  ·  ".join(subtitle_parts) or "Toutes les œuvres notées",
        )
        await view.start(interaction, deferred=True)

    @critique_group.command(name="top")
    @app_commands.rename(media_type="type", period="periode")
    @app_commands.describe(media_type="Type de média", period="Fenêtre de temps")
    @app_commands.choices(
        media_type=TYPE_CHOICES,
        period=[
            app_commands.Choice(name="Tout", value="all"),
            app_commands.Choice(name="Cette semaine", value="semaine"),
            app_commands.Choice(name="Ce mois", value="mois"),
        ],
    )
    async def critique_top(
        self,
        interaction: discord.Interaction,
        media_type: str = "all",
        period: str = "all",
    ) -> None:
        """Classement des œuvres les mieux notées du serveur."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer()
        db = self.data.get(guild)
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
        rows = await db.fetchall(sql, *args)
        items = [(hit_from_row(row), float(row["avg_rating"]), int(row["n"])) for row in rows]
        period_label = {"all": "toutes périodes", "semaine": "cette semaine", "mois": "ce mois"}.get(period, period)
        type_part = type_label(media_type) if media_type != "all" else "Tous types"
        view = CatalogView(
            self,
            guild,
            items,
            title="Top du serveur",
            subtitle=f"{type_part}  ·  {period_label}",
        )
        await view.start(interaction, deferred=True)

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
