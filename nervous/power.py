"""M4 — Power: the creature's real food source (the Renogy solar system over BLE).

Post-pivot (2026-06-20): food = literal battery power. This organ reads the Renogy MPPT charge
controller over Bluetooth (Modbus-over-BLE through its BT-1/BT-2 module) and turns it into the truth
the metabolism runs on:
  - **SOC** (DERIVED from real pack voltage, not the controller's coulomb guess — the pack is LiFePO4
    24V/100Ah, whose reported SOC is unreliable) -> anchors the energy reserve (`metabolism.anchor_soc`).
  - **PV watts** (solar coming in) -> published for context / the recharge signal.

**Self-healing is the whole point (Dean uses the Renogy phone app himself).** A BLE peripheral allows
ONE central connection at a time, so whenever Dean's phone is connected the MPPT is unreachable to us —
that is a NORMAL, expected condition, never an error. This organ therefore:
  - never raises into the tick loop (every BLE op is guarded);
  - on failure, KEEPS the last good reading and marks the feed STALE after `stale_after_s` (the metabolism
    then falls back to its internal sim — the creature keeps living on its last-known fullness);
  - BACKS OFF exponentially on repeated failure so we don't fight the phone for the radio, and resumes
    normal cadence + re-anchors the instant the device is free again (no restart, no intervention).

The BLE read is injected (`reader=`) so the self-healing logic is testable without hardware.
Reference: github.com/cyrils/renogy-bt. Probe/feasibility: experiments/renogy_probe.py.
"""
import json
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION

WRITE_CHAR = "0000ffd1-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"
READ_START = 0x0100
READ_COUNT = 34

# LiFePO4 8S (24V) resting open-circuit curve, ONE cell (volts -> SOC%); pack = ×8. Flat mid-band by
# nature (≈26.0–26.8V spans 20–90%); honest at the knees. See renogy_ble_power memory for the why.
_LFP_CELL_SOC = [
    (3.500, 100), (3.400, 99), (3.350, 90), (3.320, 80), (3.300, 70), (3.290, 60),
    (3.280, 50), (3.270, 40), (3.260, 30), (3.250, 20), (3.225, 15), (3.200, 10),
    (3.130, 5), (3.000, 0), (2.500, 0),
]


def lifepo4_soc(pack_voltage, net_current_a=0.0, *, cells=8, r_internal=0.015):
    """SOC% for an N-cell LiFePO4 pack from measured terminal voltage. net_current_a>0 charging (raises
    terminal V above resting), <0 under load; subtract I·R to estimate resting before the table lookup."""
    try:
        v_rest = float(pack_voltage) - float(net_current_a) * float(r_internal)
        cell = v_rest / float(cells)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    tbl = _LFP_CELL_SOC
    if cell >= tbl[0][0]:
        return 100.0
    if cell <= tbl[-1][0]:
        return 0.0
    for (v_hi, s_hi), (v_lo, s_lo) in zip(tbl, tbl[1:]):
        if v_lo <= cell <= v_hi:
            f = (cell - v_lo) / (v_hi - v_lo) if v_hi != v_lo else 0.0
            return round(s_lo + f * (s_hi - s_lo), 1)
    return 0.0


def _crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _build_read(dev_id, start, count) -> bytes:
    f = bytes([dev_id, 0x03, (start >> 8) & 0xFF, start & 0xFF, (count >> 8) & 0xFF, count & 0xFF])
    return f + _crc16(f)


def parse_mppt(frame: bytes, *, cells=8, r_internal=0.015) -> dict:
    """Parse a 0x0100 x34 Modbus response into the fields we need (+ derived SOC)."""
    if len(frame) < 5 or frame[1] != 0x03:
        raise ValueError(f"not a modbus-read response: {frame[:6].hex()}")
    nbytes = frame[2]
    data = frame[3:3 + nbytes]
    regs = [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data) - 1, 2)]
    if len(regs) < 10:
        raise ValueError(f"short register block: {len(regs)} regs")
    batt_v = round(regs[1] * 0.1, 2)
    batt_a = round(regs[2] * 0.01, 2)
    load_a = round(regs[5] * 0.01, 2)
    net_a = round(batt_a - load_a, 2)
    return {
        "soc": lifepo4_soc(batt_v, net_a, cells=cells, r_internal=r_internal),
        "controller_soc": regs[0],
        "battery_voltage": batt_v,
        "net_current": net_a,
        "pv_voltage": round(regs[7] * 0.1, 2),
        "pv_current": round(regs[8] * 0.01, 2),
        "pv_power": regs[9],
        "load_power": regs[6],
    }


async def _ble_read_once(address, *, dev_id=255, connect_timeout=12.0, read_timeout=10.0):
    """One real BLE round-trip. Imported lazily so bleak isn't required unless power is enabled."""
    import asyncio
    from bleak import BleakClient
    buf = bytearray()
    done = asyncio.Event()

    def on_notify(_h, data: bytearray):
        buf.extend(data)
        if len(buf) >= 3 and (buf[1] & 0x80 or len(buf) >= 3 + buf[2] + 2):
            done.set()

    async with BleakClient(address, timeout=connect_timeout) as client:
        await client.start_notify(NOTIFY_CHAR, on_notify)
        await client.write_gatt_char(WRITE_CHAR, _build_read(dev_id, READ_START, READ_COUNT),
                                     response=False)
        try:
            await asyncio.wait_for(done.wait(), timeout=read_timeout)
        finally:
            try:
                await client.stop_notify(NOTIFY_CHAR)
            except Exception:  # noqa: BLE001
                pass
    if not buf or (len(buf) >= 2 and buf[1] & 0x80):
        raise IOError(f"no/exception response: {bytes(buf).hex() or 'empty'}")
    return bytes(buf)


def default_reader(config):
    """Build the real BLE reader callable: () -> parsed dict. Runs its own short-lived asyncio loop per
    read (a fresh loop each call keeps the thread simple and avoids a wedged loop surviving a failure)."""
    import asyncio
    address = getattr(config, "power_mppt_address", "")
    dev_id = int(getattr(config, "power_device_id", 255) or 255)
    cells = int(getattr(config, "power_battery_cells", 8) or 8)
    r_int = float(getattr(config, "power_battery_r_internal", 0.015) or 0.015)

    def _read():
        loop = asyncio.new_event_loop()
        try:
            frame = loop.run_until_complete(_ble_read_once(address, dev_id=dev_id))
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
        return parse_mppt(frame, cells=cells, r_internal=r_int)

    return _read


class PowerMonitor:
    """Polls the MPPT and anchors the metabolism reserve to real SOC. Fail-open + backoff + auto-recover
    so Dean using the Renogy app (stealing the single BLE link) degrades gracefully instead of breaking."""

    def __init__(self, bus, *, config=None, source="power", reader=None, metabolism=None,
                 interval_s=60.0, stale_after_s=600.0, backoff_max_s=600.0, clock=time.monotonic):
        self.bus = bus
        self.config = config
        self.source = source
        self.reader = reader or (default_reader(config) if config is not None else None)
        self.metabolism = metabolism
        self.interval_s = float(interval_s)
        self.stale_after_s = float(stale_after_s)
        self.backoff_max_s = float(backoff_max_s)
        self._clock = clock
        self._latest = None          # last good parsed reading (dict)
        self._last_ok = 0.0          # monotonic time of last good reading
        self._fails = 0              # consecutive failures (drives backoff)
        self._last_err = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ---- read surfaces (all defensive; never raise) ----------------------------------
    def is_fresh(self):
        with self._lock:
            return self._latest is not None and (self._clock() - self._last_ok) <= self.stale_after_s

    def latest(self):
        with self._lock:
            return dict(self._latest) if self._latest else None

    def snapshot(self):
        with self._lock:
            fresh = self._latest is not None and (self._clock() - self._last_ok) <= self.stale_after_s
            age = round(self._clock() - self._last_ok, 1) if self._last_ok else None
            return {"fresh": fresh, "age_s": age, "consecutive_fails": self._fails,
                    "last_error": self._last_err, "reading": dict(self._latest) if self._latest else None}

    # ---- the poll cycle ---------------------------------------------------------------
    def poll_once(self):
        """One attempt. Returns the reading on success or None on failure. NEVER raises. On success it
        publishes the retained power event and re-anchors the metabolism to real SOC."""
        if self.reader is None:
            return None
        try:
            reading = self.reader()
        except Exception as e:  # noqa: BLE001 - a busy/absent device is normal, not fatal
            with self._lock:
                self._fails += 1
                self._last_err = f"{type(e).__name__}: {e}"
            return None
        if not isinstance(reading, dict) or reading.get("soc") is None:
            with self._lock:
                self._fails += 1
                self._last_err = "unparseable reading"
            return None
        with self._lock:
            self._latest = reading
            self._last_ok = self._clock()
            self._fails = 0
            self._last_err = ""
        # anchor the reserve to truth (guarded — metabolism faults must not kill the monitor)
        if self.metabolism is not None:
            try:
                self.metabolism.anchor_soc(reading["soc"])
            except Exception:  # noqa: BLE001
                pass
        self._publish(reading)
        return reading

    def _next_delay(self):
        """Normal cadence when healthy; exponential backoff (capped) while failing — so we stop fighting
        the phone for the radio, then snap back to cadence on the first success."""
        with self._lock:
            fails = self._fails
        if fails <= 0:
            return self.interval_s
        return min(self.backoff_max_s, self.interval_s * (2 ** min(fails, 10)))

    def _run(self):
        # first read promptly; thereafter cadence/backoff. _stop.wait doubles as the sleep (interruptible).
        self.poll_once()
        while not self._stop.wait(self._next_delay()):
            self.poll_once()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="power", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _publish(self, reading):
        if self.bus is None:
            return
        try:
            payload = json.dumps(reading, ensure_ascii=False).encode("utf-8")
            # salience rises as the battery empties (a low reserve is what you want to feel).
            soc = reading.get("soc") or 0.0
            ev = NervousEvent(SCHEMA_VERSION, self.source, Kind.power, Modality.device,
                              Delivery.retained, salience=max(0.0, 1.0 - soc / 100.0), t=time.monotonic())
            self.bus.publish(ev, payload)
        except Exception:  # noqa: BLE001
            pass
