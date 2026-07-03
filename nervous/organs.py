"""Phase 1.1 — organ lifecycle hooks: the registry that ends the god-loop.

Today `eidos.py run_loop` hand-instantiates each nervous organ in a fixed init block and hand-calls
its per-tick work scattered through a ~1,400-line loop. That makes every new drive an edit to the
loop. The `OrganRegistry` inverts that: an organ *registers* its lifecycle hooks
(`pre_tick` / `post_tick` / `on_sleep`) plus the bus topics it declares it reads and writes, and the
loop just iterates the registry. A new organ plugs in without touching the loop.

Design pins (per `PILLARS_PLAN.md` §4 N-1 and `PILLARS_TODO.md` 1.1):
  - hooks fire in REGISTRATION ORDER — the loop's determinism is preserved by registering in the
    same order the direct calls used to run;
  - every hook is GUARDED (I5): one organ's fault is logged and swallowed, never breaking the tick
    (the loop is the creature's heartbeat — a drive that throws must not stop it);
  - `reads` / `writes` are DECLARED bus topics, recorded now for future conflict-checking (two
    writers to one topic, a reader with no producer) — inert today, no behaviour hangs off them;
  - the registry carries no per-tick state of its own: all tick data flows through the `ctx` object
    the loop hands to `run_*`, so an organ hook is a pure `f(ctx)` closure over the loop's locals.
"""
import logging

logger = logging.getLogger("eidos.organs")


class Organ:
    """One registered organ: the object plus its declared lifecycle hooks and bus topics.

    A hook is any callable taking the loop's `ctx`; `None` means "this organ has no work at that
    phase" (e.g. a thread-driven organ that self-runs has no per-tick hook, only declared topics).
    `reads` / `writes` are frozen tuples of declared topic identifiers — recorded for future
    conflict-checking, unused for behaviour today.
    """

    __slots__ = ("organ", "name", "pre_tick", "post_tick", "on_sleep", "reads", "writes")

    def __init__(self, organ, *, name=None, pre_tick=None, post_tick=None, on_sleep=None,
                 reads=(), writes=()):
        self.organ = organ
        self.name = name or getattr(organ, "source", None) or type(organ).__name__
        self.pre_tick = pre_tick
        self.post_tick = post_tick
        self.on_sleep = on_sleep
        self.reads = tuple(reads)
        self.writes = tuple(writes)


class OrganRegistry:
    """Plug-in registry for the nervous organs.

    `register(organ, ...)` appends an organ with its hooks; `run_pre_tick` / `run_post_tick` /
    `run_on_sleep` iterate the registered organs IN REGISTRATION ORDER and invoke that phase's hook,
    each guarded so one organ's fault never breaks the loop (I5). Behaviour-preserving migration: the
    loop registers organs in the same order the old direct calls fired, then replaces the direct
    calls with a single `run_<phase>(ctx)` — same organs, same order, same effects.
    """

    def __init__(self):
        self._organs = []

    def register(self, organ, *, name=None, pre_tick=None, post_tick=None, on_sleep=None,
                 reads=(), writes=()):
        """Register `organ` with its lifecycle hooks and declared bus topics; returns the Organ record.

        Called once per organ at loop init. `pre_tick` / `post_tick` / `on_sleep` are callables
        taking the loop's `ctx` (or None if the organ has no work at that phase). `reads` / `writes`
        are declared topic identifiers, recorded for future conflict-checking.
        """
        rec = Organ(organ, name=name, pre_tick=pre_tick, post_tick=post_tick, on_sleep=on_sleep,
                    reads=reads, writes=writes)
        self._organs.append(rec)
        return rec

    @property
    def organs(self):
        """The registered organs, in registration order (read-only view)."""
        return tuple(self._organs)

    def __len__(self):
        return len(self._organs)

    def __iter__(self):
        return iter(self._organs)

    def _run_phase(self, hook_attr, ctx):
        """Invoke `hook_attr` on every organ that declares it, in registration order, guarded (I5)."""
        for rec in self._organs:
            hook = getattr(rec, hook_attr)
            if hook is None:
                continue
            try:
                hook(ctx)
            except Exception as e:  # noqa: BLE001 - an organ fault must never break the tick (I5)
                logger.warning("organ %s %s hook failed: %s", rec.name, hook_attr, e)

    def run_pre_tick(self, ctx):
        """Fire every registered pre_tick hook, in registration order (guarded)."""
        self._run_phase("pre_tick", ctx)

    def run_post_tick(self, ctx):
        """Fire every registered post_tick hook, in registration order (guarded)."""
        self._run_phase("post_tick", ctx)

    def run_on_sleep(self, ctx):
        """Fire every registered on_sleep hook, in registration order (guarded)."""
        self._run_phase("on_sleep", ctx)
