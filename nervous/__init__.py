"""nervous — the eiDOS V3 afferent nervous-system bus (P0: the seam).

Public surface: the NervousEvent contract, the NervousBus + its delivery classes, and the
Transport abstraction (location transparency, I9). See EIDOS_V3_ARCHITECTURE.md.
"""
from .event import (NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION,
                    RELIABLE_KINDS, RETAINED_KINDS, RELIABLE_FLOOR)
from .payload import PayloadStore
from .transport import Transport, InProcTransport, ZmqTransport
from .bus import NervousBus, Subscription, PublishResult
from .afferent import AfferentContext
from .felt import to_felt, felt_state, FeltStateView
from .arbiter import GpuArbiter, Lease, PRI_BACKGROUND, PRI_MIND, PRI_SPEECH, PRI_REFLEX


def build_bus(config, *, payload_store=None):
    """Construct a NervousBus from config: in-proc by default, ZMQ if config.nervous_transport=='zmq'.
    The deliberative core binds; organs connect (one architecture, two manifests — I9)."""
    if getattr(config, "nervous_transport", "inproc") == "zmq":
        transport = ZmqTransport(bind=getattr(config, "nervous_bind", None) or None)
    else:
        transport = InProcTransport()
    return NervousBus.from_config(config, transport=transport, payload_store=payload_store)


__all__ = [
    "NervousEvent", "Kind", "Modality", "Delivery", "SCHEMA_VERSION",
    "RELIABLE_KINDS", "RETAINED_KINDS", "RELIABLE_FLOOR",
    "PayloadStore", "Transport", "InProcTransport", "ZmqTransport",
    "NervousBus", "Subscription", "PublishResult",
    "AfferentContext", "build_bus",
    "to_felt", "felt_state", "FeltStateView",
    "GpuArbiter", "Lease", "PRI_BACKGROUND", "PRI_MIND", "PRI_SPEECH", "PRI_REFLEX",
]
