"""Battery profiler — learn THIS pack's true 0→100 over time (voltage + coulomb fusion).

The controller's SOC is a coulomb guess that's wrong on LiFePO4; even a generic resting-voltage curve
is mushy across 20–90% because LiFePO4's curve is nearly flat there. The only accurate SOC fuses
voltage (reliable ONLY at the steep ends) with coulomb counting (reliable in the flat middle),
re-anchored to the real, LEARNED endpoints of this specific pack + inverter:

  - v_full  = resting OCV when charging has tapered to ~0 at the top with sun available (absorption→
              float). Learned by observation, EMA-smoothed.
  - v_empty = the INVERTER's low-voltage cutoff. Learned ONLY when we actually observe it — a real load
              collapsing at low voltage. Until then the low end stays uncalibrated (honest, per Dean).
  - capacity_ah = coulombs counted across a full↔empty traversal (refines the 100Ah nameplate to what
              the pack actually delivers between those endpoints).

SOC fusion: coulomb counting carries the estimate through the flat middle (re-anchored to 100% at each
detected full and 0% at each cutoff); voltage corrects drift + dominates near the ends where it's steep.

Learning persists OUTSIDE the workspace so a creature wipe never erases hardware knowledge, and the
always-on dashboard poller feeds it every reading, so it keeps calibrating even when eidos is asleep.
"""
import json
import os
import time

from .power import lifepo4_soc

# Per-cell LiFePO4 thresholds (pack = ×cells). The flat band is where voltage→SOC is useless.
_FULL_CELL_V = 3.40       # resting per-cell at/above this = "top region" (full candidate)
_FLAT_LO_CELL = 3.20      # \ the flat mid-band: voltage carries almost no SOC information here,
_FLAT_HI_CELL = 3.35      # /  so coulomb counting must carry the estimate through it
_EMPTY_CELL_V = 3.05      # resting per-cell at/below this = "bottom region" (cutoff is plausible)
_TAPER_A = 1.5            # |charge current| this small near the top = absorption done (float)
_PV_AVAILABLE_W = 30.0    # PV producing at least this = the sun WOULD charge it if it weren't full
_LOAD_ON_W = 80.0         # a real load was running...
_LOAD_OFF_W = 12.0        # ...and then collapsed → the inverter cut off (only counts at low voltage)


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _ocv(voltage, net_current_a, r_internal):
    """Resting open-circuit voltage estimate: terminal V is inflated while charging, sagged under load —
    subtract the I·R offset to approximate what the pack would rest at."""
    try:
        return float(voltage) - float(net_current_a or 0.0) * float(r_internal)
    except (TypeError, ValueError):
        return None


class BatteryProfiler:
    def __init__(self, path=None, *, cells=8, r_internal=0.015, capacity_nameplate_ah=100.0,
                 clock=time.time, save_every_s=60.0, ema=0.2, max_gap_s=1800.0):
        self.path = str(path) if path else None
        self.cells = int(cells)
        self.r_internal = float(r_internal)
        self.capacity_nameplate_ah = float(capacity_nameplate_ah)
        self._clock = clock
        self.save_every_s = float(save_every_s)
        self.ema = float(ema)
        self.max_gap_s = float(max_gap_s)
        self._last_ts = None
        self._last_save = 0.0
        self._prev_load = 0.0
        self.state = {
            "v_full": None,                 # learned full-charge resting OCV
            "v_empty": None,                # learned inverter-cutoff resting OCV (observe-only)
            "capacity_ah": None,            # learned usable Ah across a full↔empty traversal
            "coulomb_ah_since_anchor": 0.0, # signed Ah integrated since the last anchor
            "anchor": None,                 # "full" | "empty" | None — what coulomb_soc is pinned to
            "coulomb_soc": None,            # SOC fraction carried by coulomb counting
            "v_min_seen": None, "v_max_seen": None,
            "full_events": 0, "empty_events": 0, "samples": 0, "updated_ts": None,
        }
        self.load()

    # ---- persistence (outside the wipe-able workspace) --------------------------------
    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                for k in self.state:
                    if k in d:
                        self.state[k] = d[k]
        except Exception:  # noqa: BLE001
            pass

    def save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass

    def _maybe_save(self, now):
        if now - self._last_save >= self.save_every_s:
            self._last_save = now
            self.save()

    # ---- learning ---------------------------------------------------------------------
    def _ema(self, old, new):
        return new if old is None else (1.0 - self.ema) * old + self.ema * new

    def _mark_full(self, ocv, now):
        st = self.state
        st["v_full"] = round(self._ema(st["v_full"], ocv), 3)
        # a full reached straight from an empty anchor → the coulombs traversed ARE the usable capacity
        if st["anchor"] == "empty" and st["coulomb_ah_since_anchor"] > 1.0:
            st["capacity_ah"] = round(self._ema(st["capacity_ah"], st["coulomb_ah_since_anchor"]), 2)
        st["anchor"] = "full"
        st["coulomb_ah_since_anchor"] = 0.0
        st["coulomb_soc"] = 1.0
        st["full_events"] += 1

    def _mark_empty(self, ocv, now):
        st = self.state
        st["v_empty"] = round(self._ema(st["v_empty"], ocv), 3)
        if st["anchor"] == "full" and st["coulomb_ah_since_anchor"] < -1.0:
            st["capacity_ah"] = round(self._ema(st["capacity_ah"], abs(st["coulomb_ah_since_anchor"])), 2)
        st["anchor"] = "empty"
        st["coulomb_ah_since_anchor"] = 0.0
        st["coulomb_soc"] = 0.0
        st["empty_events"] += 1

    def update(self, reading):
        """Ingest one reading: integrate coulombs, detect full/cutoff events, track extremes."""
        now = self._clock()
        st = self.state
        st["samples"] += 1
        st["updated_ts"] = now
        net = reading.get("net_current")
        load = float(reading.get("load_power") or 0.0)
        pv = float(reading.get("pv_power") or 0.0)
        ocv = _ocv(reading.get("battery_voltage"), net, self.r_internal)
        cell = (ocv / self.cells) if ocv is not None else None
        if ocv is not None:
            st["v_min_seen"] = ocv if st["v_min_seen"] is None else min(st["v_min_seen"], ocv)
            st["v_max_seen"] = ocv if st["v_max_seen"] is None else max(st["v_max_seen"], ocv)

        # coulomb integration (ignore the first sample and any long gap, e.g. a dashboard restart)
        dt = (now - self._last_ts) if self._last_ts is not None else 0.0
        self._last_ts = now
        if st["anchor"] is not None and net is not None and 0.0 < dt <= self.max_gap_s:
            st["coulomb_ah_since_anchor"] += float(net) * dt / 3600.0
            if st["capacity_ah"]:
                base = 1.0 if st["anchor"] == "full" else 0.0
                st["coulomb_soc"] = _clamp(base + st["coulomb_ah_since_anchor"] / st["capacity_ah"], 0.0, 1.0)

        # FULL: at the top, charge has tapered to ~float, and the sun is up (so it WOULD charge if hungry)
        if (cell is not None and cell >= _FULL_CELL_V and net is not None
                and abs(float(net)) <= _TAPER_A and pv >= _PV_AVAILABLE_W):
            self._mark_full(ocv, now)
        # CUTOFF: a real load collapsed at LOW voltage → the inverter's low-voltage disconnect tripped.
        # (A load dropping at NORMAL voltage is just the load turning off, not a cutoff — hence the gate.)
        elif (self._prev_load >= _LOAD_ON_W and load <= _LOAD_OFF_W
              and cell is not None and cell <= _EMPTY_CELL_V):
            self._mark_empty(ocv, now)
        self._prev_load = load

        self._maybe_save(now)

    # ---- estimate ---------------------------------------------------------------------
    def estimate(self, reading):
        """Best SOC + the calibration state. Fusion once both endpoints + capacity are learned; until
        then, the generic voltage curve, flagged as still calibrating."""
        st = self.state
        ocv = _ocv(reading.get("battery_voltage"), reading.get("net_current"), self.r_internal)
        v_soc = lifepo4_soc(ocv, 0.0, cells=self.cells, r_internal=0.0) if ocv is not None else None
        calibrated = (st["v_full"] is not None and st["v_empty"] is not None
                      and st["capacity_ah"] and st["coulomb_soc"] is not None)

        if calibrated and ocv is not None:
            cell = ocv / self.cells
            # voltage is informative only at the steep ends; in the flat middle, trust coulomb
            w_v = 0.6 if (cell >= _FLAT_HI_CELL or cell <= _FLAT_LO_CELL) else 0.1
            c_soc = st["coulomb_soc"] * 100.0
            soc = w_v * (v_soc if v_soc is not None else c_soc) + (1.0 - w_v) * c_soc
            # slow drift-correct the coulomb tracker toward voltage where voltage is trustworthy
            if v_soc is not None and w_v >= 0.5:
                st["coulomb_soc"] = _clamp(0.95 * st["coulomb_soc"] + 0.05 * (v_soc / 100.0), 0.0, 1.0)
            method, conf = "fusion", "good"
        else:
            soc = v_soc
            method = "voltage"
            conf = "learning" if st["v_full"] is not None else "calibrating"

        return {
            "soc": round(soc, 1) if soc is not None else None,
            "soc_method": method,
            "soc_confidence": conf,
            "soc_voltage": round(v_soc, 1) if v_soc is not None else None,
            "soc_coulomb": round(st["coulomb_soc"] * 100.0, 1) if st["coulomb_soc"] is not None else None,
            "v_full": st["v_full"],
            "v_empty": st["v_empty"],
            "capacity_ah": st["capacity_ah"],
            "profile_samples": st["samples"],
        }

    # ---- the public one-call surface the dashboard reader wraps ------------------------
    def ingest(self, reading):
        """Update the model from a reading, then enrich it with the learned SOC + calibration state and
        return it. Never raises — a profiler fault must not break the poll."""
        try:
            self.update(reading)
            reading.update(self.estimate(reading))
        except Exception:  # noqa: BLE001
            pass
        return reading
