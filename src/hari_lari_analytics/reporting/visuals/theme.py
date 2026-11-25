"""The theme: the palette, the colour tokens and the text roles of docs/components.md, "Visuals".

A visual's options name tokens (`accent`, `highlight`, ...) and roles (`small`, `body`, ...),
never a hex colour or a point size; `Theme` resolves them. `Palette` and `DAYLIGHT` are those of
the report before the component port, unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from matplotlib import font_manager

from hari_lari_analytics.reporting.formats import Tone

FONTS = Path(__file__).resolve().parents[2] / "fonts"
for _font in FONTS.glob("*.ttf"):
    font_manager.fontManager.addfont(str(_font))

W, H = 11.69, 8.27  # A4 landscape, inches

Token = Literal[
    "bg",
    "card",
    "edge",
    "grid",
    "text",
    "muted",
    "accent",
    "highlight",
    "prior",
    "good",
    "bad",
    "holiday",
    "closure",
    "promotion",
]
Role = Literal[
    "page_title", "kicker", "card_title", "hint", "headline", "value", "label", "body", "small"
]
TOKENS: tuple[Token, ...] = get_args(Token)
ROLES: tuple[Role, ...] = get_args(Role)
EVENT_KINDS = ("holiday", "closure", "promotion")


@dataclass(frozen=True)
class Palette:
    bg: str
    card: str
    edge: str
    text: str
    muted: str
    primary: str  # bars and increases
    secondary: str  # prior period, highlights
    negative: str  # decreases
    grid: str
    series: tuple[str, ...]  # payment types


DAYLIGHT = Palette(
    bg="#f3f5f8",
    card="#ffffff",
    edge="#e3e7ed",
    text="#0f172a",
    muted="#64748b",
    primary="#0e7c7b",
    secondary="#ff6b4a",
    negative="#d9480f",
    grid="#e9edf2",
    series=("#0e7c7b", "#ff6b4a", "#f2b84b", "#7c8db5"),
)

# The sizes in points of today's report.
ROLE_SIZES: dict[Role, float] = {
    "page_title": 21,
    "kicker": 7.5,
    "card_title": 10,
    "hint": 7.5,
    "headline": 22,
    "value": 17,
    "label": 7,
    "body": 8,
    "small": 6.5,
}


@dataclass(frozen=True)
class Theme:
    palette: Palette = DAYLIGHT
    sizes: dict[Role, float] = field(default_factory=lambda: dict(ROLE_SIZES))
    font: str = "Inter"
    holiday: str = "#f2b84b"
    closure: str = "#64748b"
    promotion: str = "#7c8db5"

    def color(self, token: Token) -> str:
        p = self.palette
        return {
            "bg": p.bg,
            "card": p.card,
            "edge": p.edge,
            "grid": p.grid,
            "text": p.text,
            "muted": p.muted,
            "accent": p.primary,
            "highlight": p.secondary,
            "prior": p.secondary,
            "good": p.primary,
            "bad": p.negative,
            "holiday": self.holiday,
            "closure": self.closure,
            "promotion": self.promotion,
        }[token]

    def size(self, role: Role) -> float:
        return self.sizes[role]

    def tone_color(self, tone: Tone | None, small: bool = False) -> str:
        """The colour of a change: good, bad, the text colour when neutral; muted when the value
        is missing or the sample small (a small change never looks like a signal)."""
        if small or tone is None:
            return self.color("muted")
        return self.color({"good": "good", "bad": "bad", "neutral": "text"}[tone])

    def series_color(self, i: int) -> str:
        return self.palette.series[i % len(self.palette.series)]

    def rc(self) -> dict:
        """The matplotlib settings of the report."""
        p = self.palette
        return {
            "font.family": self.font,
            "text.color": p.text,
            "axes.labelcolor": p.muted,
            "xtick.color": p.muted,
            "ytick.color": p.muted,
            "axes.facecolor": "none",
            "axes.edgecolor": p.grid,
            "figure.facecolor": p.bg,
            "savefig.facecolor": p.bg,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "xtick.major.size": 0,
            "ytick.major.size": 0,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "pdf.fonttype": 42,
        }


DEFAULT_THEME = Theme()
