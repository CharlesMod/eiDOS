"""nervous — the eiDOS V3 afferent nervous-system bus (P0: the seam).

Public surface: the NervousEvent contract, the NervousBus + its delivery classes, and the
Transport abstraction (location transparency, I9). See EIDOS_V3_ARCHITECTURE.md.
"""
from .event import (NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION,
                    RELIABLE_KINDS, RETAINED_KINDS, RELIABLE_FLOOR)
from .payload import PayloadStore
from .transport import Transport, InProcTransport, ZmqTransport
from .bus import NervousBus, Subscription, PublishResult

__all__ = [
    "NervousEvent", "Kind", "Modality", "Delivery", "SCHEMA_VERSION",
    "RELIABLE_KINDS", "RETAINED_KINDS", "RELIABLE_FLOOR",
    "PayloadStore", "Transport", "InProcTransport", "ZmqTransport",
    "NervousBus", "Subscription", "PublishResult",
]
