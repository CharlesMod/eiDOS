"""ASCII creature sprites for the eiDOS buddy dashboard.

Sprites are organized by evolution stage (derived from level) and mood.
Each sprite is a multiline string. The dashboard renders these in a <pre> block
with CSS animation cycling between frames for breathing/blinking.

get_creature() is the sole public interface.
"""

# --- Evolution stage from level ---

def _stage(level: int) -> str:
    if level <= 2:
        return "seed"
    if level <= 4:
        return "sprout"
    if level <= 7:
        return "creature"
    return "guardian"


# --- Seed stage (Lv.1-2): tiny, minimal ---

_SEED = {
    "focused": [
        r"""
    *
   ·+·
    '
""",
        r"""
    ·
   ·+·
    '
""",
    ],
    "curious": [
        r"""
    ?
   ·+·
    '
""",
        r"""
    ·
   ·+·
    '
""",
    ],
    "determined": [
        r"""
    *
   ·+·
    '
""",
        r"""
    ·
   ·+·
    ,
""",
    ],
    "triumphant": [
        r"""
   \*/
   ·+·
    '
""",
        r"""
   \·/
   ·+·
    '
""",
    ],
    "frustrated": [
        r"""
    ~
   ·+·
    .
""",
        r"""
    .
   ·+·
    .
""",
    ],
    "struggling": [
        r"""
    .
   .+.
    .
""",
        r"""

   .+.
    .
""",
    ],
}

# --- Sprout stage (Lv.3-4): small creature, expressive ---

_SPROUT = {
    "focused": [
        r"""
   ╭─╮
  (° °)
   /||\
   / \
""",
        r"""
   ╭─╮
  (°.°)
   /||\
   / \
""",
    ],
    "curious": [
        r"""
   ╭─╮
  (O O)
   /||\
   / \
""",
        r"""
   ╭─╮
  (o O)
   /||\
   / \
""",
    ],
    "determined": [
        r"""
   ╭─╮
  (= =)
   /||\
   / \
""",
        r"""
   ╭─╮
  (=.=)
   /||\
   / \
""",
    ],
    "triumphant": [
        r"""
  \╭─╮/
  (^ ^)
   /||\
   / \
""",
        r"""
   ╭─╮
  (^▽^)
   \||/
   / \
""",
    ],
    "frustrated": [
        r"""
   ╭─╮
  (>.<)
   /||\
   / \
""",
        r"""
   ╭─╮
  (> <)
   /||\
   / \
""",
    ],
    "struggling": [
        r"""
   ╭─╮
  (-.-)  '
   /||\
   / \
""",
        r"""
   ╭─╮
  (-. )  '
   /||\
   / \
""",
    ],
}

# --- Creature stage (Lv.5-7): detailed, trait-influenced ---

_CREATURE = {
    "focused": [
        r"""
  /\   /\
 ( o . o )
 (  =^=  )
  )     (
 (       )
  '-----'
""",
        r"""
  /\   /\
 ( o   o )
 (  =^=  )
  )     (
 (       )
  '-----'
""",
    ],
    "curious": [
        r"""
  /\   /\
 ( O . o )
 (  =^=  )
  )     (
 (       )
  '-----'
""",
        r"""
  /\   /\
 ( o . O )
 (  =^=  )
  )     (
 (       )
  '-----'
""",
    ],
    "determined": [
        r"""
  /\   /\
 ( - . - )
 (  =^=  )
  )     (
 (       )
  '-----'
""",
        r"""
  /\   /\
 ( -   - )
 (  =^=  )
  )     (
 (       )
  '-----'
""",
    ],
    "triumphant": [
        r"""
    ✦
  /\   /\
 ( ^ . ^ )
 (  =▽=  )
  ) ✦   (
 (       )
  '-----'
""",
        r"""
   ✦ ✦
  /\   /\
 ( ^   ^ )
 (  =▽=  )
  )     (
 (  ✦    )
  '-----'
""",
    ],
    "frustrated": [
        r"""
  /\   /\
 ( > . < )
 (  =~=  )
  )     (
 (       )
  '-----'
""",
        r"""
  /\   /\
 ( >   < )
 (  =#=  )
  )     (
 (       )
  '-----'
""",
    ],
    "struggling": [
        r"""
     '
  /\   /\
 ( - _ - )
 (  =.=  )
  )     (
 (       )
  '-----'
""",
        r"""
    ' '
  /\   /\
 ( -   - )
 (  =.=  )
  )     (
 (       )
  '-----'
""",
    ],
}

# --- Guardian stage (Lv.8+): elaborate, regal ---

_GUARDIAN = {
    "focused": [
        r"""
  ╔══✦══╗
  ║ ◆ ◆ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  o   o  )
 (  ═^═  )
  '══╧══'
""",
        r"""
  ╔══✦══╗
  ║ ◆.◆ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  o . o  )
 (  ═^═  )
  '══╧══'
""",
    ],
    "curious": [
        r"""
  ╔══✦══╗
  ║ ◆ ◇ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  O   o  )
 (  ═^═  )
  '══╧══'
""",
        r"""
  ╔══✦══╗
  ║ ◇ ◆ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  o   O  )
 (  ═^═  )
  '══╧══'
""",
    ],
    "determined": [
        r"""
  ╔══✦══╗
  ║ ◆ ◆ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  =   =  )
 (  ═^═  )
  '══╧══'
""",
        r"""
  ╔══✦══╗
  ║ ◆.◆ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  = . =  )
 (  ═^═  )
  '══╧══'
""",
    ],
    "triumphant": [
        r"""
    ★  ✦  ★
  ╔══✦══╗
  ║ ◆ ◆ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  ^   ^  )
 (  ═▽═  )
  '══╧══'
""",
        r"""
   ✦  ★  ✦
  ╔══✦══╗
  ║ ◆ ◆ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  ^ . ^  )
 (  ═▽═  )
  '══╧══'
""",
    ],
    "frustrated": [
        r"""
  ╔══·══╗
  ║ ◆ ◆ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  >   <  )
 (  ═~═  )
  '══╧══'
""",
        r"""
  ╔══·══╗
  ║ ◆.◆ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  > . <  )
 (  ═#═  )
  '══╧══'
""",
    ],
    "struggling": [
        r"""
       '
  ╔══·══╗
  ║ ◇ ◇ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  -   -  )
 (  ═.═  )
  '══╧══'
""",
        r"""
      ' '
  ╔══·══╗
  ║ ◇.◇ ║
  ╚═╤═╤═╝
 /\  | |  /\
(  - . -  )
 (  ═.═  )
  '══╧══'
""",
    ],
}

# --- Special states (override mood) ---

_SLEEPING = r"""
        z
       Z
  /\   /\
 ( -   - )  Z
 (  =.=  )
  )     (
 (       )
  '-----'
"""

_THINKING = [
    r"""
         ···
  /\   /\
 ( °   ° )
 (  =^=  )
  )     (
 (       )
  '-----'
""",
    r"""
        ····
  /\   /\
 ( °   ° )
 (  =^=  )
  )     (
 (       )
  '-----'
""",
    r"""
       ·····
  /\   /\
 ( °   ° )
 (  =^=  )
  )     (
 (       )
  '-----'
""",
]

_THERMAL = r"""
      ~ ~ ~
  /\   /\
 ( @   @ )
 (  =P=  )
  )  ~  (
 (       )
  '-----'
"""

_DEAD = r"""
  /\   /\
 ( x   x )
 (  =.=  )
  )     (
 (       )
  '-----'
"""

_STAGE_MAP = {
    "seed": _SEED,
    "sprout": _SPROUT,
    "creature": _CREATURE,
    "guardian": _GUARDIAN,
}

# Fallback mood order — if exact mood not found, try these
_MOOD_FALLBACK = {
    "triumphant": "focused",
    "frustrated": "determined",
    "struggling": "frustrated",
    "curious": "focused",
    "determined": "focused",
    "focused": "determined",
}


def get_creature(level: int, mood: str, traits: list = None,
                 special: str = None) -> dict:
    """Return creature data for the dashboard.

    Args:
        level: persona level (determines evolution stage)
        mood: persona mood string
        traits: list of trait names (for future visual modifiers)
        special: override state — "sleeping", "thinking", "thermal", "dead"

    Returns:
        dict with keys:
            frames: list[str] — ASCII art frames
            interval_ms: int — suggested animation interval
            stage: str — evolution stage name
            particles: str — suggested particle characters for CSS effects
    """
    if special == "sleeping":
        return {"frames": [_SLEEPING.strip('\n')], "interval_ms": 2000, "stage": _stage(level), "particles": "z Z"}
    if special == "thinking":
        return {"frames": [f.strip('\n') for f in _THINKING], "interval_ms": 800, "stage": _stage(level), "particles": "· ·"}
    if special == "thermal":
        return {"frames": [_THERMAL.strip('\n')], "interval_ms": 500, "stage": _stage(level), "particles": "~ ~"}
    if special == "dead":
        return {"frames": [_DEAD.strip('\n')], "interval_ms": 0, "stage": "dead", "particles": ""}

    stage = _stage(level)
    sprites = _STAGE_MAP.get(stage, _CREATURE)

    # Look up mood, with fallback
    frames = sprites.get(mood)
    if not frames:
        fallback = _MOOD_FALLBACK.get(mood, "focused")
        frames = sprites.get(fallback, list(sprites.values())[0])

    interval = 1500 if stage in ("seed", "sprout") else 1200

    # Particle characters by mood
    particle_map = {
        "triumphant": "✦ ★ *",
        "focused": "· ·",
        "determined": "·",
        "frustrated": "# ~",
        "struggling": "' .",
        "curious": "? ·",
    }
    particles = particle_map.get(mood, "·")

    return {
        "frames": [f.strip('\n') for f in frames],
        "interval_ms": interval,
        "stage": stage,
        "particles": particles,
    }

# self-edit pipeline test marker (auto-removed)
