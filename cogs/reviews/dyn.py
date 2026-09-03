"""Boutons persistants d'une fiche publiée (DynamicItem), TTL 10 min comme MARIA.

custom_id `ack:rev:{id}:{action}` + SQLite. À l'échéance (ou clic trop tard),
le message est réécrit sans boutons. Un clic ouvre le menu en éphémère.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import discord

from .providers import MediaHit

logger = logging.getLogger("ACK.Reviews.Dyn")

DB_PATH = Path(__file__).resolve().parent / "data" / "dyn_fiches.db"
TTL = timedelta(minutes=10)
PURGE_AFTER = timedelta(hours=1)
_ID_RE = re.compile(r"^[0-9a-f]{8}$")


@dataclass
class FicheRecord:
    id: str
    payload: dict[str, Any]
    expires_at: datetime
    channel_id: int
    message_id: int
    stripped: bool


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_db() -> None:
    with _db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fiches (
                id          TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                channel_id  INTEGER DEFAULT 0,
                message_id  INTEGER DEFAULT 0,
                stripped    INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rev_dyn_exp ON fiches(stripped, expires_at)"
        )


def hit_to_dict(hit: MediaHit) -> dict[str, Any]:
    return {
        "source": hit.source,
        "source_id": hit.source_id,
        "media_type": hit.media_type,
        "title": hit.title,
        "subtitle": hit.subtitle,
        "year": hit.year,
        "poster_url": hit.poster_url,
        "url": hit.url,
        "overview": hit.overview,
        "genres": list(hit.genres),
        "extra": dict(hit.extra),
    }


def hit_from_dict(data: dict[str, Any]) -> MediaHit:
    year = data.get("year")
    return MediaHit(
        source=str(data.get("source") or ""),
        source_id=str(data.get("source_id") or ""),
        media_type=str(data.get("media_type") or ""),
        title=str(data.get("title") or ""),
        subtitle=str(data.get("subtitle") or ""),
        year=int(year) if year else None,
        poster_url=data.get("poster_url") or None,
        url=str(data.get("url") or ""),
        overview=str(data.get("overview") or ""),
        genres=[str(part) for part in (data.get("genres") or [])],
        extra=dict(data.get("extra") or {}),
    )


def _row_to_rec(row: sqlite3.Row) -> FicheRecord:
    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return FicheRecord(
        id=row["id"],
        payload=payload,
        expires_at=_as_utc(datetime.fromisoformat(row["expires_at"])),
        channel_id=int(row["channel_id"] or 0),
        message_id=int(row["message_id"] or 0),
        stripped=bool(row["stripped"]),
    )


def get_record(wid: str) -> FicheRecord | None:
    if not _ID_RE.match(wid):
        return None
    with _db() as conn:
        row = conn.execute("SELECT * FROM fiches WHERE id = ?", (wid,)).fetchone()
    return _row_to_rec(row) if row else None


def create_record(payload: dict[str, Any]) -> str:
    wid = uuid.uuid4().hex[:8]
    with _db() as conn:
        conn.execute(
            "INSERT INTO fiches (id, payload, expires_at) VALUES (?, ?, ?)",
            (wid, json.dumps(payload, ensure_ascii=False), (_now() + TTL).isoformat()),
        )
    return wid


def bind_record(wid: str, channel_id: int, message_id: int) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE fiches SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel_id, message_id, wid),
        )


def mark_stripped(wid: str) -> None:
    with _db() as conn:
        conn.execute("UPDATE fiches SET stripped = 1 WHERE id = ?", (wid,))


def is_live(rec: FicheRecord | None) -> bool:
    return rec is not None and not rec.stripped and rec.expires_at > _now()


def _due_unstripped(now: datetime) -> list[FicheRecord]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM fiches WHERE stripped = 0 AND expires_at <= ?",
            (now.isoformat(),),
        ).fetchall()
    return [_row_to_rec(row) for row in rows]


def _purge(now: datetime) -> None:
    cut = (now - PURGE_AFTER).isoformat()
    with _db() as conn:
        conn.execute("DELETE FROM fiches WHERE stripped = 1 AND expires_at <= ?", (cut,))
        conn.execute(
            "DELETE FROM fiches WHERE message_id = 0 AND expires_at <= ?",
            (now.isoformat(),),
        )


async def sweep_expired(bot: Any, render) -> None:
    now = _now()
    for rec in _due_unstripped(now):
        if rec.message_id:
            view = render(rec, live=False)
            if view is not None:
                try:
                    channel = bot.get_channel(rec.channel_id)
                    if channel is None:
                        channel = await bot.fetch_channel(rec.channel_id)
                    msg = await channel.fetch_message(rec.message_id)
                    await msg.edit(view=view)
                except Exception as exc:
                    logger.info("dyn fiche strip %s : %s", rec.id, exc)
        mark_stripped(rec.id)
    _purge(now)


class FicheDynButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ack:rev:(?P<wid>[0-9a-f]{8}):(?P<act>fiche|critiques|noter)",
):
    def __init__(
        self,
        wid: str,
        action: str,
        *,
        label: str = "·",
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                style=style,
                label=(label or "·")[:80],
                custom_id=f"ack:rev:{wid}:{action}",
            )
        )
        self.wid = wid
        self.action = action

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ):
        return cls(
            match["wid"],
            match["act"],
            label=item.label or "·",
            style=item.style,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from .reviews import handle_published_fiche_click

        await handle_published_fiche_click(interaction, self.wid, self.action)


_init_db()
