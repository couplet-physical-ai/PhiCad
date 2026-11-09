#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""The AI View window's look: colours taken from the shipped theme, and the text
formatting that makes a model's answer readable.

WHY THE SHELL FOLLOWS THE OPERATING SYSTEM
-------------------------------------------
Tk 9.0/aqua ignores custom backgrounds on most ttk controls: frames, buttons, entries,
notebooks and tabs are native light or dark according to macOS.  Replacing them with
painted ``tk.Label`` controls would lose native keyboard, focus and accessibility
behaviour, so PhiCad keeps the native controls and adapts the surfaces it *can* paint.

``use_system_appearance`` resolves Tk's semantic window colour with ``winfo_rgb`` and
selects readable shell text, dividers and cards for that luminance.  The board canvas
and code blocks stay on the theme's near-black board background in both appearances.
This matters beyond taste: the former light-only grey labels were barely visible when
macOS switched the otherwise-native shell to dark mode.

EVERY COLOUR BELOW IS TRACEABLE
-------------------------------
The accents are the layer colours out of `phicad/resources/themes/phicad-altium.json`,
so the pane speaks the same colour language as the board it is describing:

    board.background   rgb(10,10,10)    -> INK          canvas, code blocks
    board.copper.f     rgb(235,45,40)   -> red          errors, front copper
    board.copper.b     rgb(60,100,240)  -> blue         you / user turns
    board.f_silks      rgb(255,235,60)  -> amber        strings in code, warnings
    board.edge_cuts    rgb(230,65,220)  -> magenta      design / synth replies
    board.plated_hole  rgb(60,200,215)  -> cyan         board + view replies
    board.copper.in1   rgb(0,178,0)     -> green        a gate that passed

Full-strength layer colours are legible on the near-black canvas.  Each one gets a
darkened text twin on light surfaces and a lightened twin on dark surfaces, while code
blocks keep their bright forms.  That is the whole rule; there is no unrelated accent
palette.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- colour
# Layer colours, verbatim from phicad-altium.json.  Kept as named constants so the
# derivation below is checkable against the theme file rather than being folklore.
LAYER_COPPER_F = "#eb2d28"      # board.copper.f    rgb(235, 45, 40)
LAYER_COPPER_B = "#3c64f0"      # board.copper.b    rgb(60, 100, 240)
LAYER_SILK = "#ffeb3c"          # board.f_silks     rgb(255, 235, 60)
LAYER_EDGE = "#e641dc"          # board.edge_cuts   rgb(230, 65, 220)
LAYER_HOLE = "#3cc8d7"          # board.plated_hole rgb(60, 200, 215)
LAYER_IN1 = "#00b200"           # board.copper.in1  rgb(0, 178, 0)
INK = "#0a0a0a"                 # board.background  rgb(10, 10, 10)

# Neutral shell.  Warm-neutral rather than blue-grey so it sits beside the macOS
# window chrome instead of competing with it.
SURFACE = "#ffffff"
SURFACE_SUB = "#f4f5f7"         # mode bar, hint row, quiet fills
SURFACE_SUNK = "#eaecef"        # pressed / selected rows
LINE = "#dcdfe4"                # hairline
LINE_SOFT = "#e9ebee"
TEXT = "#14181d"
TEXT_DIM = "#5c6570"
TEXT_FAINT = "#8b949e"

# The darkened twins, for text on a light surface.
ON_LIGHT_RED = "#b3211d"
ON_LIGHT_BLUE = "#2447c8"
ON_LIGHT_AMBER = "#8a6b00"
ON_LIGHT_MAGENTA = "#a32a9c"
ON_LIGHT_CYAN = "#0e7c8c"
ON_LIGHT_GREEN = "#17792f"

# Code blocks: the board background, with the layer colours at full strength because
# that is the surface they were chosen for.
CODE_BG = "#101419"             # INK lifted just enough to read as a card, not a hole
CODE_BG_HEAD = "#181d24"
CODE_FG = "#d9dee5"
CODE_KEY = "#5fd0e0"            # LAYER_HOLE
CODE_STR = "#e8d472"            # LAYER_SILK, dimmed off pure yellow
CODE_NUM = "#8fa8ff"            # LAYER_COPPER_B, lightened
CODE_LIT = "#ff7b74"            # LAYER_COPPER_F, lightened
CODE_PUNC = "#7b8794"
CODE_DIM = "#69737f"

# The canvas an image tab draws on: the theme's board background exactly, so a plot
# with no page fill continues the board's own void instead of sitting in a grey box.
CANVAS = INK

# Aqua follows the macOS appearance even when an application sets no theme of its own.
# The original palette assumed a light shell, which made explicit grey label colours
# nearly disappear when macOS was dark.  Keep the board canvas fixed, but adapt every
# shell neutral and text accent to the surface Tk is actually drawing.
DARK_APPEARANCE = False


def use_system_appearance(root) -> bool:
    """Adapt shell colours to the native light/dark appearance.

    Tk exposes semantic macOS colours such as ``systemWindowBackgroundColor``.  Asking
    ``winfo_rgb`` resolves that token to the colour currently on screen, so this works
    without shelling out to ``defaults`` and also behaves sensibly on Windows/X11.
    Returns whether the native shell is dark.
    """
    global DARK_APPEARANCE
    global SURFACE, SURFACE_SUB, SURFACE_SUNK, LINE, LINE_SOFT
    global TEXT, TEXT_DIM, TEXT_FAINT
    global ON_LIGHT_RED, ON_LIGHT_BLUE, ON_LIGHT_AMBER
    global ON_LIGHT_MAGENTA, ON_LIGHT_CYAN, ON_LIGHT_GREEN

    try:
        red, green, blue = root.winfo_rgb(root.cget("background"))
        luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 65535
        DARK_APPEARANCE = luminance < 0.5
    except Exception:                                      # noqa: BLE001
        DARK_APPEARANCE = False

    if DARK_APPEARANCE:
        SURFACE = "#1e1f22"
        SURFACE_SUB = "#27292d"
        SURFACE_SUNK = "#34373d"
        LINE = "#41454d"
        LINE_SOFT = "#32353a"
        TEXT = "#f2f4f7"
        TEXT_DIM = "#b5bbc5"
        TEXT_FAINT = "#858e9b"
        ON_LIGHT_RED = "#ff7b74"
        ON_LIGHT_BLUE = "#8fa8ff"
        ON_LIGHT_AMBER = "#e8d472"
        ON_LIGHT_MAGENTA = "#ef8bea"
        ON_LIGHT_CYAN = "#5fd0e0"
        ON_LIGHT_GREEN = "#74d68a"
    else:
        SURFACE = "#ffffff"
        SURFACE_SUB = "#f4f5f7"
        SURFACE_SUNK = "#eaecef"
        LINE = "#dcdfe4"
        LINE_SOFT = "#e9ebee"
        TEXT = "#14181d"
        TEXT_DIM = "#5c6570"
        TEXT_FAINT = "#8b949e"
        ON_LIGHT_RED = "#b3211d"
        ON_LIGHT_BLUE = "#2447c8"
        ON_LIGHT_AMBER = "#8a6b00"
        ON_LIGHT_MAGENTA = "#a32a9c"
        ON_LIGHT_CYAN = "#0e7c8c"
        ON_LIGHT_GREEN = "#17792f"

    return DARK_APPEARANCE


def pick_family(root, wanted: Tuple[str, ...], fallback: str) -> str:
    """First installed family from `wanted`.

    Tk silently substitutes an unknown family, which is how a window ends up in a font
    nobody chose; asking the font database first keeps the choice explicit.
    """
    try:
        from tkinter import font as tkfont              # noqa: PLC0415

        available = {name.lower() for name in tkfont.families(root)}
    except Exception:                                    # noqa: BLE001
        return fallback
    for name in wanted:
        if name.lower() in available:
            return name
    return fallback


@dataclass
class Fonts:
    """The window's type scale.  Two families, five sizes, no exceptions."""

    ui: tuple
    ui_bold: tuple
    small: tuple
    small_bold: tuple
    title: tuple
    mono: tuple
    mono_small: tuple

    @classmethod
    def resolve(cls, root) -> "Fonts":
        sans = pick_family(root, ("SF Pro Text", "Helvetica Neue", "Helvetica"),
                           "Helvetica")
        mono = pick_family(root, ("SF Mono", "Menlo", "Monaco", "Courier"), "Courier")
        return cls(
            ui=(sans, 13),
            ui_bold=(sans, 13, "bold"),
            small=(sans, 11),
            small_bold=(sans, 11, "bold"),
            title=(sans, 15, "bold"),
            mono=(mono, 12),
            mono_small=(mono, 11),
        )


def apply_ttk_styles(style, fonts: "Fonts") -> None:
    """Configure the ttk styles this window uses.

    Only the options aqua actually honours are set: fonts, padding, and layout.  A
    -background here would be silently dropped (see the module docstring), so setting
    one would be a lie in the source about what the window looks like.
    """
    # Agent view's notebook has no tab strip: in that mode the window IS the view.
    style.layout("Agent.TNotebook.Tab", [])
    style.configure("TNotebook", tabmargins=(6, 4, 6, 0))
    style.configure("TNotebook.Tab", font=fonts.small, padding=(12, 4))
    style.configure("Toolbutton", font=fonts.small, padding=(9, 3))
    style.configure("TButton", font=fonts.small)
    style.configure("Small.TButton", font=fonts.small, padding=(6, 1))
    # The floating chat panel needs an edge or it merges into the render behind it.
    style.configure("Panel.TFrame", relief="solid", borderwidth=1)


# ----------------------------------------------------------------- text formatting

_FENCE_RE = re.compile(r"^\s*```([A-Za-z0-9_+-]*)\s*$")
_INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")
_BULLET_RE = re.compile(r"^(\s*)([-*•]|\d+[.)])\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")


@dataclass
class Block:
    """One renderable run of a reply: prose, or a fenced code block."""

    kind: str                  # "prose" | "code"
    lines: List[str]
    language: str = ""

    def is_json(self) -> bool:
        """Whether to syntax-colour as JSON.

        Judged on the text, not on the fence's language tag: models label the design IR
        ```json, ```jsonc, ``` and occasionally ```JSON, and the tag is the one part of
        the answer nothing validates.
        """
        if self.language.lower() in ("json", "jsonc", "json5"):
            return True
        body = "\n".join(self.lines).strip()
        return body.startswith(("{", "[")) and body.endswith(("}", "]"))


def split_blocks(text: str) -> List[Block]:
    """Split a reply into prose and fenced code blocks.

    An unterminated fence is treated as running to the end of the message rather than
    being dropped: a truncated answer must still show the code it did produce.
    """
    blocks: List[Block] = []
    current: List[str] = []
    fence: Optional[str] = None

    for line in (text or "").splitlines():
        match = _FENCE_RE.match(line)
        if match and fence is None:
            if current:
                blocks.append(Block("prose", current))
                current = []
            fence = match.group(1)
            continue
        if match and fence is not None:
            blocks.append(Block("code", current, fence))
            current, fence = [], None
            continue
        current.append(line)

    if current:
        blocks.append(Block("code", current, fence or "") if fence is not None
                      else Block("prose", current))
    return blocks


@dataclass
class Span:
    """A styled run inside one prose line: (text, tag suffix)."""

    text: str
    style: str = ""            # "" | "strong" | "code"


def inline_spans(line: str) -> List[Span]:
    """Split **bold** and `code` out of a prose line."""
    spans: List[Span] = []
    for part in _INLINE_RE.split(line):
        if not part:
            continue
        if len(part) > 4 and part.startswith("**") and part.endswith("**"):
            spans.append(Span(part[2:-2], "strong"))
        elif len(part) > 2 and part.startswith("`") and part.endswith("`"):
            spans.append(Span(part[1:-1], "code"))
        else:
            spans.append(Span(part))
    return spans


def prose_line(line: str) -> Tuple[str, str, List[Span]]:
    """Classify one prose line.

    Returns (kind, prefix, spans) where kind is "text" | "bullet" | "heading" and
    prefix is the bullet glyph or "" -- the caller owns indentation, because a Text
    widget indents with tag margins rather than with spaces.
    """
    heading = _HEADING_RE.match(line)
    if heading:
        return ("heading", "", inline_spans(heading.group(2)))
    bullet = _BULLET_RE.match(line)
    if bullet:
        marker = bullet.group(2)
        glyph = "•  " if marker in ("-", "*", "•") else f"{marker}  "
        return ("bullet", glyph, inline_spans(bullet.group(3)))
    return ("text", "", inline_spans(line))


# JSON tokens, in the order they must be tried: a key is a string followed by a colon,
# so it has to win over the plain-string rule.
_JSON_TOKEN_RE = re.compile(
    r'(?P<key>"(?:[^"\\]|\\.)*"\s*:)'
    r'|(?P<str>"(?:[^"\\]|\\.)*")'
    r'|(?P<num>-?\b\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b)'
    r'|(?P<lit>\b(?:true|false|null)\b)'
    r'|(?P<punc>[\{\}\[\],:])'
)

_JSON_STYLES = {"key": "key", "str": "str", "num": "num", "lit": "lit",
                "punc": "punc"}


def json_spans(line: str) -> List[Span]:
    """Colour one JSON line.

    Deliberately a tokenizer over the RAW line rather than a re-serialisation of parsed
    JSON: the model's block is what the user is being asked to approve, so it has to be
    shown exactly as written -- including the trailing comma or stray key that will make
    /design save reject it.
    """
    spans: List[Span] = []
    pos = 0
    for match in _JSON_TOKEN_RE.finditer(line):
        if match.start() > pos:
            spans.append(Span(line[pos:match.start()]))
        kind = match.lastgroup or ""
        spans.append(Span(match.group(), _JSON_STYLES.get(kind, "")))
        pos = match.end()
    if pos < len(line):
        spans.append(Span(line[pos:]))
    return spans


def code_caption(block: Block) -> str:
    """The dim line above a code block: what it is, and how big.

    Says "design IR" only when the object really carries parts and nets, because that
    caption is the cue to run /design save and it must not appear over a snippet that
    the compiler would reject for having neither.
    """
    count = len(block.lines)
    body = "\n".join(block.lines)
    label = block.language.lower() or ("json" if block.is_json() else "text")
    if block.is_json():
        try:
            import json                                  # noqa: PLC0415

            parsed = json.loads(body)
        except Exception:                                 # noqa: BLE001
            parsed = None
        if isinstance(parsed, dict) and "parts" in parsed and "nets" in parsed:
            return (f"design IR  ·  {len(parsed['parts'])} parts, "
                    f"{len(parsed['nets'])} nets  ·  /design save to keep it")
    return f"{label}  ·  {count} line{'s' if count != 1 else ''}"
