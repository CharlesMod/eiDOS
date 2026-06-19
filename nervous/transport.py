"""Location transparency (I9) — the transport abstraction.

A Transport is a *bus-to-bus link*. The bus owns ALL delivery-class logic; the transport only
moves wire-dicts (+ inline payload bytes) between `NervousBus` instances. Because the bus always
serializes (publish -> to_wire -> route -> recv -> from_wire), the in-process path and the
cross-wire path exercise the *same* serialization — which is what makes the tri-mode equivalence
gate meaningful (a wire-only bug also shows up in-process).

  InProcTransport — one bus, no remote peer. `send()` is a no-op; the bus delivers locally.
  ZmqTransport    — links two buses over ZeroMQ (DEALER<->DEALER: bidirectional, auto-reconnect).
                    Bind `tcp://0.0.0.0:<port>`; a peer connects. `127.0.0.1` = cross-process, a
                    LAN/Tailscale host = cross-device, with NO code change (one architecture, two
                    manifests — EIDOS_V3_ARCHITECTURE.md §9). Fail-open on any error: a vanished
                    peer is a severed nerve (I5/I9), never a wedge.

pyzmq is imported lazily, so the in-process path never requires it.
"""
import abc
import json
import logging
import threading

logger = logging.getLogger("eidos.nervous")


class Transport(abc.ABC):
    """Carries wire-dicts (+ optional inline payload bytes) between bus instances."""

    @abc.abstractmethod
    def start(self, on_remote):
        """Register the inbound handler `on_remote(wire: dict, payload: bytes|None)` and begin
        receiving. Called once by the owning bus."""

    @abc.abstractmethod
    def send(self, wire, payload):
        """Forward a locally-published event to remote peer(s). MUST never raise (fail-open)."""

    @abc.abstractmethod
    def close(self):
        ...


class InProcTransport(Transport):
    """No remote peer — a single in-process bus. The bus does all delivery locally, so `send`
    forwards nothing. (The bus still round-trips to_wire/from_wire on every delivery, so the
    in-process path is byte-for-byte the wire path minus the socket.)"""

    def __init__(self):
        self._on_remote = None

    def start(self, on_remote):
        self._on_remote = on_remote

    def send(self, wire, payload):
        return  # nothing to forward; local delivery already happened in the bus

    def close(self):
        return


class ZmqTransport(Transport):
    """A bidirectional ZeroMQ DEALER<->DEALER link between two buses. One side binds, the other
    connects (to the binder's address). Events flow both ways; the wire-dict is one frame and the
    (small, inline) payload is a second frame."""

    def __init__(self, *, bind=None, connect=None, hwm=100_000, recv_timeout_ms=200):
        import zmq  # lazy: the in-proc path never needs pyzmq
        self._zmq = zmq
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.DEALER)
        self._sock.setsockopt(zmq.SNDHWM, int(hwm))
        self._sock.setsockopt(zmq.RCVHWM, int(hwm))
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.setsockopt(zmq.RCVTIMEO, int(recv_timeout_ms))
        if bind:
            self._sock.bind(bind)
        if connect:
            self._sock.connect(connect)
        self._send_lock = threading.Lock()
        self._on_remote = None
        self._stop = threading.Event()
        self._rx = None

    def start(self, on_remote):
        self._on_remote = on_remote
        self._rx = threading.Thread(target=self._recv_loop, name="nervous-zmq-rx", daemon=True)
        self._rx.start()

    def send(self, wire, payload):
        try:
            frames = [json.dumps(wire, ensure_ascii=False).encode("utf-8"), payload or b""]
            with self._send_lock:
                self._sock.send_multipart(frames, flags=self._zmq.NOBLOCK)
        except Exception as e:  # noqa: BLE001 - fail-open: a slow/dead peer never wedges a producer
            logger.debug("nervous zmq send dropped: %s", e)

    def _recv_loop(self):
        zmq = self._zmq
        while not self._stop.is_set():
            try:
                frames = self._sock.recv_multipart()
            except zmq.Again:
                continue  # recv timeout; loop to re-check _stop (responsive shutdown)
            except Exception as e:  # noqa: BLE001
                if self._stop.is_set():
                    return
                logger.debug("nervous zmq recv error: %s", e)
                continue
            try:
                wire = json.loads(frames[0].decode("utf-8"))
                payload = frames[1] if len(frames) > 1 and frames[1] else None
                if self._on_remote is not None:
                    self._on_remote(wire, payload)
            except Exception as e:  # noqa: BLE001
                logger.debug("nervous zmq decode error: %s", e)

    def close(self):
        self._stop.set()
        try:
            if self._rx is not None:
                self._rx.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._sock.close(linger=0)
        except Exception:
            pass
