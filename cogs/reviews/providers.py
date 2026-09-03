"""Clients HTTP pour TMDB, Steam, Spotify et Open Library.

Les clés TMDB / Spotify sont lues depuis `.env` (mêmes noms que MARIA_R).
Steam Store et Open Library n'exigent aucune clé.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .emojis import REVIEW_ACCLAIMED, REVIEW_BOMB, REVIEW_DOWN, REVIEW_MIXED, REVIEW_OK, REVIEW_UP

logger = logging.getLogger("ACK.Reviews.Providers")

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w500{}"
TMDB_BACKDROP = "https://image.tmdb.org/t/p/w780{}"

STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/"
STEAM_DETAILS = "https://store.steampowered.com/api/appdetails"
STEAM_HEADER = "https://cdn.akamai.steamstatic.com/steam/apps/{}/header.jpg"
STEAM_STORE = "https://store.steampowered.com/app/{}"

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"

OPENLIB_SEARCH = "https://openlibrary.org/search.json"
OPENLIB_COVER = "https://covers.openlibrary.org/b/id/{}-M.jpg"
OPENLIB_WORK = "https://openlibrary.org{}"

_YEAR_RE = re.compile(r"\s*[\(\[]?(19\d{2}|20\d{2})[\)\]]?\s*$")
_TOKEN_SAFETY_MARGIN = 60
_LOW_PRIORITY_MARKERS = (
    "karaoke", "made famous", "tribute", "in the style of",
    "cover version", "instrumental version",
)
_STEAM_REVIEWS = {
    "Overwhelmingly Positive": (REVIEW_ACCLAIMED, "Acclamé"),
    "Very Positive": (REVIEW_UP, "Très positif"),
    "Positive": (REVIEW_UP, "Positif"),
    "Mostly Positive": (REVIEW_OK, "Plutôt positif"),
    "Mixed": (REVIEW_MIXED, "Mitigé"),
    "Mostly Negative": (REVIEW_DOWN, "Plutôt négatif"),
    "Negative": (REVIEW_DOWN, "Négatif"),
    "Very Negative": (REVIEW_BOMB, "Très négatif"),
    "Overwhelmingly Negative": (REVIEW_BOMB, "Descendu en flammes"),
}


@dataclass
class MediaHit:
    source: str
    source_id: str
    media_type: str
    title: str
    subtitle: str = ""
    year: int | None = None
    poster_url: str | None = None
    url: str = ""
    overview: str = ""
    genres: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.source, self.source_id, self.media_type)


def parse_query_year(query: str) -> tuple[str, int | None]:
    match = _YEAR_RE.search(query)
    if not match:
        return query.strip(), None
    cleaned = query[: match.start()].strip()
    return cleaned or query.strip(), int(match.group(1))


def _poster(path: str | None) -> str | None:
    return TMDB_IMG.format(path) if path else None


def _year_from(date_str: str | None) -> int | None:
    if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
        return int(date_str[:4])
    return None


def _fmt_duration_ms(ms: int) -> str:
    minutes, seconds = divmod(ms // 1000, 60)
    return f"{minutes}:{seconds:02d}"


def _best_spotify_image(images: list[dict]) -> str | None:
    if not images:
        return None
    ranked = sorted(images, key=lambda img: img.get("width") or 0)
    for img in ranked:
        if (img.get("width") or 0) >= 300:
            return img.get("url")
    return images[0].get("url")


async def _json(session: aiohttp.ClientSession, url: str, **kwargs: Any) -> Any:
    async with session.get(url, **kwargs) as resp:
        if resp.status >= 400:
            raise aiohttp.ClientResponseError(
                resp.request_info,
                resp.history,
                status=resp.status,
                message=resp.reason or "",
            )
        return await resp.json(content_type=None)


class TMDBClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self.session = session
        self.api_key = api_key

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, media_type: str | None, limit: int) -> list[MediaHit]:
        if not self.available:
            return []
        clean, year = parse_query_year(query)
        params: dict[str, Any] = {
            "query": clean,
            "api_key": self.api_key,
            "language": "fr-FR",
            "include_adult": "false",
        }
        if media_type == "movie":
            endpoint = f"{TMDB_BASE}/search/movie"
            if year:
                params["year"] = year
        elif media_type == "tv":
            endpoint = f"{TMDB_BASE}/search/tv"
            if year:
                params["first_air_date_year"] = year
        else:
            endpoint = f"{TMDB_BASE}/search/multi"

        try:
            payload = await _json(self.session, endpoint, params=params, timeout=aiohttp.ClientTimeout(total=8))
        except aiohttp.ClientResponseError as exc:
            logger.warning("Recherche TMDB échouée : HTTP %s (%s) %s", exc.status, endpoint, exc.message)
            return []
        except Exception as exc:
            logger.warning("Recherche TMDB échouée (%s) : %s", endpoint, exc)
            return []

        raw_results = payload.get("results") or []
        results: list[dict] = []
        for item in raw_results:
            kind = item.get("media_type") or media_type
            if kind not in ("movie", "tv"):
                continue
            item = {**item, "media_type": kind}
            if year and media_type not in ("movie", "tv"):
                item_year = _year_from(item.get("release_date") or item.get("first_air_date"))
                title = (item.get("title") or item.get("name") or "").casefold()
                original = (item.get("original_title") or item.get("original_name") or "").casefold()
                if item_year != year and title != clean.casefold() and original != clean.casefold():
                    continue
            results.append(item)

        q = clean.casefold()

        def rank(item: dict) -> tuple[int, int, float]:
            title = (item.get("title") or item.get("name") or "").casefold()
            original = (item.get("original_title") or item.get("original_name") or "").casefold()
            exact = title == q or original == q
            item_year = _year_from(item.get("release_date") or item.get("first_air_date"))
            year_match = year is not None and item_year == year
            return (int(exact and year_match), int(exact or year_match), float(item.get("popularity") or 0))

        results.sort(key=rank, reverse=True)
        return [self._from_search(item) for item in results[:limit]]

    async def enrich(self, hit: MediaHit) -> MediaHit:
        if not self.available:
            return hit
        try:
            payload = await _json(
                self.session,
                f"{TMDB_BASE}/{hit.media_type}/{hit.source_id}",
                params={
                    "api_key": self.api_key,
                    "language": "fr-FR",
                    "append_to_response": "credits",
                },
                timeout=aiohttp.ClientTimeout(total=8),
            )
        except Exception as exc:
            logger.warning("Détails TMDB indisponibles (%s/%s) : %s", hit.media_type, hit.source_id, exc)
            return hit
        return self._from_details(hit, payload)

    def _from_search(self, item: dict) -> MediaHit:
        kind = item.get("media_type", "movie")
        title = item.get("title") or item.get("name") or "?"
        date_str = item.get("release_date") or item.get("first_air_date") or ""
        tmdb_id = str(item.get("id", ""))
        return MediaHit(
            source="tmdb",
            source_id=tmdb_id,
            media_type=kind,
            title=title,
            year=_year_from(date_str),
            poster_url=_poster(item.get("poster_path")),
            url=f"https://www.themoviedb.org/{kind}/{tmdb_id}" if tmdb_id else "",
            overview=(item.get("overview") or "").strip(),
            extra={
                "vote_average": item.get("vote_average") or 0,
                "vote_count": item.get("vote_count") or 0,
                "original_language": item.get("original_language") or "",
            },
        )

    def _from_details(self, hit: MediaHit, details: dict) -> MediaHit:
        credits = details.get("credits") or {}
        crew = credits.get("crew") or []
        cast = [c.get("name") for c in (credits.get("cast") or [])[:3] if c.get("name")]
        directors = [c.get("name") for c in crew if c.get("job") == "Director" and c.get("name")]
        created_by = [c.get("name") for c in (details.get("created_by") or []) if c.get("name")]
        genres = [g["name"] for g in details.get("genres") or [] if g.get("name")]
        backdrop = details.get("backdrop_path")
        hit.title = details.get("title") or details.get("name") or hit.title
        hit.year = _year_from(details.get("release_date") or details.get("first_air_date")) or hit.year
        hit.poster_url = _poster(details.get("poster_path")) or hit.poster_url
        hit.overview = (details.get("overview") or "").strip() or hit.overview
        hit.genres = genres
        hit.extra = {
            **hit.extra,
            "vote_average": details.get("vote_average") or hit.extra.get("vote_average") or 0,
            "vote_count": details.get("vote_count") or hit.extra.get("vote_count") or 0,
            "runtime": details.get("runtime") or 0,
            "seasons": details.get("number_of_seasons") or 0,
            "director": directors[0] if directors else "",
            "created_by": created_by,
            "cast": cast,
            "original_language": details.get("original_language") or "",
            "backdrop_url": TMDB_BACKDROP.format(backdrop) if backdrop else "",
        }
        return hit


class SteamClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def search(self, query: str, limit: int) -> list[MediaHit]:
        try:
            payload = await _json(
                self.session,
                STEAM_SEARCH,
                params={"term": query, "cc": "fr", "l": "french", "num": max(limit, 5)},
                timeout=aiohttp.ClientTimeout(total=8),
            )
        except Exception as exc:
            logger.warning("Recherche Steam échouée : %s", exc)
            return []

        hits: list[MediaHit] = []
        q_low = query.casefold()
        want_extra = any(marker in q_low for marker in ("soundtrack", "ost", "dlc", "bundle"))
        for item in payload.get("items") or []:
            kind = (item.get("type") or "app").lower()
            name = (item.get("name") or "")
            if kind in ("music", "video", "hardware") and not want_extra:
                continue
            if kind in ("dlc", "bundle") and not want_extra:
                continue
            if not want_extra and any(marker in name.casefold() for marker in ("soundtrack", " original score", "ost")):
                continue
            appid = item.get("id")
            if not appid:
                continue
            hits.append(
                MediaHit(
                    source="steam",
                    source_id=str(appid),
                    media_type="game",
                    title=item.get("name") or "?",
                    poster_url=item.get("tiny_image") or STEAM_HEADER.format(appid),
                    url=STEAM_STORE.format(appid),
                )
            )
            if len(hits) >= limit:
                break
        return hits

    async def enrich(self, hit: MediaHit) -> MediaHit:
        try:
            payload = await _json(
                self.session,
                STEAM_DETAILS,
                params={"appids": hit.source_id, "cc": "fr", "l": "french"},
                timeout=aiohttp.ClientTimeout(total=8),
            )
        except Exception as exc:
            logger.warning("Détails Steam indisponibles (appid %s) : %s", hit.source_id, exc)
            return hit

        block = (payload or {}).get(str(hit.source_id)) or {}
        if not block.get("success"):
            return hit
        data = block.get("data") or {}
        hit.title = data.get("name") or hit.title
        hit.overview = (data.get("short_description") or "").strip() or hit.overview
        hit.genres = [g["description"] for g in data.get("genres") or [] if g.get("description")]
        hit.poster_url = STEAM_HEADER.format(hit.source_id)
        hit.url = STEAM_STORE.format(hit.source_id)
        release = (data.get("release_date") or {}).get("date") or ""
        year_match = re.search(r"(19\d{2}|20\d{2})", release)
        if year_match:
            hit.year = int(year_match.group(1))
        price = data.get("price_overview") or {}
        review_desc = data.get("review_score_desc") or ""
        if not review_desc:
            try:
                reviews = await _json(
                    self.session,
                    f"https://store.steampowered.com/appreviews/{hit.source_id}",
                    params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
                    timeout=aiohttp.ClientTimeout(total=8),
                )
                review_desc = (reviews.get("query_summary") or {}).get("review_score_desc") or ""
            except Exception:
                review_desc = ""
        emoji, label = _STEAM_REVIEWS.get(review_desc, ("", review_desc))
        developers = data.get("developers") or []
        hit.subtitle = developers[0] if developers else ""
        hit.extra = {
            **hit.extra,
            "is_free": bool(data.get("is_free")),
            "price_final": price.get("final"),
            "price_initial": price.get("initial"),
            "discount": price.get("discount_percent") or 0,
            "review_emoji": emoji,
            "review_label": label,
            "developers": developers[:2],
        }
        return hit


class SpotifyClient:
    def __init__(self, session: aiohttp.ClientSession, client_id: str, client_secret: str):
        self.session = session
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def available(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _token_value(self) -> str | None:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        if not self.available:
            return None
        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            async with self.session.post(
                SPOTIFY_TOKEN_URL,
                headers={
                    "Authorization": f"Basic {creds}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status >= 400:
                    logger.warning("Auth Spotify échouée : HTTP %s", resp.status)
                    return None
                payload = await resp.json()
        except Exception as exc:
            logger.warning("Auth Spotify échouée : %s", exc)
            return None
        token = payload.get("access_token")
        if not token:
            return None
        self._token = token
        self._token_expires_at = time.time() + int(payload.get("expires_in") or 3600) - _TOKEN_SAFETY_MARGIN
        return token

    async def search(self, query: str, media_type: str, limit: int) -> list[MediaHit]:
        token = await self._token_value()
        if not token:
            return []
        spotify_type = "album" if media_type == "album" else "track"
        try:
            payload = await _json(
                self.session,
                SPOTIFY_SEARCH_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={"q": query, "type": spotify_type, "market": "FR", "limit": max(limit, 8)},
                timeout=aiohttp.ClientTimeout(total=8),
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                self._token = None
            logger.warning("Recherche Spotify échouée : %s", exc)
            return []
        except Exception as exc:
            logger.warning("Recherche Spotify échouée : %s", exc)
            return []

        if media_type == "album":
            items = (payload.get("albums") or {}).get("items") or []
            return [self._from_album(item) for item in items[:limit] if item]
        items = (payload.get("tracks") or {}).get("items") or []
        ranked = self._rank_tracks(items, query)
        return [self._from_track(item) for item in ranked[:limit]]

    def _rank_tracks(self, items: list[dict], query: str) -> list[dict]:
        q = query.strip().casefold()

        def score(item: dict) -> tuple[int, int, int, float]:
            title = (item.get("name") or "").strip()
            artists = [a.get("name", "") for a in item.get("artists") or []]
            low = title.casefold()
            return (
                int(low == q),
                int(any(name and name.casefold() in q for name in artists)),
                -int(any(marker in low for marker in _LOW_PRIORITY_MARKERS)),
                float(item.get("popularity") or 0),
            )

        return sorted(items, key=score, reverse=True)

    def _from_track(self, item: dict) -> MediaHit:
        artists = [a.get("name") for a in item.get("artists") or [] if a.get("name")]
        album = item.get("album") or {}
        duration = item.get("duration_ms")
        return MediaHit(
            source="spotify",
            source_id=item.get("id") or "",
            media_type="track",
            title=item.get("name") or "?",
            subtitle=", ".join(artists),
            year=_year_from(album.get("release_date")),
            poster_url=_best_spotify_image(album.get("images") or []),
            url=(item.get("external_urls") or {}).get("spotify") or "",
            extra={
                "album": album.get("name") or "",
                "duration": _fmt_duration_ms(duration) if isinstance(duration, int) else "",
                "explicit": bool(item.get("explicit")),
                "popularity": item.get("popularity") or 0,
            },
        )

    def _from_album(self, item: dict) -> MediaHit:
        artists = [a.get("name") for a in item.get("artists") or [] if a.get("name")]
        return MediaHit(
            source="spotify",
            source_id=item.get("id") or "",
            media_type="album",
            title=item.get("name") or "?",
            subtitle=", ".join(artists),
            year=_year_from(item.get("release_date")),
            poster_url=_best_spotify_image(item.get("images") or []),
            url=(item.get("external_urls") or {}).get("spotify") or "",
            extra={"total_tracks": item.get("total_tracks") or 0},
        )


class OpenLibraryClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def search(self, query: str, limit: int) -> list[MediaHit]:
        try:
            payload = await _json(
                self.session,
                OPENLIB_SEARCH,
                params={"q": query, "limit": limit, "fields": "key,title,author_name,first_publish_year,cover_i,subject"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
        except Exception as exc:
            logger.warning("Recherche Open Library échouée : %s", exc)
            return []

        hits: list[MediaHit] = []
        for item in payload.get("docs") or []:
            key = item.get("key") or ""
            if not key:
                continue
            authors = item.get("author_name") or []
            cover_id = item.get("cover_i")
            subjects = [s for s in (item.get("subject") or []) if isinstance(s, str)][:3]
            hits.append(
                MediaHit(
                    source="openlibrary",
                    source_id=key,
                    media_type="book",
                    title=item.get("title") or "?",
                    subtitle=", ".join(authors[:2]),
                    year=item.get("first_publish_year") if isinstance(item.get("first_publish_year"), int) else None,
                    poster_url=OPENLIB_COVER.format(cover_id) if cover_id else None,
                    url=OPENLIB_WORK.format(key),
                    genres=subjects,
                    extra={"authors": authors[:3]},
                )
            )
        return hits


_TYPE_PRIORITY = {
    "movie": 0,
    "tv": 1,
    "game": 2,
    "album": 3,
    "book": 4,
    "track": 5,
}


class MediaCatalog:
    """Orchestre les fournisseurs et fusionne les résultats multi-sources."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        tmdb_key: str,
        spotify_id: str,
        spotify_secret: str,
    ):
        self.tmdb = TMDBClient(session, tmdb_key.strip())
        self.steam = SteamClient(session)
        self.spotify = SpotifyClient(session, spotify_id.strip(), spotify_secret.strip())
        self.books = OpenLibraryClient(session)

    def status(self) -> dict[str, bool]:
        return {
            "TMDB": self.tmdb.available,
            "Spotify": self.spotify.available,
            "Steam": True,
            "Livres": True,
        }

    async def search(self, query: str, media_type: str) -> list[MediaHit]:
        wide = media_type == "all"
        per = 4 if wide else 8
        clean, _year = parse_query_year(query)
        tasks: list[Any] = []
        # Film + série en parallèle : /search/multi ignore trop souvent les films
        # et, en cas d'échec silencieux, Spotify se retrouvait en tête.
        if media_type == "all":
            tasks.append(self.tmdb.search(query, "movie", 8))
            tasks.append(self.tmdb.search(query, "tv", 6))
        elif media_type in ("movie", "tv"):
            tasks.append(self.tmdb.search(query, media_type, 8))
        if media_type in ("all", "game"):
            tasks.append(self.steam.search(clean, per))
        if media_type in ("all", "album"):
            tasks.append(self.spotify.search(clean, "album", per))
        if media_type in ("all", "track"):
            tasks.append(self.spotify.search(clean, "track", 3 if wide else 8))
        if media_type in ("all", "book"):
            tasks.append(self.books.search(clean, per))

        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        merged: list[MediaHit] = []
        seen: set[tuple[str, str, str]] = set()
        for result in gathered:
            if isinstance(result, Exception):
                logger.warning("Fournisseur média en échec : %s", result)
                continue
            for hit in result:
                if not hit.source_id or hit.identity in seen:
                    continue
                seen.add(hit.identity)
                merged.append(hit)

        q = clean.casefold()

        def rank(hit: MediaHit) -> tuple[int, int, int, int]:
            title = hit.title.casefold()
            return (
                -_TYPE_PRIORITY.get(hit.media_type, 9),
                int(title == q),
                int(title.startswith(q)),
                int(q in title),
            )

        merged.sort(key=rank, reverse=True)
        if wide and not any(hit.source == "tmdb" for hit in merged):
            if not self.tmdb.available:
                logger.warning("Recherche « tous types » sans TMDB : clé absente")
            else:
                logger.warning("Recherche « tous types » : TMDB n'a rien renvoyé pour %r", query)
        return merged[:25]

    async def enrich(self, hit: MediaHit) -> MediaHit:
        if hit.source == "tmdb":
            return await self.tmdb.enrich(hit)
        if hit.source == "steam":
            return await self.steam.enrich(hit)
        return hit
