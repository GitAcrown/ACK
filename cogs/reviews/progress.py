"""XP, paliers et affinités de goût — la liste PROFILE_REWARDS se craft ici."""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Récompenses de profil
# Ajoute tes emojis custom ACK dans `emoji` (<:name:id>) quand tu les as.
# `unlock_level` = niveau à partir duquel ça s'affiche sur le profil.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileReward:
    id: str
    name: str
    unlock_level: int
    emoji: str = ""
    description: str = ""

    def label(self) -> str:
        return f"{self.emoji} {self.name}".strip() if self.emoji else self.name


PROFILE_REWARDS: tuple[ProfileReward, ...] = (
    ProfileReward("novice", "Novice", 1, description="Le carnet est ouvert."),
    ProfileReward("curieux", "Curieux", 2, description="Tu reviens noter."),
    ProfileReward("chroniqueur", "Chroniqueur", 3, description="Les commentaires comptent."),
    ProfileReward("critique", "Critique", 5, description="On lit tes notes."),
    ProfileReward("referent", "Référent", 8, description="Les autres se calent sur toi."),
    ProfileReward("legende", "Légende du salon", 12, description="Le goût du serveur."),
)

LEVEL_TITLES: tuple[tuple[int, str], ...] = (
    (12, "Légende du salon"),
    (8, "Référent"),
    (5, "Critique"),
    (3, "Chroniqueur"),
    (2, "Curieux"),
    (1, "Novice"),
)

XP_PIONEER = 15
XP_JOIN = 10
XP_COMMENT = 10
XP_UPDATE = 2
DAILY_CAP = 80
FULL_AWARDS_PER_DAY = 5
MIN_AFFINITY_OVERLAP = 3


@dataclass
class XpAward:
    gained: int
    total: int
    daily: int
    level: int
    previous_level: int
    capped: bool

    @property
    def leveled_up(self) -> bool:
        return self.level > self.previous_level

    @property
    def new_rewards(self) -> list[ProfileReward]:
        return [r for r in PROFILE_REWARDS if self.previous_level < r.unlock_level <= self.level]


def title_for_level(level: int) -> str:
    for minimum, title in LEVEL_TITLES:
        if level >= minimum:
            return title
    return "Novice"


def rewards_for_level(level: int) -> list[ProfileReward]:
    return [r for r in PROFILE_REWARDS if r.unlock_level <= level]


def xp_to_reach_level(level: int) -> int:
    """XP cumulé nécessaire pour atteindre `level` (le niveau 1 vaut 0)."""
    if level <= 1:
        return 0
    total = 0
    for current in range(1, level):
        total += 40 * current
    return total


def level_for_xp(xp: int) -> int:
    level = 1
    while xp >= xp_to_reach_level(level + 1):
        level += 1
        if level > 99:
            break
    return level


def level_progress(xp: int) -> tuple[int, int, int, int]:
    """(niveau, xp dans le palier, xp requis pour le suivant, xp total)."""
    level = level_for_xp(xp)
    current_floor = xp_to_reach_level(level)
    next_floor = xp_to_reach_level(level + 1)
    return level, xp - current_floor, next_floor - current_floor, xp


def apply_daily_limits(base: int, *, awards_today: int, daily_xp: int) -> tuple[int, bool]:
    if base <= 0:
        return 0, daily_xp >= DAILY_CAP
    if daily_xp >= DAILY_CAP:
        return 0, True
    amount = base if awards_today < FULL_AWARDS_PER_DAY else max(1, base // 2)
    room = DAILY_CAP - daily_xp
    if amount > room:
        return room, True
    return amount, False


def compute_review_xp(*, created: bool, pioneer: bool, new_comment: bool) -> int:
    if created:
        amount = XP_PIONEER if pioneer else XP_JOIN
        if new_comment:
            amount += XP_COMMENT
        return amount
    amount = XP_UPDATE
    if new_comment:
        amount += XP_COMMENT
    return amount


def agreement_percent(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    mean_diff = sum(abs(a - b) for a, b in pairs) / len(pairs)
    return max(0.0, min(100.0, 100.0 * (1.0 - mean_diff / 5.0)))


@dataclass
class Affinity:
    user_id: int
    overlap: int
    percent: float
    agreements: list[tuple[str, float, float]]
    disagreements: list[tuple[str, float, float]]
