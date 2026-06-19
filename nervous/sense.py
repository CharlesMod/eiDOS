"""The trivial sense (HeartbeatSense) + a read-only viewer.

`HeartbeatSense` is the minimal receptor adapter: one organ that owns its timer and touches the
world only through `bus.publish` (I2). It runs identically under any transport — it is the organ
the tri-mode gate exercises. `ViewerSubscriber` is read-only: it subscribes and recv-loops, never
publishes (the creature-render reader model, I6).

CLI:  python -m nervous.sense --role sense  --transport zmq
      python -m nervous.sense --role viewer --transport zmq   (other shell)
"""
import argparse
import json
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION
from .bus import NervousBus
from .transport import InProcTransport, ZmqTransport


class HeartbeatSense:
    def __init__(self, bus, source="heartbeat", interval_s=0.5, modality=Modality.time, salience=0.1):
        self.bus = bus
        self.source = source
        self.interval_s = float(interval_s)
        self.modality = modality
        self.salience = float(salience)
        self.beat = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="heartbeat-sense", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        # emit once immediately, then every interval
        self.emit()
        while not self._stop.wait(self.interval_s):
            self.emit()

    def emit(self):
        self.beat += 1
        payload = json.dumps({"beat": self.beat}).encode("utf-8")
        ev = NervousEvent(schema_version=SCHEMA_VERSION, source_organ=self.source,
                          kind=Kind.sensory, modality=self.modality, delivery=Delivery.fungible,
                          salience=self.salience, t=time.monotonic())
        return self.bus.publish(ev, payload)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class ViewerSubscriber:
    def __init__(self, bus, topics=None, deliveries=None, on_event=None):
        self.bus = bus
        self.sub = bus.subscribe(topics=topics, deliveries=deliveries)
        self.on_event = on_event
        self.events = []
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="viewer", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            ev = self.bus.recv(self.sub, timeout=0.25)
            if ev is None:
                continue
            self.events.append(ev)
            self.bus.ack(ev)
            if self.on_event is not None:
                self.on_event(ev)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.bus.unsubscribe(self.sub)


def _build_bus(args):
    if args.transport == "zmq":
        kwargs = {}
        if args.bind:
            kwargs["bind"] = args.bind
        if args.connect:
            kwargs["connect"] = args.connect
        transport = ZmqTransport(**kwargs)
    else:
        transport = InProcTransport()
    return NervousBus(transport=transport)


def main():
    ap = argparse.ArgumentParser(description="eiDOS nervous-system trivial sense / viewer")
    ap.add_argument("--role", choices=["sense", "viewer"], required=True)
    ap.add_argument("--transport", choices=["inproc", "zmq"], default="inproc")
    ap.add_argument("--bind", default=None)
    ap.add_argument("--connect", default=None)
    ap.add_argument("--port", type=int, default=8120)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    # Default endpoints: the sense binds, the viewer connects.
    if args.transport == "zmq" and not args.bind and not args.connect:
        if args.role == "sense":
            args.bind = f"tcp://0.0.0.0:{args.port}"
        else:
            args.connect = f"tcp://127.0.0.1:{args.port}"

    bus = _build_bus(args)
    try:
        if args.role == "sense":
            s = HeartbeatSense(bus, interval_s=args.interval).start()
            time.sleep(args.seconds)
            s.stop()
            print(f"[sense] emitted {s.beat} beats over {args.transport}")
        else:
            v = ViewerSubscriber(
                bus, on_event=lambda e: print(f"[viewer] {e.source_organ} t={e.t:.3f} ref={e.payload_ref}")
            ).start()
            time.sleep(args.seconds)
            v.stop()
            print(f"[viewer] received {len(v.events)} events over {args.transport}")
    finally:
        bus.close()


if __name__ == "__main__":
    main()
