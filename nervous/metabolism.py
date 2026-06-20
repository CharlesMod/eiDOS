"""M0 — Metabolism: the creature's energy economy (the organism's stakes).

The first fresh-creature night proved survival / identity / calm — but the creature RUMINATED (5067
thoughts, ~3 actions): a free creature with no STAKES reverts to its training and just thinks. *A robot
that sits on its charger all day is a flawed being.* Metabolism adds genuine scarcity: an **energy
reserve** that DRAINS with living — and with cognition most of all (an LLM "thought" is the dearest
metabolic act, the GPU at full tilt).

**Food = literal energy = available battery power (Dean, 2026-06-20 pivot).** The abstract-nutrient idea
(learning-progress / mastery AS food) was retired from the ENERGY economy — it was a rabbit hole. The
reserve IS the charge, and recharge comes from real power. How energy comes IN depends on the organism
ARCHETYPE:
  - **plant** (this stationary, solar-powered node) — autotroph: recharges PASSIVELY from environmental
    power (`charge_in`, e.g. solar by day). It does not nap to refill; at night it husbands its reserve.
  - **animal** — recharges by RESTING in place / docking (the `recovery` term while `resting`), i.e. a
    mobile bot that sleeps at its charger. `charge_in` still applies if it's docked under power.
The real power signal is the **Renogy Rover 20A Bluetooth** (SOC + PV watts); until that BLE reader
exists, a plant uses `solar_charge_in()` — a simple daylight-curve placeholder.

Low energy is FELT as **hunger** — a felt bar that is NOT baseline (unlike VRAM), so a depleting
reserve genuinely worsens the body-feeling and rides the existing wellbeing→reward homeostatic loop
(`felt.py` + `reward.py W_FELT`). Empty rumination (cognition cost, no power in) becomes a net loss the
creature learns to avoid — no scripted "stop ruminating" rule; it falls out of the economy (loops, not
guardrails).

This organ only tracks/recharges/publishes energy. It never acts, never blocks the tick, fully guarded.
"""
import json
import math
import os
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION

# Per-tick energy costs / recovery (defaults; all overridable via config). Thinking is the dearest act.
BASAL_DRAIN = 0.001       # just being alive, per tick
COGNITION_DRAIN = 0.004   # one LLM "thought" — the dearest metabolic event
ACTION_DRAIN = 0.002      # a world-touching tool action
REST_RECOVERY = 0.02      # an ANIMAL resting/docked recharges; a PLANT does not (it uses charge_in)

# Solar placeholder (until the Renogy BLE reader lands). A daylight triangle: zero before sunrise /
# after sunset, peaking at solar noon. Per-tick charge rate, so it composes with the per-tick drains.
SOLAR_PEAK = 0.03         # per-tick charge at solar noon (must out-pace daytime drain to net-charge)
SOLAR_SUNRISE_H = 6.0
SOLAR_SUNSET_H = 20.0


def solar_charge_in(hour, *, peak=SOLAR_PEAK, sunrise=SOLAR_SUNRISE_H, sunset=SOLAR_SUNSET_H):
    """Interim plant power source: per-tick solar charge for a given local hour (float, 0..24).
    A smooth half-sine over the daylight window, zero at night. Replaced by the Renogy PV reading."""
    if hour <= sunrise or hour >= sunset or sunset <= sunrise:
        return 0.0
    frac = (hour - sunrise) / (sunset - sunrise)   # 0..1 across the day
    return max(0.0, float(peak) * math.sin(math.pi * frac))

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
                 recovery=REST_RECOVERY, archetype="animal", state_path=None, save_every=20):
        self.bus = bus
        self.config = config
        self.source = source
        self.basal = float(basal)
        self.cognition = float(cognition)
        self.action = float(action)
        self.recovery = float(recovery)
        # plant = recharges from environmental power (charge_in) only; animal = also recharges by
        # resting/docking. The general class defaults to "animal" (intuitive nap-recovers organism);
        # the eidos node runs as a "plant" via config (stationary + solar).
        self.archetype = str(archetype or "animal")
        self.energy = max(0.0, min(1.0, float(start_energy)))
        self._anchored_at = 0.0   # monotonic time of the last real-SOC anchor (0 = never)
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
    def metabolize(self, *, thought=True, acted=False, resting=False, charge_in=0.0):
        """One tick of the energy economy. Living costs energy; thinking costs most; acting costs. Power
        comes IN as `charge_in` (environmental power — a plant's solar; an animal's dock), and — for an
        ANIMAL only — from resting/docking (`recovery`). A resting organism is dormant: it pays basal
        but not cognition/action. Returns energy SPENT this tick (>0 net drain, <0 net gain)."""
        with self._lock:
            before = self.energy
            drain = self.basal
            if not resting:                 # dormant body spends nothing on thought/action
                if thought:
                    drain += self.cognition
                if acted:
                    drain += self.action
            gain = max(0.0, float(charge_in))                 # environmental power in (solar / dock)
            if self.archetype == "animal" and resting:
                gain += self.recovery                         # an animal naps/docks to recharge
            self.energy = max(0.0, min(1.0, self.energy + gain - drain))
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
        """Nourishment restores energy. Clamped to [0,1]. Returns the new energy. (Mostly used by the
        animal archetype / tests now; for a real-power node the reserve is ANCHORED to SOC instead.)"""
        with self._lock:
            self.energy = max(0.0, min(1.0, self.energy + float(amount)))
            e = self.energy
        self._publish()
        return round(e, 4)

    def anchor_soc(self, soc_percent):
        """Set the reserve to the REAL battery state-of-charge (0..100%). This is the post-pivot truth
        path: when a power reader has a fresh reading, the energy reserve IS the battery, not a sim. The
        per-tick metabolize() drift between reads still applies (so thinking is still felt as spending);
        each fresh reading re-anchors to reality. Ignores out-of-range / None (fail-safe). Returns energy."""
        try:
            s = float(soc_percent)
        except (TypeError, ValueError):
            return self.energy
        if s != s or s < 0.0 or s > 100.0:   # NaN or out of range -> ignore (don't corrupt the reserve)
            return self.energy
        with self._lock:
            self.energy = s / 100.0
            self._anchored_at = time.monotonic()
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
