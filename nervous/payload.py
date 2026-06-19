"""Content-addressed, immutable payload store (EIDOS_V3_ARCHITECTURE.md §4).

Large data never travels inline on the bus and never crosses an organ boundary as a pointer
(I9). A producer `put()`s bytes and gets a content hash (the `payload_ref`); consumers `get()`
by ref. Writing the same bytes twice is idempotent (same ref). Bounded: LRU eviction past
`max_bytes` — but a ref **pinned** by an un-acked reliable/ordered event is never evicted under
it (no torn read). Models voice.py's `_speech_texts` cap-and-drop dict, hardened with refcounts.

Thread-safe (one Lock); stdlib only.
"""
import hashlib
import threading
from collections import OrderedDict
from typing import Optional


class PayloadStore:
    def __init__(self, max_bytes: int):
        self._max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._data = OrderedDict()   # ref -> bytes, in LRU order (oldest first)
        self._pins = {}              # ref -> pin count (pinned refs are never evicted)
        self._bytes = 0

    @staticmethod
    def ref_for(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def put(self, data: bytes) -> str:
        """Store bytes, return their content ref. Idempotent (same bytes -> same ref)."""
        ref = self.ref_for(data)
        with self._lock:
            if ref in self._data:
                self._data.move_to_end(ref)  # refresh LRU recency
                return ref
            self._data[ref] = data
            self._bytes += len(data)
            self._evict_locked()
            return ref

    def get(self, ref: Optional[str]) -> Optional[bytes]:
        """Return the bytes for a ref, or None if absent/evicted. A None is a legitimate
        fungible drop; reliable/ordered refs are pinned so they never return None under load."""
        if ref is None:
            return None
        with self._lock:
            data = self._data.get(ref)
            if data is not None:
                self._data.move_to_end(ref)
            return data

    def pin(self, ref: Optional[str]) -> None:
        """Pin a ref so it cannot be evicted (held by reliable/ordered until `ack`)."""
        if ref is None:
            return
        with self._lock:
            self._pins[ref] = self._pins.get(ref, 0) + 1

    def unpin(self, ref: Optional[str]) -> None:
        if ref is None:
            return
        with self._lock:
            n = self._pins.get(ref, 0)
            if n <= 1:
                self._pins.pop(ref, None)
            else:
                self._pins[ref] = n - 1
            self._evict_locked()  # an unpinned ref may now be evictable

    def _evict_locked(self) -> None:
        # LRU-first eviction, skipping pinned refs. If everything over budget is pinned we hold
        # past budget on purpose — never evict bytes an un-acked reliable/ordered event needs.
        if self._bytes <= self._max_bytes:
            return
        for ref in list(self._data.keys()):
            if self._bytes <= self._max_bytes:
                break
            if ref in self._pins:
                continue
            data = self._data.pop(ref)
            self._bytes -= len(data)

    def stats(self) -> dict:
        with self._lock:
            return {"count": len(self._data), "bytes": self._bytes, "pinned": len(self._pins)}
