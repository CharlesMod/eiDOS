"""P1a — interoception: the first real organ. The creature feels its own body.

Reads coarse host telemetry (RAM, disk, CPU, and best-effort GPU VRAM/temp) and publishes ONE
`interoceptive` NervousEvent per interval carrying the felt "resource bars" — coarse raw bins
(ok / elevated / high / critical). This is the *buildable-now* form: NOT yet the felt-qualia
transfer function (P1b) and NOT model-based interoceptive inference (T3). The organ is the single
writer of the felt-state (I6).

Substrate-independent: it reuses the repo's Windows-native readers (safety/telemetry), which degrade
gracefully elsewhere; any signal that can't be read is simply absent (a Jetson reads what it has).

Biomimetic touch: salience = the WORST bar, so an at-rest body is barely salient and a stressed one
shouts — you don't notice your body until something is wrong.
"""
import json
import subprocess
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION
from .felt import felt_state, stress_bars

# ascending pressure: ok < elevated < high < critical
LEVELS = ["ok", "elevated", "high", "critical"]
_SEVERITY = {None: 0.0, "ok": 0.1, "elevated": 0.4, "high": 0.7, "critical": 1.0}


def _bin(value, t1, t2, t3):
    """Higher raw value -> higher bar (monotonic non-decreasing). None -> None."""
    if value is None:
        return None
    if value >= t3:
        return "critical"
    if value >= t2:
        return "high"
    if value >= t1:
        return "elevated"
    return "ok"


def _bin_free(value, t1, t2, t3):
    """For 'free' signals (more free = better): LOWER value -> higher pressure (monotonic)."""
    if value is None:
        return None
    if value <= t3:
        return "critical"
    if value <= t2:
        return "high"
    if value <= t1:
        return "elevated"
    return "ok"


def read_host_telemetry(config=None):
    """Best-effort raw signals; any unavailable signal is None (never raises)."""
    out = {"ram_pct": None, "disk_free_gb": None, "cpu_pct": None,
           "vram_used_pct": None, "gpu_temp_c": None}
    try:
        import safety
        out["ram_pct"] = float(safety.check_ram(100.0)[1])
    except Exception:
        pass
    try:
        import safety
        path = str(config.workspace) if config is not None else "."
        out["disk_free_gb"] = float(safety.check_disk_space(path, 0.0)[1])
    except Exception:
        pass
    try:
        import telemetry
        out["cpu_pct"] = float(telemetry.get_cpu_pct())
    except Exception:
        pass
    # GPU: best-effort nvidia-smi (eiDOS's #1 interoceptive signal). Fail-open if absent (a Jetson
    # would use a different reader; substrate-independence).
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4)
        if r.returncode == 0 and r.stdout.strip():
            used, total, temp = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            used, total = float(used), float(total)
            out["vram_used_pct"] = (used / total * 100.0) if total else None
            out["gpu_temp_c"] = float(temp)
    except Exception:
        pass
    return out


def felt_bars(t):
    """Map raw telemetry -> coarse felt bars. Monotonic in each signal (the P1a gate)."""
    return {
        "ram": _bin(t.get("ram_pct"), 70, 85, 95),
        "disk": _bin_free(t.get("disk_free_gb"), 20, 5, 1),
        "cpu": _bin(t.get("cpu_pct"), 60, 85, 97),
        "vram": _bin(t.get("vram_used_pct"), 80, 92, 98),
        "gpu_temp": _bin(t.get("gpu_temp_c"), 70, 82, 90),
    }


def worst_salience(bars):
    # Baseline systems (the resident mind filling VRAM, by design) are posture, not threat — they never
    # make the body salient. You don't notice your body until something is actually wrong.
    return max((_SEVERITY.get(v, 0.0) for v in stress_bars(bars).values()), default=0.0)


class Interoception:
    def __init__(self, bus, *, source="interoception", interval_s=5.0, config=None, reader=None,
                 metabolism=None):
        self.bus = bus
        self.source = source
        self.interval_s = float(interval_s)
        self.config = config
        self.reader = reader or (lambda: read_host_telemetry(config))
        self.metabolism = metabolism   # M0: fold the creature's hunger into the felt body (NON-baseline)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="interoception", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        self.emit()
        while not self._stop.wait(self.interval_s):
            self.emit()

    def emit(self):
        try:
            raw = self.reader()
        except Exception:
            return None
        bars = {k: v for k, v in felt_bars(raw).items() if v is not None}
        # M0: the creature's hunger is interoception too — fold the metabolism's energy bar in so it
        # rides the SAME felt-state (one source of truth). Non-baseline → a hungry body feels worse.
        if self.metabolism is not None:
            try:
                bars["energy"] = self.metabolism.hunger_bar()
            except Exception:  # noqa: BLE001
                pass
        if not bars:
            return None
        # P1b: the single felt-state projection {bars, overall, felt} — one source of truth (I6),
        # published RETAINED (last-value-wins) so the core and any creature render read the SAME
        # current felt body. The qualia (the Pantheon abstraction) ride alongside the raw bars.
        projection = felt_state(bars)
        payload = json.dumps(projection, ensure_ascii=False).encode("utf-8")
        ev = NervousEvent(SCHEMA_VERSION, self.source, Kind.interoceptive, Modality.intero,
                          Delivery.retained, salience=worst_salience(bars), t=time.monotonic())
        return self.bus.publish(ev, payload)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
