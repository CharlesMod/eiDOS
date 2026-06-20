"""The NervousEvent contract — the ONE message every organ shares.

The entire public surface area of the eiDOS V3 afferent nervous system is this single,
versioned message type on the bus (EIDOS_V3_ARCHITECTURE.md §4). Nothing else crosses an
organ boundary (I1/I2). `to_wire`/`from_wire` is the *single serialization seam* every
transport calls, so the in-process object and the on-wire bytes are provably identical
(I9 location transparency — a wire-only bug also shows up in-process).

Stdlib only (dataclasses/enum/json), matching the repo's no-pydantic/no-msgpack norm.
"""
import dataclasses
import enum
from typing import Optional

# Bumped only on a breaking change to this contract. Carried on every event; the bus
# rejects a mismatch and logs it (full min-common-version negotiation is T5 / P3).
SCHEMA_VERSION = 1


class Kind(str, enum.Enum):
    """What an event *is*. `str`-mixin so a member IS its wire string — json round-trips
    with no custom encoder."""
    # afferent / percepts
    sensory = "sensory"
    proprioceptive = "proprioceptive"
    change = "change"
    interoceptive = "interoceptive"
    percept = "percept"
    # efferent / reflex
    reflex_fired = "reflex_fired"
    action_request = "action_request"
    efference_copy = "efference_copy"
    # top-down / global
    relevance_set = "relevance_set"   # core -> gate (goal relevance)
    modulation = "modulation"         # neuromodulatory state -> all (retained)
    capability = "capability"         # an organ advertises identity/version (retained, I8)
    # learning / reward (the basal-ganglia / dopamine layer — self-improvement over time)
    reward = "reward"                 # a dopaminergic reward-prediction-error spike (transient)
    drive = "drive"                   # a homeostatic / intrinsic drive level e.g. curiosity (retained)
    metabolism = "metabolism"         # the energy reserve / hunger level (homeostatic, retained)
    power = "power"                   # real external power telemetry: battery SOC + solar watts (retained)
    # bus-emitted control / self-health (never produced by an organ directly)
    sequence_aborted = "sequence_aborted"            # an ordered stream aborted atomically
    reliable_undeliverable = "reliable_undeliverable"  # a reliable event could not be delivered (the "numbness" alarm)


class Modality(str, enum.Enum):
    audio = "audio"
    vision = "vision"
    intero = "intero"
    proprio = "proprio"
    device = "device"
    time = "time"
    system = "system"   # bus-internal / control / health


class Delivery(str, enum.Enum):
    fungible = "fungible"   # best-effort, drop-by-priority OK (counted + logged)
    ordered = "ordered"     # in-order, atomic-abort, never a hole
    reliable = "reliable"   # never dropped under normal backpressure; priority floor
    retained = "retained"   # last-value-wins global state (a late subscriber gets the current value)


# Kinds the bus delivers as `reliable` regardless of an organ's request: these are efferent
# intent, top-down control, capability/version, and the bus's own health signals. Their
# priority floor guarantees they outrank any fungible percept under contention (kills the
# efferent priority-inversion — EIDOS_V3_ARCHITECTURE.md §7).
RELIABLE_KINDS = frozenset({
    Kind.action_request, Kind.efference_copy, Kind.reflex_fired,
    Kind.relevance_set, Kind.capability,
    Kind.sequence_aborted, Kind.reliable_undeliverable,
})

# Kinds carried as retained (last-value-wins) global state.
RETAINED_KINDS = frozenset({Kind.modulation, Kind.capability, Kind.drive, Kind.metabolism, Kind.power})

# admit_priority floor added to salience for reliable kinds, so reliable always outranks fungible.
RELIABLE_FLOOR = 1_000_000.0


@dataclasses.dataclass(frozen=True)
class NervousEvent:
    """One signal on the nervous system. Immutable (frozen): no torn reads, and the payload
    itself is content-addressed + immutable in the PayloadStore (the event carries only the
    hash, never the bytes or a pointer)."""
    schema_version: int
    source_organ: str
    kind: Kind
    modality: Modality
    delivery: Delivery
    salience: float = 0.0          # bottom-up priority
    precision: float = 0.0         # confidence / top-down gain
    t: float = 0.0                 # monotonic stamp at publish
    payload_ref: Optional[str] = None   # content hash into PayloadStore (NOT the bytes)
    sequence_id: Optional[str] = None   # set for `ordered` streams
    ordinal: Optional[int] = None       # position within sequence_id
    admit_priority: float = 0.0    # derived at publish: floor(kind) + salience

    def to_wire(self) -> dict:
        """The single serialization seam. Plain json-safe dict; enums are already their str value."""
        return {
            "schema_version": self.schema_version,
            "source_organ": self.source_organ,
            "kind": self.kind.value,
            "modality": self.modality.value,
            "delivery": self.delivery.value,
            "salience": self.salience,
            "precision": self.precision,
            "t": self.t,
            "payload_ref": self.payload_ref,
            "sequence_id": self.sequence_id,
            "ordinal": self.ordinal,
            "admit_priority": self.admit_priority,
        }

    @classmethod
    def from_wire(cls, d: dict) -> "NervousEvent":
        return cls(
            schema_version=int(d["schema_version"]),
            source_organ=str(d["source_organ"]),
            kind=Kind(d["kind"]),
            modality=Modality(d["modality"]),
            delivery=Delivery(d["delivery"]),
            salience=float(d.get("salience", 0.0)),
            precision=float(d.get("precision", 0.0)),
            t=float(d.get("t", 0.0)),
            payload_ref=d.get("payload_ref"),
            sequence_id=d.get("sequence_id"),
            ordinal=(None if d.get("ordinal") is None else int(d["ordinal"])),
            admit_priority=float(d.get("admit_priority", 0.0)),
        )

    def topic(self) -> tuple:
        """The (kind, modality) routing key. Retained topics key on (kind, modality, source)
        so each organ's last-value is distinct — handled by the bus."""
        return (self.kind, self.modality)
