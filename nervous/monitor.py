"""NervousMonitor — the operator's read-only window into the V3 nervous system (the *Pantheon*
"peek behind the curtain").

The creature feels ONE line ("body feels at ease (mind fully resident)"). Under that single qualia
runs a whole nervous system: interoception binning raw telemetry into felt bars, the felt transfer
function, the neuromodulatory arousal/mood state, the GPU lease arbiter, and the bus carrying typed
events through four delivery classes into the deliberative core's context. The monitor SUBSCRIBES to
the bus (I6 — it reads the published projections, it never recomputes the felt state), reads
`bus.stats()`, and assembles a compact snapshot of how each module is functioning and contributing to
the whole, which it atomically writes to a state file the dashboard serves to its "behind the curtain"
tab.

Pure observer: it never publishes and never acts; everything is guarded so a monitor fault can never
touch the creature. It subscribes ONLY to the droppable/retained traffic (never reliable/ordered), so
it cannot interfere with reliable payload pinning.
"""
import json
import os
import threading
import time

from .event import Kind, Modality, Delivery
from .felt import BASELINE_SYSTEMS, system_phrase

# The organs we narrate, in rough signal-flow order (substrate -> felt -> mood -> core). `kinds` are the
# event Kinds that organ emits, used to attribute feed traffic; `role` is the plain-language function.
_ORGANS = [
    ("interoception", "feels the body — telemetry → felt bars",        (Kind.interoceptive,)),
    ("neuromod",      "neuromodulation — arousal + affect (mood)",     (Kind.modulation,)),
    ("power",         "metabolic intake — battery SOC + solar (real food)", (Kind.power,)),
    ("metabolism",    "energy economy — hunger + tiredness (stakes)",  (Kind.metabolism,)),
    ("reward",        "reward learning — value cache + dopamine (RPE)", (Kind.reward,)),
    ("curiosity",     "curiosity drive — novelty → intrinsic reward",   (Kind.drive,)),
    ("gpu_arbiter",   "GPU residency — who holds the one GPU",         (Kind.capability,)),
    ("change",        "novelty / habituation — forwards only surprise", (Kind.change,)),
    ("exteroception", "world senses — camera / mic pre-filters",       (Kind.sensory, Kind.percept)),
    ("efferent",      "action + efference copy — the sense of agency", (Kind.action_request, Kind.efference_copy)),
    ("reflex",        "reflex arc — fires without the core",           (Kind.reflex_fired,)),
    ("bus",           "self-health — the 'numbness' alarm",            (Kind.reliable_undeliverable, Kind.sequence_aborted)),
]


def _enum_val(x):
    return x.value if hasattr(x, "value") else str(x)


class NervousMonitor:
    def __init__(self, bus, *, arbiter=None, config=None, snapshot_path=None,
                 interval_s=1.0, feed_max=48, active_window_s=12.0):
        self.bus = bus
        self.arbiter = arbiter
        self.config = config
        self.snapshot_path = str(snapshot_path) if snapshot_path else None
        self.interval_s = float(interval_s)
        self.feed_max = int(feed_max)
        self.active_window_s = float(active_window_s)
        # read-only subscription to the droppable + retained traffic ONLY (never reliable/ordered, whose
        # payloads are refcount-pinned). late-subscriber semantics give us the current retained values now.
        self.sub = bus.subscribe(deliveries={Delivery.fungible, Delivery.retained})
        self._feed = []              # rolling recent events (oldest..newest)
        self._seen = {}              # source_organ -> [last_wall, count]
        self._last_pub = None        # (published_count, wall) for rate
        self._stop = threading.Event()
        self._thread = None
        self._t0 = time.monotonic()

    # ---- lifecycle -------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="nervous-monitor", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            self.bus.unsubscribe(self.sub)
        except Exception:
            pass

    def _run(self):
        try:
            self.tick()
        except Exception:
            pass
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self):
        """Drain the bus, build the snapshot, write it. Public so a test can drive one cycle."""
        self._drain()
        snap = self.snapshot()
        if self.snapshot_path:
            self._write(snap)
        return snap

    # ---- collection ------------------------------------------------------------------
    def _drain(self):
        now = time.time()
        drained = 0
        while drained < 1000:
            ev = self.bus.recv(self.sub, timeout=0.0)
            if ev is None:
                break
            drained += 1
            src = ev.source_organ or "?"
            rec = self._seen.get(src)
            self._seen[src] = [now, (rec[1] + 1 if rec else 1)]
            self._feed.append({
                "t": round(now, 3),
                "source": src,
                "kind": _enum_val(ev.kind),
                "modality": _enum_val(ev.modality),
                "delivery": _enum_val(ev.delivery),
                "salience": round(float(ev.salience), 3),
            })
        if len(self._feed) > self.feed_max:
            self._feed = self._feed[-self.feed_max:]

    def _retained_json(self, kind, modality, source=None):
        try:
            ev = self.bus.retained_snapshot(kind, modality, source)
            if ev is None or not ev.payload_ref:
                return None
            raw = self.bus.payloads.get(ev.payload_ref)
            return json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            return None

    def snapshot(self) -> dict:
        now = time.time()
        felt = self._retained_json(Kind.interoceptive, Modality.intero) or {}
        mood = self._retained_json(Kind.modulation, Modality.system) or {}
        holder = None
        if self.arbiter is not None:
            try:
                holder = self.arbiter.current()
            except Exception:
                holder = None
        if holder is None:
            holder = (self._retained_json(Kind.capability, Modality.system, "gpu_arbiter") or {}).get("gpu_holder")
        try:
            stats = self.bus.stats()
        except Exception:
            stats = {}
        bars = (felt or {}).get("bars") or {}
        transduction = {sys: {"level": lvl, "phrase": system_phrase(sys, lvl),
                              "baseline": sys in BASELINE_SYSTEMS}
                        for sys, lvl in bars.items()}
        return {
            "ts": round(now, 3),
            "uptime_s": round(time.monotonic() - self._t0, 1),
            "felt": felt,                              # {bars, overall, felt}
            "mood": mood,                              # {arousal, valence, mood}
            "transduction": transduction,              # per-system {level, phrase, baseline} (raw->bar->felt)
            "power": self._retained_json(Kind.power, Modality.device),  # Renogy MPPT: SOC, solar watts, etc.
            "gpu_holder": holder,
            "baseline_systems": list(BASELINE_SYSTEMS),
            "bus": {**stats, "rate_per_s": self._rate(stats, now)},
            "organs": self._organs(now, felt, mood, holder, stats),
            "feed": list(reversed(self._feed)),        # newest first for the UI
        }

    def _rate(self, stats, now):
        pub = stats.get("published")
        if pub is None:
            return 0.0
        r = 0.0
        if self._last_pub is not None:
            dt = now - self._last_pub[1]
            if dt > 0:
                r = max(0.0, (pub - self._last_pub[0]) / dt)
        self._last_pub = (pub, now)
        return round(r, 2)

    def _organs(self, now, felt, mood, holder, stats):
        out = []
        for name, role, _kinds in _ORGANS:
            seen = self._seen.get(name)
            last_s = round(now - seen[0], 1) if seen else None
            count = seen[1] if seen else 0
            out.append({
                "name": name,
                "role": role,
                "last_s": last_s,
                "count": count,
                "active": (last_s is not None and last_s <= self.active_window_s),
                "detail": self._detail(name, felt, mood, holder, stats),
            })
        return out

    @staticmethod
    def _worst_bar(felt):
        order = {"ok": 0, "elevated": 1, "high": 2, "critical": 3}
        bars = (felt or {}).get("bars") or {}
        worst, wname = -1, None
        for k, v in bars.items():
            if order.get(v, 0) > worst:
                worst, wname = order.get(v, 0), (k, v)
        return wname  # (system, level) or None

    def _detail(self, name, felt, mood, holder, stats):
        try:
            if name == "interoception":
                overall = (felt or {}).get("overall")
                wb = self._worst_bar(felt)
                if not overall:
                    return "—"
                if wb:
                    base = " (baseline)" if wb[0] in BASELINE_SYSTEMS else ""
                    return f"{overall} · {wb[0]} {wb[1]}{base}"
                return overall
            if name == "neuromod":
                if not mood:
                    return "—"
                return f"{mood.get('mood','?')} · arousal {float(mood.get('arousal',0)):.2f} · valence {float(mood.get('valence',0)):+.2f}"
            if name == "power":
                pw = self._retained_json(Kind.power, Modality.device)
                if not pw or pw.get("soc") is None:
                    return "no battery link (using internal sim)"
                net = pw.get("net_current")
                # Direction is the NET current, not the presence of sun (load can exceed solar).
                if net is None:
                    flow = ""
                elif net > 0.1:
                    flow = f" · charging {net:.1f}A"
                elif net < -0.1:
                    flow = f" · discharging {abs(net):.1f}A"
                else:
                    flow = " · idle"
                return f"SOC {float(pw['soc']):.0f}% · {float(pw.get('battery_voltage', 0)):.1f}V{flow}"
            if name == "metabolism":
                meta = self._retained_json(Kind.metabolism, Modality.intero)
                if not meta:
                    return "—"
                bar = meta.get("bar", "ok")
                tag = "full" if bar == "ok" else f"{bar} hunger"
                return f"energy {float(meta.get('energy', 0)):.2f} · {tag}"
            if name == "gpu_arbiter":
                return f"holder: {holder}" if holder else "free (no lease held)"
            if name == "bus":
                nd = stats.get("undeliverable", 0)
                dr = stats.get("dropped", 0)
                return f"{dr} dropped · {nd} undeliverable" + (" ⚠" if nd else "")
            seen = self._seen.get(name)
            if seen:
                return f"{seen[1]} events"
            return "quiet"
        except Exception:
            return "—"

    # ---- output ----------------------------------------------------------------------
    def _write(self, snap):
        data = json.dumps(snap, ensure_ascii=False)
        tmp = f"{self.snapshot_path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
        except Exception:
            return
        for _ in range(40):   # Windows: dst may be briefly open for read by the dashboard (atomicio pattern)
            try:
                os.replace(tmp, self.snapshot_path)
                return
            except PermissionError:
                time.sleep(0.02)
            except Exception:
                return
