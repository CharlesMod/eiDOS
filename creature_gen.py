"""Procedural creature generator — genome → body parts → render spec.

Pure functions, stdlib only, no I/O (glue.py discipline): dashboard.py owns
creature.json persistence; static/creature.js owns animation. This module just
turns a 64-bit seed into a deterministic morphology and composes fixed-width
ASCII grids the client animates in layers (body breath / appendage sway / eyes
/ mouth / fx are stamped client-side — eye and mouth cells are SPACES in the
base frames emitted here).

Determinism contract: genome_from_seed draws genes in a FIXED, documented
order. Never insert a draw mid-sequence — add new genes at the END and bump
GENOME_VERSION. (Belt and braces: the genome dict itself is persisted in
creature.json, so derivation drift can never mutate an existing creature.)

Glyph discipline: single-width characters only — ASCII plus the small set
already proven in the dashboard's <pre> (· ° ═ ◆ ✦ ▽). No emoji (double-width
breaks the grid).
"""

from __future__ import annotations

import hashlib
import random
from typing import TypedDict

GENOME_VERSION = 1

BODY_FAMILIES = ("round", "box", "blob", "tall", "crystal")
EYE_FAMILY_NAMES = ("dot", "ring", "glow", "slit", "star")
MOUTH_SET_NAMES = ("cat", "flat", "fang", "wave")
EAR_KIND_NAMES = ("none", "cat", "antennae", "horns", "fins")
LIMB_KIND_NAMES = ("stubby", "long", "wings", "none")
TAIL_KIND_NAMES = ("none", "curl", "spike", "wisp")
ACCENTS = ("#00ff41", "#ffb000", "#33bbff", "#aa88ff")
PARTICLE_AFFINITIES = {"dots": "· ·", "sparks": "* ·", "stars": "✦ ·", "static": "' ."}
EGG_PATTERN_NAMES = ("speckle", "zigzag", "band", "swirl")

STAGES = ("egg", "hatchling", "juvenile", "adult", "guardian")
# Base canvas (w, h) per stage; rows 0-1 are the fx/ear margin, body sits below.
CANVAS = {"egg": (9, 5), "hatchling": (11, 6), "juvenile": (13, 7),
          "adult": (15, 8), "guardian": (17, 10)}

# Whitelist enforced by tests: every emitted character must be in here.
APPROVED_GLYPHS = frozenset(
    " .,'\"`-_=~^*+<>(){}[]/\\|!?#@:;%&$0123456789"
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "·°═◆✦▽zZ"
)


class Genome(TypedDict):
    v: int
    seed: int
    body: str
    eyes: str
    mouth: str
    ears: str
    limbs: str
    tail: str
    size: int          # -1 narrow | 0 normal | +1 wide (w += 2*size)
    accent: str
    particle: str      # key into PARTICLE_AFFINITIES
    egg_pattern: str
    blink_ms: int
    saccade_ms: int
    sway_amp: int      # 0..2 — how many sway frames appendages actually use
    breath_ms: int
    micro_ms: int


def genome_from_seed(seed: int) -> Genome:
    """Deterministic genome. Draw order is FROZEN (see module docstring)."""
    rng = random.Random(seed)
    return Genome(
        v=GENOME_VERSION,
        seed=seed,
        body=rng.choice(BODY_FAMILIES),            # draw 1
        eyes=rng.choice(EYE_FAMILY_NAMES),         # draw 2
        mouth=rng.choice(MOUTH_SET_NAMES),         # draw 3
        ears=rng.choice(EAR_KIND_NAMES),           # draw 4
        limbs=rng.choice(LIMB_KIND_NAMES),         # draw 5
        tail=rng.choice(TAIL_KIND_NAMES),          # draw 6
        size=rng.choice((-1, 0, 0, 1)),            # draw 7 (normal twice as likely)
        accent=rng.choice(ACCENTS),                # draw 8
        particle=rng.choice(tuple(PARTICLE_AFFINITIES)),  # draw 9
        egg_pattern=rng.choice(EGG_PATTERN_NAMES), # draw 10
        blink_ms=rng.randint(2200, 5200),          # draw 11
        saccade_ms=rng.randint(3500, 8000),        # draw 12
        sway_amp=rng.randint(0, 2),                # draw 13
        breath_ms=rng.randint(2600, 4200),         # draw 14
        micro_ms=rng.randint(8000, 16000),         # draw 15
    )


def stage_for(level: int, hatched: bool) -> str:
    """Same level thresholds as the legacy ascii_art._stage; egg only pre-hatch."""
    if level <= 2:
        return "hatchling" if hatched else "egg"
    if level <= 4:
        return "juvenile"
    if level <= 7:
        return "adult"
    return "guardian"


# --- Part libraries -------------------------------------------------------

EYE_FAMILIES = {
    "dot":  {"open": "o", "closed": "-", "look_l": "o", "look_r": "o",
             "happy": "^", "dead": "x", "strain": ">", "low": "-"},
    "ring": {"open": "O", "closed": "-", "look_l": "o", "look_r": "o",
             "happy": "^", "dead": "x", "strain": ">", "low": "o"},
    "glow": {"open": "°", "closed": "·", "look_l": "°", "look_r": "°",
             "happy": "^", "dead": "x", "strain": "°", "low": "·"},
    "slit": {"open": "=", "closed": "-", "look_l": "<", "look_r": ">",
             "happy": "^", "dead": "x", "strain": ">", "low": "_"},
    "star": {"open": "*", "closed": "+", "look_l": "*", "look_r": "*",
             "happy": "^", "dead": "x", "strain": "+", "low": "·"},
}

MOUTH_SETS = {
    "cat":  {"idle": "=^=", "smile": "=▽=", "frown": "=~=", "grit": "=#=",
             "sleep": "=.=", "speak": ["=o=", "=O=", "=-=", "=o="]},
    "flat": {"idle": "---", "smile": "-▽-", "frown": "-~-", "grit": "-#-",
             "sleep": "-.-", "speak": ["-o-", "-O-", "---", "-o-"]},
    "fang": {"idle": "=w=", "smile": "=W=", "frown": "=m=", "grit": "=#=",
             "sleep": "=.=", "speak": ["=o=", "=0=", "=w=", "=o="]},
    "wave": {"idle": "~-~", "smile": "~▽~", "frown": "~~~", "grit": "~#~",
             "sleep": "~.~", "speak": ["~o~", "~O~", "~-~", "~o~"]},
}

# Appendage glyph frames: per kind, a list of sway frames; each frame is one
# glyph per cell. The client clamps the frame index by the sway_amp gene.
_EAR_FRAMES = {
    "cat":      [["/", "\\"], ["/", "\\"], ["/", "|"]],
    "antennae": [["\\", "/"], ["|", "|"], ["/", "\\"]],
    "horns":    [["\\", "/"], ["\\", "/"], ["|", "|"]],
    "fins":     [["<", ">"], ["{", "}"], ["<", ">"]],
}
_LIMB_FRAMES = {
    "stubby": [["(", ")"], ["(", ")"], ["<", ">"]],
    "long":   [["/", "\\"], ["|", "|"], ["\\", "/"]],
    "wings":  [["{", "}"], ["(", ")"], ["{", "}"]],
}
_TAIL_FRAMES = {
    "curl":  [["~"], ["-"], ["~"]],
    "spike": [["/"], ["|"], ["/"]],
    "wisp":  [["°"], ["·"], ["°"]],
}

# Body outline kits: (top_l, top_fill, top_r, side_l, side_r, bot_l, bot_fill, bot_r)
_BODY_KITS = {
    "round":   (".", "-", ".", "(", ")", "'", "-", "'"),
    "box":     (".", "=", ".", "[", "]", "'", "=", "'"),
    "blob":    (",", "~", ",", "{", "}", "'", "~", "'"),
    "tall":    ("/", "-", "\\", "|", "|", "\\", "_", "/"),
    "crystal": ("/", "═", "\\", "<", ">", "\\", "═", "/"),
}


# --- Composition ----------------------------------------------------------

def _blank(w: int, h: int) -> list[list[str]]:
    return [[" "] * w for _ in range(h)]


def _stamp(grid: list[list[str]], r: int, c: int, text: str) -> None:
    for i, ch in enumerate(text):
        if 0 <= r < len(grid) and 0 <= c + i < len(grid[0]):
            grid[r][c + i] = ch


def _draw_body(family: str, w: int, h: int, inhale: bool) -> list[list[str]]:
    """Body outline on a w×h canvas: rows 0-1 are margin, body fills 2..h-1.
    The inhale frame pushes the side walls outward one column on middle rows."""
    tl, tf, tr, sl, sr, bl, bf, br = _BODY_KITS[family]
    grid = _blank(w, h)
    top, bot = 2, h - 1
    left, right = 1, w - 2
    _stamp(grid, top, left, tl + tf * (right - left - 1) + tr)
    _stamp(grid, bot, left, bl + bf * (right - left - 1) + br)
    for r in range(top + 1, bot):
        bulge = 1 if (inhale and top + 1 < r < bot - 1) else 0
        grid[r][max(0, left - bulge)] = sl
        grid[r][min(w - 1, right + bulge)] = sr
    if family == "crystal":
        grid[top][w // 2] = "◆"
    return grid


def _anchors(w: int, h: int) -> dict:
    """Computed feature positions for a body canvas (see _draw_body geometry)."""
    top, bot = 2, h - 1
    center = w // 2
    gap = max(1, w // 6)
    eye_row = top + max(1, (bot - top) // 3)
    return {
        "eye_row": eye_row,
        # Sockets span 2 cells and extend OUTWARD from mirrored rest positions:
        # left pupil rests at socket cell 1 (col center-gap), right at cell 0
        # (col center+gap) — saccades shift both pupils toward cell 0 or cell 1.
        "eye_l": [eye_row, center - gap - 1],
        "eye_r": [eye_row, center + gap],
        "mouth": [min(eye_row + 1, bot - 1), center - 1],
        "ear_l": [[1, 2], [1, 3]],
        "ear_r": [[1, w - 4], [1, w - 3]],
        "limb_row": min(top + (bot - top) // 2 + 1, bot - 1),
        "tail": [bot, w - 1],
    }


# Guardian-stage appendages RESTRUCTURE, not just persist (Phase B metamorphosis):
# ears grow a second vertical cell, tails gain a tip segment.
_TAIL_TIPS = {"curl": "~", "spike": "|", "wisp": "·"}


def _appendages(genome: Genome, stage: str, w: int, h: int) -> list[dict]:
    """Appendage overlay records. Their cells are SPACES in the base frames.
    Progression: hatchling = none; juvenile = ears+tail; adult+ = ears+tail+limbs;
    guardian = taller ears + two-segment tail (visible metamorphosis payoff)."""
    if stage == "hatchling":
        return []
    a = _anchors(w, h)
    rng = random.Random(genome["seed"] + 7)   # deterministic per-appendage periods
    guardian = stage == "guardian"
    out: list[dict] = []
    if genome["ears"] != "none":
        fr = _EAR_FRAMES[genome["ears"]]
        lc, rc = a["ear_l"][0], a["ear_r"][1]
        l_cells = [lc] + ([[0, lc[1]]] if guardian else [])
        r_cells = [rc] + ([[0, rc[1]]] if guardian else [])
        out.append({"name": "ear_l", "cells": l_cells,
                    "frames": [[f[0]] * len(l_cells) for f in fr],
                    "period_ms": rng.randint(1500, 2400)})
        out.append({"name": "ear_r", "cells": r_cells,
                    "frames": [[f[1]] * len(r_cells) for f in fr],
                    "period_ms": rng.randint(1500, 2400)})
    if genome["tail"] != "none":
        fr = _TAIL_FRAMES[genome["tail"]]
        cells = [a["tail"]]
        frames = [list(f) for f in fr]
        if guardian:
            cells.append([a["tail"][0] - 1, a["tail"][1]])
            tip = _TAIL_TIPS[genome["tail"]]
            frames = [f + [tip] for f in frames]
        out.append({"name": "tail", "cells": cells, "frames": frames,
                    "period_ms": rng.randint(1900, 2800)})
    if stage in ("adult", "guardian") and genome["limbs"] != "none":
        fr = _LIMB_FRAMES[genome["limbs"]]
        row = a["limb_row"]
        out.append({"name": "limb_l", "cells": [[row, 0]],
                    "frames": [[f[0]] for f in fr], "period_ms": rng.randint(1700, 2600)})
        out.append({"name": "limb_r", "cells": [[row, w - 1]],
                    "frames": [[f[1]] for f in fr], "period_ms": rng.randint(1700, 2600)})
    return out


def _grid_to_lines(grid: list[list[str]]) -> list[str]:
    return ["".join(row) for row in grid]


def compose(genome: Genome, stage: str) -> dict:
    """Body + anchors for a non-egg stage: 2 breath frames with eye/mouth/appendage
    cells left blank (the client stamps those layers)."""
    base_w, h = CANVAS[stage]
    w = max(9, base_w + 2 * genome["size"])
    frames = []
    appendages = _appendages(genome, stage, w, h)
    a = _anchors(w, h)
    for inhale in (False, True):
        grid = _draw_body(genome["body"], w, h, inhale)
        if stage == "guardian":
            _stamp(grid, 1, w // 2 - 1, "═✦═")
        # Feature cells stay blank in the base — they are client-side layers.
        for c in (a["eye_l"][1], a["eye_l"][1] + 1, a["eye_r"][1], a["eye_r"][1] + 1):
            grid[a["eye_row"]][c] = " "
        _stamp(grid, a["mouth"][0], a["mouth"][1], "   ")
        for ap in appendages:
            for (r, c) in ap["cells"]:
                grid[r][c] = " "
        frames.append(_grid_to_lines(grid))
    return {
        "w": w, "h": h, "base": frames,
        "eyes": {"l": a["eye_l"], "r": a["eye_r"], "socket": 2,
                 "family": genome["eyes"], "glyphs": EYE_FAMILIES[genome["eyes"]]},
        "mouth": {"row": a["mouth"][0], "col": a["mouth"][1], "len": 3,
                  "glyphs": MOUTH_SETS[genome["mouth"]]},
        "appendages": appendages,
    }


def compose_egg(genome: Genome, progress: float) -> dict:
    """Patterned egg with deterministic cracks; k cracks = floor(progress · total)."""
    w, h = CANVAS["egg"]
    rng = random.Random(genome["seed"] + 31)
    interior = [(r, c) for r in range(1, h - 1) for c in range(2, w - 2)]
    pattern = genome["egg_pattern"]
    fills = {"speckle": "· .", "zigzag": "^v", "band": "=-", "swirl": "@·"}[pattern]
    marks = rng.sample(interior, k=min(6, len(interior)))
    cracks = rng.sample(interior, k=4)
    crack_glyphs = ["\\", "/", "X", "/"]
    k = int(max(0.0, min(1.0, progress)) * len(cracks))
    frames = []
    for wob in (0, 1):
        grid = _blank(w, h)
        _stamp(grid, 0, 2, ".---.")
        for r in range(1, h - 1):
            grid[r][1] = "("
            grid[r][w - 2] = ")"
        _stamp(grid, h - 1, 2, "'---'")
        for i, (r, c) in enumerate(marks):
            if pattern == "band" and r != h // 2:
                continue
            grid[r][c] = fills[(i + wob) % len(fills)].strip() or "·"
        for i in range(k):
            r, c = cracks[i]
            grid[r][c] = crack_glyphs[i]
        frames.append(_grid_to_lines(grid))
    return {"w": w, "h": h, "base": frames, "eyes": None, "mouth": None,
            "appendages": []}


def compose_cocoon(genome: Genome, stage: str) -> dict:
    """The chrysalis a creature wraps into during a stage metamorphosis. Sized to
    the NEW stage's canvas so emergence doesn't jump the layout; patterned like
    the creature's egg (same genome fills) — lineage shows in the silk."""
    base_w, h = CANVAS[stage]
    w = max(9, base_w + 2 * genome["size"])
    rng = random.Random(genome["seed"] + 53)
    fills = {"speckle": "·.", "zigzag": "^v", "band": "=-", "swirl": "@·"}[genome["egg_pattern"]]
    top, bot = 1, h - 1
    left = max(2, w // 2 - 3)
    right = min(w - 3, w // 2 + 3)
    interior = [(r, c) for r in range(top + 1, bot) for c in range(left + 1, right)]
    marks = rng.sample(interior, k=min(7, len(interior)))
    frames = []
    for wob in (0, 1):
        grid = _blank(w, h)
        grid[0][w // 2] = "|"                     # silk thread
        _stamp(grid, top, left, "." + "=" * (right - left - 1) + ".")
        for r in range(top + 1, bot):
            grid[r][left] = "("
            grid[r][right] = ")"
        _stamp(grid, bot, left, "'" + "-" * (right - left - 1) + "'")
        for i, (r, c) in enumerate(marks):
            grid[r][c] = fills[(i + wob) % len(fills)]
        frames.append(_grid_to_lines(grid))
    return {"w": w, "h": h, "base": frames, "eyes": None, "mouth": None,
            "appendages": []}


def build_spec(genome: Genome, stage: str, hatch: dict, expr: dict) -> dict:
    """The full payload /api/status ships to the client compositor."""
    parts = compose_egg(genome, hatch.get("progress", 0.0)) if stage == "egg" \
        else compose(genome, stage)
    return {
        "v": 1,
        "id": hashlib.sha256(str(genome["seed"]).encode()).hexdigest()[:8],
        "stage": stage,
        "accent": genome["accent"],
        "particles": PARTICLE_AFFINITIES[genome["particle"]],
        "anim": {
            "fps": 10,
            "breath_ms": genome["breath_ms"],
            "blink_ms": genome["blink_ms"],
            "blink_jitter_ms": 1500,
            "saccade_ms": genome["saccade_ms"],
            "micro_ms": genome["micro_ms"],
            "sway_amp": genome["sway_amp"],
        },
        "hatch": dict(hatch),
        "expr": dict(expr),
        **parts,
    }
