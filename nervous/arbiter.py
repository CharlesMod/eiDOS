"""P2 — the GPU lease arbiter (I7: arbitrate residency, not just utilization).

The adversarial review found `gpu_gate.py` is a single-waiter mutex. P2 builds the real multi-tenant
lease manager. Claimants — the mind tick, TTS, and (later) escalated perception — `acquire` a named
lease at a priority; the arbiter grants exclusive GPU residency, **preempts** a strictly-lower-priority
holder, and **reclaims** a lease that goes silent past its liveness cap (the gpu_gate discipline,
generalized). The existing speech-gate becomes its first client: TTS holds a higher priority than the
mind, so the tick yields to live speech — the existing behaviour, now one case of a general arbiter.

If given a bus, it publishes the current holder as a RETAINED event, so GPU contention is FELT (the
felt-state reflects it — the P2 gate). Every grant/preempt/reclaim is logged. Cooperative preemption:
a preempted holder sees `lease.preempted` set and is expected to stop using the GPU and release.
"""
import json
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION

# Priorities — higher preempts lower.
PRI_BACKGROUND = 0
PRI_MIND = 10      # the deliberative tick (llama.cpp decode)
PRI_SPEECH = 20    # TTS — the tick yields to live speech (the existing speech-gate semantics)
PRI_REFLEX = 30    # an escalated reflex / perception that must run now


class Lease:
    def __init__(self, name, priority, max_s):
        self.name = name
        self.priority = priority
        self.max_s = float(max_s)
        self.preempted = threading.Event()   # set if a higher-priority claimant took the GPU
        self.granted_at = time.monotonic()
        self.last_progress = self.granted_at


class GpuArbiter:
    def __init__(self, *, bus=None, log_path=None):
        self.bus = bus
        self.log_path = log_path
        self._cond = threading.Condition()
        self._holder = None
        self._log_lock = threading.Lock()

    def acquire(self, name, priority=PRI_MIND, max_s=60.0, timeout=None):
        """Block until the GPU is free or we outrank the holder. Returns a Lease, or None on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                self._reap_locked()
                h = self._holder
                if h is None:
                    lease = Lease(name, priority, max_s)
                    self._holder = lease
                    self._log("grant", lease)
                    self._publish_holder(name)
                    return lease
                if priority > h.priority:
                    h.preempted.set()                # cooperative preemption: holder must yield
                    self._log("preempt", h, by=name)
                    self._holder = None
                    self._publish_holder(None)
                    self._cond.notify_all()
                    continue                          # loop: now free, take it ourselves
                self._cond.wait(timeout=0.25)         # wake periodically to reap an expired holder

    def release(self, lease):
        with self._cond:
            if self._holder is lease:
                self._holder = None
                self._log("release", lease)
                self._publish_holder(None)
                self._cond.notify_all()

    def progress(self, lease):
        """Refresh a lease's liveness (a working holder stays alive however long; gpu_gate pattern)."""
        with self._cond:
            if self._holder is lease:
                lease.last_progress = time.monotonic()

    def current(self):
        with self._cond:
            return self._holder.name if self._holder else None

    def _reap_locked(self):
        h = self._holder
        if h is not None and time.monotonic() - h.last_progress > h.max_s:
            # a holder silent past its cap is presumed wedged/dead -> reclaim (no wedge, ARCH #2)
            h.preempted.set()
            self._log("reclaim", h)
            self._holder = None
            self._publish_holder(None)
            self._cond.notify_all()

    def _publish_holder(self, name):
        if self.bus is None:
            return
        try:
            payload = json.dumps({"gpu_holder": name}, ensure_ascii=False).encode("utf-8")
            ev = NervousEvent(SCHEMA_VERSION, "gpu_arbiter", Kind.capability, Modality.system,
                              Delivery.retained, salience=(0.0 if name in (None, "mind") else 0.6),
                              t=time.monotonic())
            self.bus.publish(ev, payload)
        except Exception:
            pass

    def _log(self, action, lease, by=None):
        if not self.log_path:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "action": action, "name": lease.name, "priority": lease.priority}
        if by is not None:
            entry["by"] = by
        try:
            with self._log_lock:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass
