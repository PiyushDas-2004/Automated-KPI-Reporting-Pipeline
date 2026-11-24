"""Static matplotlib charts for the weekly report (PNG, light theme, email-safe).

Design rules followed: one axis per chart, thin bars, 2px lines, hairline
recessive gridlines, text in ink colours (never the series colour), a legend
only when there are two or more series, and selective value labels.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless (CI)
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SERIES_1 = "#2a78d6"  # blue
SERIES_2 = "#eb6834"  # orange

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_2,
    "axes.titlecolor": INK,
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.titlepad": 12,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelcolor": INK_2,
    "ytick.labelcolor": INK_2,
    "font.size": 9.5,
    "legend.frameon": False,
    "legend.labelcolor": INK_2,
})


def _style(ax, grid_axis: str = "y") -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left" if grid_axis == "y" else "bottom"].set_visible(False)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def weekly_trend(history: pd.DataFrame, path: Path, weeks: int = 12) -> Path:
    """New registrations per week (bars) with the 4-week rolling average (line)."""
    df = history.sort_values("week_start").tail(weeks)
    labels = [d.strftime("%d %b") for d in pd.to_datetime(df["week_start"])]
    x = range(len(df))

    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    bars = ax.bar(x, df["value"], width=0.45, color=SERIES_1, label="New trials registered", zorder=2)
    (line,) = ax.plot(x, df["rolling_4wk_avg"], color=SERIES_2, linewidth=2, solid_capstyle="round",
            marker="o", markersize=5, markeredgecolor=SURFACE, markeredgewidth=1.5,
            label="4-week rolling average", zorder=3)
    last = len(df) - 1
    ax.annotate(f"{int(df['value'].iloc[-1]):,}", (last, df["value"].iloc[-1]),
                textcoords="offset points", xytext=(0, 5), ha="center", color=INK, fontsize=9,
                fontweight="bold")
    ax.set_xticks(list(x), labels, rotation=0)
    ax.set_title("New trials registered per week")
    ax.set_xlabel("Week starting")
    ax.legend(handles=[bars, line], loc="upper left", bbox_to_anchor=(0, -0.22), ncol=2, fontsize=9)
    _style(ax, "y")
    return _save(fig, path)


def phase_breakdown(rows: pd.DataFrame, path: Path) -> Path:
    """This week's new trials by phase, with the 4-week average as a marker."""
    df = rows.iloc[::-1]  # first category on top
    y = range(len(df))
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    bars = ax.barh(y, df["value"], height=0.5, color=SERIES_1, label="This week", zorder=2)
    dots = ax.scatter(df["rolling_4wk_avg"], y, marker="D", s=30, color=SERIES_2, edgecolor=SURFACE,
               linewidth=1.2, label="4-week average", zorder=3)
    xmax = max(df["value"].max(), df["rolling_4wk_avg"].fillna(0).max())
    for yi, v, avg in zip(y, df["value"], df["rolling_4wk_avg"].fillna(0)):
        # label sits past whichever is further right (bar end or average marker), so they never collide
        ax.text(max(v, avg) + xmax * 0.02, yi, f"{int(v):,}", va="center", color=INK_2, fontsize=8.5)
    ax.set_yticks(list(y), df["dimension_value"])
    ax.set_xlim(0, xmax * 1.12)
    ax.set_title("New trials by phase - this week vs 4-week average")
    ax.legend(handles=[bars, dots], loc="upper left", bbox_to_anchor=(0, -0.08), ncol=2, fontsize=9)
    _style(ax, "x")
    return _save(fig, path)


def top_countries(rows: pd.DataFrame, path: Path) -> Path:
    """Top 10 countries by trial sites added through this week's new registrations."""
    df = rows.sort_values("value").tail(10)
    y = range(len(df))
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    ax.barh(y, df["value"], height=0.5, color=SERIES_1, zorder=2)
    xmax = df["value"].max()
    for yi, v in zip(y, df["value"]):
        ax.text(v + xmax * 0.012, yi, f"{int(v):,}", va="center", color=INK_2, fontsize=8.5)
    ax.set_yticks(list(y), df["dimension_value"])
    ax.set_xlim(0, xmax * 1.12)
    ax.set_title("Top 10 countries by new trial sites this week")
    _style(ax, "x")
    return _save(fig, path)
