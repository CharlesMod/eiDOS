"""Synthetic multi-priority load + the T8 metrics reporter.

Drives the bus past its fungible cap (forcing drops), runs ordered sequences, a reliable trickle,
periodic retained modulation, and occasional large payloads — all at once. `run()` yields
admits/sec and per-hop latency (p50/p95) per transport: the **T8** number the architecture defers
to P0, which sets later gate thresholds and ratifies (or vetoes) the ZMQ transport.

CLI:  python -m nervous.firehose --transport inproc --seconds 10
      python -m nervous.firehose --transport zmq    --seconds 10
"""
import argparse
import json
import os
import random
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION
from .bus import NervousBus
from .transport import InProcTransport, ZmqTransport


def _make_buses(transport, port, drop_log_path, fungible_qsize):
    if transport == "zmq":
        producer = NervousBus(transport=ZmqTransport(bind=f"tcp://0.0.0.0:{port}"),
                              drop_log_path=drop_log_path, fungible_qsize=fungible_qsize)
        consumer = NervousBus(transport=ZmqTransport(connect=f"tcp://127.0.0.1:{port}"),
                              fungible_qsize=fungible_qsize)
        time.sleep(0.3)  # let the DEALER link settle before publishing
        return producer, consumer
    bus = NervousBus(transport=InProcTransport(), drop_log_path=drop_log_path, fungible_qsize=fungible_qsize)
    return bus, bus


def run(transport="inproc", seconds=10.0, n_producers=4, port=8131,
        large_payload_every=500, large_payload_bytes=256 * 1024,
        drop_log_path=None, fungible_qsize=256):
    producer_bus, consumer_bus = _make_buses(transport, port, drop_log_path, fungible_qsize)
    sub = consumer_bus.subscribe()  # all topics + all deliveries
    stop = threading.Event()
    latencies = []
    lat_lock = threading.Lock()
    received = [0]
    published = [0]
    small_payload = json.dumps({"x": 1}).encode("utf-8")
    big_payload = os.urandom(large_payload_bytes)

    def consumer():
        local = []
        while not stop.is_set():
            ev = consumer_bus.recv(sub, timeout=0.1)
            if ev is None:
                continue
            local.append((time.monotonic() - ev.t) * 1000.0)
            received[0] += 1
            consumer_bus.ack(ev)
        with lat_lock:
            latencies.extend(local)

    consumers = [threading.Thread(target=consumer, name=f"fh-consumer-{i}", daemon=True) for i in range(2)]
    for c in consumers:
        c.start()

    def producer(idx):
        n = 0

        def pub(ev, payload=None):
            producer_bus.publish(ev, payload)
            published[0] += 1

        while not stop.is_set():
            n += 1
            roll = random.random()
            t = time.monotonic()
            if roll < 0.04:  # reliable efferent trickle
                pub(NervousEvent(SCHEMA_VERSION, f"src{idx}", Kind.action_request, Modality.device,
                                 Delivery.reliable, salience=random.random(), t=t), small_payload)
            elif roll < 0.09:  # short ordered sequence (completes, no hole)
                sid = f"seq-{idx}-{n}"
                for o in range(6):
                    pub(NervousEvent(SCHEMA_VERSION, f"src{idx}", Kind.percept, Modality.audio,
                                     Delivery.ordered, salience=0.5, t=time.monotonic(),
                                     sequence_id=sid, ordinal=o))
            elif roll < 0.10:  # retained modulation
                pub(NervousEvent(SCHEMA_VERSION, f"src{idx}", Kind.modulation, Modality.system,
                                 Delivery.retained, salience=0.0, precision=random.random(), t=t))
            else:  # fungible flood (varied salience), occasional large payload
                payload = big_payload if (n % large_payload_every == 0) else small_payload
                pub(NervousEvent(SCHEMA_VERSION, f"src{idx}", Kind.sensory, Modality.vision,
                                 Delivery.fungible, salience=random.random(), t=t), payload)

    producers = [threading.Thread(target=producer, args=(i,), name=f"fh-producer-{i}", daemon=True)
                 for i in range(n_producers)]
    for p in producers:
        p.start()

    time.sleep(seconds)
    stop.set()
    for p in producers:
        p.join(timeout=2.0)
    time.sleep(0.3)  # let consumers drain
    for c in consumers:
        c.join(timeout=2.0)

    with lat_lock:
        lat = sorted(latencies)

    def pct(p):
        if not lat:
            return 0.0
        return lat[min(len(lat) - 1, int(len(lat) * p))]

    admits_per_sec = received[0] / seconds if seconds else 0.0
    report = {
        "transport": transport,
        "seconds": seconds,
        "published": published[0],
        "received": received[0],
        "admits_per_sec": round(admits_per_sec, 1),
        "admits_per_sec_rpi_x7": round(admits_per_sec / 7.0, 1),  # conftest's RPi-4 projection convention
        "p50_ms": round(pct(0.50), 3),
        "p95_ms": round(pct(0.95), 3),
        "p99_ms": round(pct(0.99), 3),
        "producer_stats": producer_bus.stats(),
        "consumer_stats": consumer_bus.stats(),
    }

    consumer_bus.unsubscribe(sub)
    if consumer_bus is not producer_bus:
        consumer_bus.close()
    producer_bus.close()
    return report


def main():
    ap = argparse.ArgumentParser(description="eiDOS nervous-system firehose / T8 metrics")
    ap.add_argument("--transport", choices=["inproc", "zmq"], default="inproc")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--producers", type=int, default=4)
    ap.add_argument("--port", type=int, default=8131)
    ap.add_argument("--metrics-log", default=None)
    args = ap.parse_args()
    rep = run(transport=args.transport, seconds=args.seconds, n_producers=args.producers, port=args.port)
    line = json.dumps(rep, ensure_ascii=False)
    print(line)
    if args.metrics_log:
        with open(args.metrics_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")


if __name__ == "__main__":
    main()
