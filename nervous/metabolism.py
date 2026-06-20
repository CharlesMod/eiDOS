"""M0 — Metabolism: the creature's energy economy (the organism's stakes).

The first fresh-creature night proved survival / identity / calm — but the creature RUMINATED (5067
thoughts, ~3 actions): a free creature with no STAKES reverts to its training and just thinks. *A robot
that sits on its charger all day is a flawed being.* Metabolism adds genuine scarcity: an **energy
reserve** that DRAINS with living — and with cognition most of all (an LLM "thought" is the dearest
metabolic act, the GPU at full tilt) — and is restored by REST (later: nourishing acts in M1, real
solar power in M4).

Low energy is FELT as **hunger** — a felt bar that is NOT baseline (unlike VRAM), so a depleting
reserve genuinely worsens the body-feeling and rides the existing wellbeing→reward homeostatic loop
(`felt.py` + `reward.py W_FELT`). The creature is already rewarded for keeping its body well, so empty
rumination (cognition cost, no nourishment) becomes a net loss it learns to avoid — no scripted "stop
ruminating" rule; the behavior falls out of the energy economy (loops, not guardrails).

This organ only tracks/recovers/publishes energy. It never acts, never blocks the tick, fully guarded.
Tiredness→sleep coupling (M0.3) and the nutrient feeds (M1) wire in separately; `feed()` is the seam
they'll call.
"""
import json
import os
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION

# Per-tick energy costs / recovery (defaults; all overridable via config). Thinking is the dearest act.
BASAL_DRAIN = 0.001       # just being alive, per tick
COGNITION_DRAIN = 0.004   # one LLM "thought" — the dearest metabolic event
ACTION_DRAIN = 0.002      # a world-touching tool action (costs, but M1 lets such acts also NOURISH)
REST_RECOVERY = 0.02      # resting / sleeping restores far faster than activity drains

# Hunger (= 1 - energy) -> felt-bar level. Low energy = high hunger = stress (NON-baseline in felt.py).
HUNGER_ELEVATED = 0.30
HUNGER_HIGH = 0.55
HUNGER_CRITICAL = 0.80


def hunger_to_bar(hunger):
    """Map hunger (0=full .. 1=empty) to a felt bar level. Higher hunger = higher pressure."""
    if hunger >= HUNGER_CRITICAL:
        return "critical"
    if hunger >= HUNGER_HIGH:
        return "high"
    if hunger >= HUNGER_ELEVATED:
        return "elevated"
    return "ok"


class Metabolism:
    """The energy reserve. `metabolize()` once per tick (drain or recover); `feed()` for nourishment;
    `hunger_bar()` feeds the felt-state. Energy persists across restarts — a tired creature wakes tired."""

    def __init__(self, bus=None, *, config=None, source="metabolism", start_energy=0.8,
                 basal=BASAL_DRAIN, cognition=COGNITION_DRAIN, action=ACTION_DRAIN,
                 recovery=REST_RECOVERY, state_path=None, save_every=20):
        self.bus = bus
        self.config = config
        self.source = source
        self.basal = float(basal)
        self.cognition = float(cognition)
        self.action = float(action)
        self.recovery = float(recovery)
        self.energy = max(0.0, min(1.0, float(start_energy)))
        self.save_every = int(save_every)
        self._since_save = 0
        self._lock = threading.Lock()
        if state_path is None and config is not None:
            try:
                state_path = str(config.state_dir / "metabolism.json")
            except Exception:  # noqa: BLE001
                state_path = None
        self.state_path = state_path
        self._load()

    # ---- the per-tick energy step ----------------------------------------------------
    def metabolize(self, *, thought=True, acted=False, resting=False):
        """One tick of the energy economy. Living costs energy; thinking costs most; acting costs (M1
        lets acts nourish too); resting recovers. Returns energy SPENT this tick (>0 drain, <0 gain)."""
        with self._lock:
            before = self.energy
            if resting:
                self.energy = min(1.0, self.energy + self.recovery)
            else:
                drain = self.basal
                if thought:
                    drain += self.cognition
                if acted:
                    drain += self.action
                self.energy = max(0.0, self.energy - drain)
            spent = before - self.energy
            self._since_save += 1
            do_save = self._since_save >= self.save_every
            if do_save:
                self._since_save = 0
        self._publish()
        if do_save:
            self._save()
        return round(spent, 5)

    def feed(self, amount):
        """Nourishment restores energy (M1: learning progress, mastery, connection, exploration; M4:
        real solar charge). Clamped to [0,1]. Returns the new energy."""
        with self._lock:
            self.energy = max(0.0, min(1.0, self.energy + float(amount)))
            e = self.energy
        self._publish()
        return round(e, 4)

    # ---- read surfaces ---------------------------------------------------------------
    def hunger(self):
        with self._lock:
            return round(1.0 - self.energy, 4)

    def hunger_bar(self):
        """The felt bar interoception folds into the body-feeling (NON-baseline → it CAN distress)."""
        return hunger_to_bar(self.hunger())

    def snapshot(self):
        with self._lock:
            e = self.energy
        h = round(1.0 - e, 4)
        return {"energy": round(e, 4), "hunger": h, "bar": hunger_to_bar(h)}

    # ---- bus projection (retained; the monitor + any reader see the current energy) --
    def _publish(self):
        if self.bus is None:
            return
        try:
            payload = json.dumps(self.snapshot(), ensure_ascii=False).encode("utf-8")
            ev = NervousEvent(SCHEMA_VERSION, self.source, Kind.metabolism, Modality.intero,
                              Delivery.retained, salience=self.hunger(), t=time.monotonic())
            self.bus.publish(ev, payload)
        except Exception:  # noqa: BLE001
            pass

    # ---- persistence (energy carries across restarts; never raises) ------------------
    def _load(self):
        if self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    d = json.load(f)
                self.energy = max(0.0, min(1.0, float(d.get("energy", self.energy))))
            except Exception:  # noqa: BLE001
                pass

    def _save(self):
        if not self.state_path:
            return
        tmp = f"{self.state_path}.tmp"
        try:
            with self._lock:
                data = json.dumps({"energy": self.energy})
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
        except Exception:  # noqa: BLE001
            return
        for _ in range(40):   # Windows: dst may be briefly open for read (atomicio pattern)
            try:
                os.replace(tmp, self.state_path)
                return
            except PermissionError:
                time.sleep(0.02)
            except Exception:  # noqa: BLE001
                return
