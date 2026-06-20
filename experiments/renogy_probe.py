"""Renogy Rover BLE probe (experiment) — can we SEE the charge controller and read SOC + PV watts?

This is the feasibility spike for the metabolism pivot (food = literal battery power). The Rover talks
Modbus-over-BLE through its BT-1/BT-2 module:
  - write char  0000ffd1-0000-1000-8000-00805f9b34fb  (send the Modbus read request)
  - notify char 0000fff1-0000-1000-8000-00805f9b34fb  (the response comes back here, maybe chunked)
Reading 34 holding registers from 0x0100 gives the live charge state. We only need two:
  - 0x0100  battery state-of-charge (%)   -> the reserve anchor
  - 0x0109  charging power (W, = PV watts) -> the recharge rate
(reference: github.com/cyrils/renogy-bt)

Usage (repo venv has bleak):
  PYTHONUTF8=1 .venv/Scripts/python.exe experiments/renogy_probe.py            # scan, auto-pick, read
  PYTHONUTF8=1 .venv/Scripts/python.exe experiments/renogy_probe.py --scan      # scan + list only
  PYTHONUTF8=1 .venv/Scripts/python.exe experiments/renogy_probe.py <ADDRESS>   # connect to one device
"""
import argparse
import asyncio
import json
import sys

from bleak import BleakClient, BleakScanner

WRITE_CHAR = "0000ffd1-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"
DEVICE_ID = 255          # standalone (not hub)
READ_START = 0x0100      # rover live-status block
READ_COUNT = 34          # 34 registers covers SOC (0x0100) .. charging power (0x0109) and more

# Renogy BT-1/BT-2 modules advertise names like "BT-TH-XXXXXXXX"; some show "BTRIC..." Flag candidates.
NAME_HINTS = ("BT-TH", "BT-", "BTRIC", "RNG", "RENOGY", "ROVER")


def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])   # Modbus appends low byte first


def build_read(device_id: int, start: int, count: int) -> bytes:
    frame = bytes([device_id, 0x03, (start >> 8) & 0xFF, start & 0xFF,
                   (count >> 8) & 0xFF, count & 0xFF])
    return frame + crc16_modbus(frame)


# --- LiFePO4 24V (8S) state-of-charge from REAL pack voltage --------------------------------------
# Dean: don't trust the controller's reported SOC (a lead-acid-style coulomb guess, wrong on LiFePO4);
# derive it from the actual battery voltage. The pack is 8 cells in series (8 × 3.2V nominal = 25.6V),
# BMS-managed, LiFePO4. The curve is famously FLAT in the middle (≈26.0–26.8V spans 20–90%), so voltage
# SOC is coarse mid-band but honest and well-anchored at the knees. We correct the measured voltage back
# toward RESTING with a light internal-resistance term, because charge/load current offsets it (we read
# 27.5V while charging at ~20A — inflated; resting would be lower).
# Resting open-circuit table for ONE LiFePO4 cell (volts -> SOC%); pack = ×8.
_LFP_CELL_SOC = [
    (3.500, 100), (3.400, 99), (3.350, 90), (3.320, 80), (3.300, 70), (3.290, 60),
    (3.280, 50), (3.270, 40), (3.260, 30), (3.250, 20), (3.225, 15), (3.200, 10),
    (3.130, 5), (3.000, 0), (2.500, 0),
]


def lifepo4_24v_soc(pack_voltage, net_current_a=0.0, r_internal=0.015):
    """Derive SOC% for a 24V (8S) LiFePO4 pack from measured terminal voltage. net_current_a > 0 when
    charging (raises terminal V above resting) and < 0 under load; we subtract I·R to estimate resting."""
    v_rest = float(pack_voltage) - float(net_current_a) * float(r_internal)
    cell = v_rest / 8.0
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


def _u16(regs, i):
    return regs[i]


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def parse_rover(payload: bytes) -> dict:
    """payload = full response frame [id, func, byte_count, data..., crc_lo, crc_hi]."""
    if len(payload) < 5 or payload[1] != 0x03:
        raise ValueError(f"not a valid modbus-read response: {payload[:8].hex()}")
    nbytes = payload[2]
    data = payload[3:3 + nbytes]
    regs = [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]
    temp = _u16(regs, 3)
    ctrl_t = _s16_byte(temp >> 8)
    batt_t = _s16_byte(temp & 0xFF)
    batt_v = round(_u16(regs, 1) * 0.1, 2)
    batt_a = round(_u16(regs, 2) * 0.01, 2)    # charge current INTO the battery (from PV)
    load_a = round(_u16(regs, 5) * 0.01, 2)    # current OUT to the controller's load terminal
    net_a = round(batt_a - load_a, 2)          # >0 net charging, <0 net discharging
    return {
        "controller_soc": _u16(regs, 0),                     # 0x0100  the controller's OWN SOC (untrusted)
        "soc": lifepo4_24v_soc(batt_v, net_a),               # <- DERIVED from real voltage (LiFePO4 8S)
        "battery_voltage": batt_v,                           # 0x0101  the honest signal
        "battery_current": batt_a,                           # 0x0102  charge A
        "net_current": net_a,
        "controller_temp_c": ctrl_t,
        "battery_temp_c": batt_t,
        "load_voltage": round(_u16(regs, 4) * 0.1, 2),       # 0x0104
        "load_current": load_a,                              # 0x0105
        "load_power": _u16(regs, 6),                         # 0x0106  W
        "pv_voltage": round(_u16(regs, 7) * 0.1, 2),         # 0x0107
        "pv_current": round(_u16(regs, 8) * 0.01, 2),        # 0x0108
        "pv_power": _u16(regs, 9),                            # 0x0109  W  <- recharge rate
        "_n_regs": len(regs),
    }


def _s16_byte(b):
    return b - 0x100 if b & 0x80 else b


# The Renogy GATT service that carries ffd1/fff1; its modules advertise this (and/or a manufacturer
# record). Matching on it finds a Renogy device even when it advertises no friendly name.
RENOGY_SVC_HINTS = ("0000ffd0", "0000fff0", "0000ffe0")


async def scan(timeout=10.0):
    print(f"Scanning for BLE devices ({timeout:.0f}s, active)...", flush=True)
    found = await BleakScanner.discover(timeout=timeout, return_adv=True, scanning_mode="active")
    rows = []
    for addr, (dev, adv) in found.items():
        name = (getattr(dev, "name", None) or getattr(adv, "local_name", None) or "").strip()
        rssi = getattr(adv, "rssi", None)
        svcs = [str(u).lower() for u in (getattr(adv, "service_uuids", None) or [])]
        mfr = getattr(adv, "manufacturer_data", None) or {}
        mfr_ids = ",".join(f"{k:#06x}" for k in mfr.keys())
        svc_hit = any(s.startswith(h) for s in svcs for h in RENOGY_SVC_HINTS)
        name_hit = any(h in name.upper() for h in NAME_HINTS) if name else False
        cand = name_hit or svc_hit
        short_svcs = ",".join(s[:8] for s in svcs)[:40]
        rows.append((cand, rssi if rssi is not None else -999, addr, name, short_svcs, mfr_ids))
    rows.sort(key=lambda r: (not r[0], -r[1]))
    print(f"\n{'CAND':<5}{'RSSI':<6}{'ADDRESS':<20}{'NAME':<22}{'SVCS':<22}MFR")
    for cand, rssi, addr, name, svcs, mfr in rows:
        print(f"{'★' if cand else '':<5}{rssi:<6}{addr:<20}{(name or '(no name)'):<22}{svcs:<22}{mfr}")
    candidates = [(addr, name) for cand, _, addr, name, _, _ in rows if cand]
    return candidates


async def _one_read(client, dev_id, start, count, settle=8.0):
    """Send one Modbus read and return the raw response frame (bytes). Raises on timeout."""
    buf = bytearray()
    done = asyncio.Event()

    def on_notify(_handle, data: bytearray):
        buf.extend(data)
        if len(buf) >= 3 and (buf[1] & 0x80 or len(buf) >= 3 + buf[2] + 2):
            done.set()   # error frame (func|0x80) or a complete data frame

    await client.start_notify(NOTIFY_CHAR, on_notify)
    req = build_read(dev_id, start, count)
    print(f"  -> id={dev_id} read 0x{start:04x} x{count}: {list(req)}", flush=True)
    await client.write_gatt_char(WRITE_CHAR, req, response=False)
    try:
        await asyncio.wait_for(done.wait(), timeout=settle)
    finally:
        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:  # noqa: BLE001
            pass
    if not buf:
        raise TimeoutError("no response")
    return bytes(buf)


def _describe(frame: bytes) -> str:
    if len(frame) >= 2 and frame[1] & 0x80:
        codes = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
                 4: "device failure"}
        code = frame[2] if len(frame) > 2 else 0
        return f"EXCEPTION {code} ({codes.get(code, '?')})  raw={frame.hex()}"
    return f"DATA {frame[2] if len(frame) > 2 else 0} bytes  raw={frame.hex()}"


async def read_device(address: str, dev_id=DEVICE_ID, start=READ_START, count=READ_COUNT,
                      timeout=20.0) -> dict:
    print(f"\nConnecting to {address} ...", flush=True)
    async with BleakClient(address, timeout=timeout) as client:
        print(f"  connected={client.is_connected}", flush=True)
        frame = await _one_read(client, dev_id, start, count)
    print(f"  <- {_describe(frame)}")
    return parse_rover(frame)


async def probe_device(address: str, timeout=20.0):
    """Sweep candidate (id, register-block) pairs and report which ones the device answers with DATA.
    Renogy charge controllers live at 0x0100; RIU inverters expose live data elsewhere — find it."""
    blocks = [
        (0x0100, 34), (0x0100, 8), (0x000A, 17),         # rover/charge-controller + device-info
        (0x0FA0, 16), (0x1000, 16), (0x2000, 16),        # inverter live-data candidates
        (0x0200, 16), (0x0500, 16), (0x021B, 4),
    ]
    print(f"\nProbing {address} for readable register blocks ...", flush=True)
    hits = []
    async with BleakClient(address, timeout=timeout) as client:
        print(f"  connected={client.is_connected}", flush=True)
        for dev_id in (1, 255):
            for start, count in blocks:
                try:
                    frame = await _one_read(client, dev_id, start, count, settle=4.0)
                    desc = _describe(frame)
                except Exception as e:  # noqa: BLE001
                    desc = f"{type(e).__name__}: {e}"
                ok = desc.startswith("DATA")
                print(f"  id={dev_id} 0x{start:04x} x{count:<3} -> {desc}")
                if ok:
                    hits.append((dev_id, start, count, frame))
                await asyncio.sleep(0.3)
    print(f"\n{len(hits)} readable block(s).")
    return hits


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("address", nargs="?", help="BLE address to connect to (skip scan)")
    ap.add_argument("--scan", action="store_true", help="scan + list only, don't connect")
    ap.add_argument("--probe", action="store_true", help="sweep register blocks to find readable data")
    ap.add_argument("--id", type=int, default=DEVICE_ID, help="modbus device id (try 1 or 255)")
    ap.add_argument("--start", type=lambda x: int(x, 0), default=READ_START, help="start register e.g. 0x0100")
    ap.add_argument("--count", type=int, default=READ_COUNT, help="register count")
    ap.add_argument("--timeout", type=float, default=10.0, help="scan seconds")
    args = ap.parse_args()

    address = args.address
    if not address:
        candidates = await scan(args.timeout)
        if args.scan:
            return
        if not candidates:
            print("\nNo Renogy-like device found. Is the Rover/BT module powered and in range?")
            print("If you know its address, pass it explicitly. Re-run with --scan to see all devices.")
            return
        address, name = candidates[0]
        print(f"\nAuto-picked candidate: {address}  ({name})")

    if args.probe:
        await probe_device(address)
        return

    try:
        data = await read_device(address, dev_id=args.id, start=args.start, count=args.count)
    except Exception as e:  # noqa: BLE001
        print(f"\nREAD FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
    print("\n=== Rover live status ===")
    print(json.dumps(data, indent=2))
    # 24V 100Ah LiFePO4 pack → nominal 2.56 kWh; SOC*capacity gives rough stored Wh.
    wh = round(25.6 * 100 * data["soc"] / 100.0)
    print(f"\nSOC (derived from voltage) = {data['soc']}%   "
          f"[controller claims {data['controller_soc']}%]   ~{wh} Wh stored of 2560")
    print(f"PV power IN = {data['pv_power']} W   |   net {data['net_current']:+} A   "
          f"(battery {data['battery_voltage']} V, load {data['load_power']} W)")


if __name__ == "__main__":
    asyncio.run(main())
