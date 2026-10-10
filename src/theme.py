"""Colorways.

Every color on the page is a CSS custom property. A theme is one of the
presets below -- a full set of them for light and for dark -- written into
each page by css().
"""
from __future__ import annotations

from typing import Any

# The properties every preset sets, in the order _palette() takes them.
TOKEN_NAMES = (
    "paper",           # page background
    "panel",           # raised surfaces, inputs
    "ink",             # body text
    "muted",           # secondary text
    "line",            # soft borders
    "line-hard",       # slab borders and shadows
    "accent",          # links, buttons
    "accent-soft",     # tinted backgrounds
    "on-accent",       # text on filled buttons and chips
    "second",          # read and archived marks
    "second-deep",     # small stamped text, filled chips
    "highlight",       # spines, underlines, selection
    "highlight-soft",  # tinted backgrounds
    "on-highlight",    # selected text
    "danger",          # delete, errors
    "warn",            # partial archives
    "warn-soft",       # notices
)

MODES = ("auto", "light", "dark")

DEFAULT = "umber"

# The paper texture overlay has to blend the other way on a dark ground.
_TOOTH = {
    "light": {"tooth-blend": "multiply", "tooth-opacity": ".22"},
    "dark": {"tooth-blend": "screen", "tooth-opacity": ".1"},
}


def _palette(*values: str) -> dict[str, str]:
    assert len(values) == len(TOKEN_NAMES)
    return dict(zip(TOKEN_NAMES, values))


PRESETS: dict[str, dict[str, Any]] = {
    # The original: cream stock, burnt umber and olive.
    "umber": {
        "label": "Umber & sage",
        "light": _palette(
            "#fcecd8", "#fff6e9", "#2a1608", "#7a5b3e", "#ddc4a2", "#2a1608",
            "#6e3511", "#f3ded0", "#fcecd8",
            "#597928", "#4f6b23", "#91ac67", "#e6ecd6", "#17100a",
            "#8f2f14", "#8a5a12", "#f6e4c8"),
        "dark": _palette(
            "#17100a", "#201710", "#f2e2cc", "#b09373", "#3d2c1c", "#5d4630",
            "#d08a49", "#33210f", "#17100a",
            "#9dbe6e", "#9dbe6e", "#91ac67", "#26301b", "#17100a",
            "#dd7a5b", "#d2a85a", "#33260f"),
    },
    # Blueprint paper: prussian blue on pale stock, teal for what is done.
    "cyanotype": {
        "label": "Cyanotype",
        "light": _palette(
            "#e9eef2", "#f6f9fb", "#0f1d2b", "#4a5d70", "#c3cfda", "#0f1d2b",
            "#1d4e89", "#d9e4f1", "#f6f9fb",
            "#2f6f73", "#245a5e", "#8fb8c9", "#dbe9ee", "#0f1d2b",
            "#a1301f", "#7d5410", "#f3e6cc"),
        "dark": _palette(
            "#0d141c", "#142030", "#dfe8f0", "#93a6b8", "#24364a", "#3e5670",
            "#7fb0e6", "#182c44", "#0d141c",
            "#7cc4c4", "#7cc4c4", "#5d8fa3", "#1a2e36", "#0d141c",
            "#ef8a74", "#e0b25e", "#33280f"),
    },
    # Bound ledger: oxblood, slate and brass.
    "oxblood": {
        "label": "Oxblood & brass",
        "light": _palette(
            "#f3efe9", "#fbf8f4", "#1f1a1c", "#6b6062", "#d8cfc6", "#1f1a1c",
            "#7a1f2b", "#f0dcdc", "#fbf8f4",
            "#3f5f6b", "#34505a", "#c9a66b", "#f1e6cf", "#1f1a1c",
            "#9b2c12", "#7d5410", "#f4e5c6"),
        "dark": _palette(
            "#161112", "#1f191a", "#efe6e3", "#ab9c98", "#3a2f30", "#5a4a4b",
            "#e0848f", "#3a1c21", "#161112",
            "#8fb5c2", "#8fb5c2", "#c9a66b", "#342a18", "#161112",
            "#f08a6c", "#dcae5c", "#33270f"),
    },
    # Black, white and red all over.
    "newsprint": {
        "label": "Newsprint",
        "light": _palette(
            "#f2f0eb", "#fbfaf7", "#111111", "#5e5c58", "#d4d1ca", "#111111",
            "#b3261e", "#f5dcd9", "#fbfaf7",
            "#3d3d3d", "#2e2e2e", "#b8b5ad", "#e6e3dc", "#111111",
            "#b3261e", "#7a5a10", "#efe5cc"),
        "dark": _palette(
            "#121212", "#1b1b1b", "#ececea", "#a3a19c", "#333333", "#555555",
            "#ff7a6e", "#3a1a17", "#121212",
            "#cfcfcf", "#cfcfcf", "#8a8780", "#2a2926", "#121212",
            "#ff7a6e", "#e0b45e", "#322810"),
    },
    # Plum ink, marigold for what is done.
    "plum": {
        "label": "Plum & marigold",
        "light": _palette(
            "#f6eef3", "#fdf8fb", "#241426", "#6e5870", "#dccbd8", "#241426",
            "#6b2d6f", "#eedcef", "#fdf8fb",
            "#8c5a00", "#6f4800", "#e8b04a", "#f8ead0", "#241426",
            "#a3271f", "#7d5410", "#f5e5c4"),
        "dark": _palette(
            "#160f17", "#201622", "#f1e6f0", "#b39db3", "#3a2a3c", "#5c4660",
            "#d49ad8", "#3a2040", "#160f17",
            "#e8b04a", "#e8b04a", "#b98a3a", "#33270f", "#160f17",
            "#f08a7c", "#e8c06e", "#33280f"),
    },
    # Forest green with rust for what is done.
    "forest": {
        "label": "Forest & rust",
        "light": _palette(
            "#eef0e6", "#f8f9f2", "#17201a", "#56604f", "#cdd3c1", "#17201a",
            "#2d5a3d", "#dbe6d9", "#f8f9f2",
            "#9a4a1c", "#8a3f15", "#c9874f", "#f2e2d2", "#17201a",
            "#a12a1a", "#7a5a10", "#f0e6c8"),
        "dark": _palette(
            "#0f1511", "#172019", "#e3eadf", "#9eab98", "#26332a", "#405244",
            "#8cc79c", "#1c3022", "#0f1511",
            "#e09a6a", "#e09a6a", "#b9774a", "#33231a", "#0f1511",
            "#f08a74", "#dcb060", "#322a12"),
    },
    # Fired clay and a cool teal against it.
    "terracotta": {
        "label": "Terracotta & teal",
        "light": _palette(
            "#f7ebe3", "#fdf6f1", "#2b1a14", "#75584b", "#e3cbbd", "#2b1a14",
            "#a3442a", "#f4d9cd", "#fdf6f1",
            "#1f6f6b", "#195b58", "#e39a7a", "#d6ebe8", "#2b1a14",
            "#9b2215", "#7e5612", "#f5e4c6"),
        "dark": _palette(
            "#1a110d", "#241813", "#f3e3d9", "#b89c8e", "#3d2a21", "#5e4436",
            "#ec9575", "#3d2219", "#1a110d",
            "#6cc4bd", "#6cc4bd", "#c9775a", "#193230", "#1a110d",
            "#f2876f", "#dcb064", "#33270f"),
    },
    # After Solarized: warm cream or deep teal, blue and yellow.
    "solar": {
        "label": "Solar",
        "light": _palette(
            "#fdf6e3", "#fffbf0", "#073642", "#586e75", "#e6dcc0", "#073642",
            "#1d6fa5", "#e3eef3", "#fdf6e3",
            "#5c6b00", "#4e5c00", "#d0a21a", "#f3ead0", "#073642",
            "#b8261f", "#8a5d00", "#f5e6c0"),
        "dark": _palette(
            "#002b36", "#073642", "#eee8d5", "#93a1a1", "#0f4553", "#2a5f6c",
            "#4fa3e0", "#06303d", "#002b36",
            "#9bb000", "#9bb000", "#b58900", "#1f3a2a", "#002b36",
            "#f0605a", "#d6a72b", "#2a3a1e"),
    },
    # Pencil grey, slate blue and a citron highlighter.
    "graphite": {
        "label": "Graphite & citron",
        "light": _palette(
            "#efefec", "#f9f9f7", "#1c1d1f", "#5c5f63", "#d2d3cf", "#1c1d1f",
            "#33415c", "#dfe3ea", "#f9f9f7",
            "#5e6b00", "#4d5800", "#d4e04a", "#eef2c8", "#1c1d1f",
            "#a8261c", "#7a5a10", "#efe6c8"),
        "dark": _palette(
            "#141517", "#1d1f22", "#e8e9e6", "#9ea2a6", "#2d3034", "#4a4e54",
            "#a9b8d9", "#252d3d", "#141517",
            "#cfdc4a", "#cfdc4a", "#a9b52c", "#2b2f12", "#141517",
            "#f08070", "#ddb560", "#322a10"),
    },
}


def preset_name(name: str | None) -> str:
    return name if name in PRESETS else DEFAULT


def _block(selector: str, palette: dict[str, str], mode: str) -> str:
    props = {**palette, **_TOOTH[mode]}
    body = " ".join(f"--{name}: {value};" for name, value in props.items())
    return f"{selector} {{ {body} }}"


def css(preset: str | None) -> str:
    """The custom properties for both modes.

    Dark follows the system unless the page says otherwise: <html
    data-theme="light"> or "dark" pins it, which is how the appearance
    setting works.
    """
    palette = PRESETS[preset_name(preset)]
    return "\n".join((
        _block(":root", palette["light"], "light"),
        "@media (prefers-color-scheme: dark) { "
        + _block(':root:not([data-theme="light"])', palette["dark"], "dark")
        + " }",
        _block(':root[data-theme="dark"]', palette["dark"], "dark"),
    ))


def from_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Everything a page needs to draw itself in the chosen colors."""
    preset = preset_name(settings.get("THEME"))
    mode = settings.get("THEME_MODE")
    return {
        "preset": preset,
        "mode": mode if mode in MODES else "auto",
        "css": css(preset),
    }
