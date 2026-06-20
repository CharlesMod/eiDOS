"""The predictive world-model (active inference / T2, honest-now form).

Biology: the brain is a prediction machine — it maintains a generative model of its world and forwards
only PREDICTION ERROR upward; what it already predicts is suppressed (habituation), what it cannot is
surprise. eiDOS builds the buildable-now version: a count-based transition model over its own situations
— given (situation, action), what situation tends to follow? Surprise = the negative log-probability of
the actual next situation. The model improves with experience (counts accumulate), so a world that was
once baffling becomes predictable, and only genuine novelty propagates.

It feeds two things: the change/salience gate (suppress the expected) and CURIOSITY (surprise is the
intrinsic-reward signal). Pure observer; never acts; persisted; never raises.
"""
import json
import math
import os
import threading
import time

SURPRISE_MAX = 6.0   # cap on -log2 p (an unseen transition ~= maximally novel)


class WorldModel:
    def __init__(self, *, config=None, path=None, max_contexts=3000, save_every=20, progress_beta=0.7):
        if config is not None and path is None:
            path = str(config.state_dir / "world_model.json")
        self.path = path
        self.max_contexts = int(max_contexts)
        self.save_every = int(save_every)
        self.progress_beta = float(progress_beta)
        self._lock = threading.Lock()
        self.transitions = {}      # context_key -> {next_situation: count}
        self._sema = {}            # context_key -> EMA of surprise (for learning-progress, M1)
        self.last_progress = 0.0   # learning progress of the most recent observe() (the nutrient signal)
        self._since_save = 0
        self._load()

    @staticmethod
    def _ck(situation, action):
        return f"{(situation or '')}=>{(action or '')}"[:300]

    def observe(self, situation, action, next_situation):
        """Record that (situation, action) was followed by next_situation. Returns the surprise of that
        transition BEFORE this update (how novel arriving here was), and stashes `last_progress` — the
        LEARNING PROGRESS (M1): how much the model just got BETTER at predicting this context.

        Progress is the nutrient signal (Loop A). It is gated on RE-encountering a transition: only a
        repeat can show the model improving, so pure noise (a fresh outcome every time — TV static)
        never feeds, while a learnable pattern feeds a burst that decays to ~0 as it's mastered (satiety,
        then frontier-seeking). This is why we measure learning progress, not raw surprise."""
        s = self.surprise(situation, action, next_situation)
        ck = self._ck(situation, action)
        nk = str(next_situation or "")
        with self._lock:
            d = self.transitions.get(ck)
            if d is None:
                d = {}
                self.transitions[ck] = d
            c = d.get(nk, 0)                       # times THIS exact transition seen before (0 = first)
            prev_ema = self._sema.get(ck)
            # only a re-encountered transition whose surprise fell below the running average is "learning"
            progress = max(0.0, prev_ema - s) if (c >= 1 and prev_ema is not None) else 0.0
            self._sema[ck] = s if prev_ema is None else (prev_ema * self.progress_beta + s * (1.0 - self.progress_beta))
            self.last_progress = round(progress, 4)
            d[nk] = c + 1
            self._evict_if_needed()
            self._since_save += 1
            do_save = self._since_save >= self.save_every
        if do_save:
            self._save()
            self._since_save = 0
        return s

    def surprise(self, situation, action, next_situation) -> float:
        """-log2 P(next | situation, action), Laplace-smoothed. Unseen context => maximally novel."""
        ck = self._ck(situation, action)
        with self._lock:
            d = self.transitions.get(ck)
            if not d:
                return SURPRISE_MAX
            total = sum(d.values())
            c = d.get(str(next_situation or ""), 0)
        p = (c + 0.5) / (total + 0.5 * (len(d) + 1))
        return min(SURPRISE_MAX, -math.log2(p)) if p > 0 else SURPRISE_MAX

    def predict(self, situation, action):
        """The predicted distribution over next situations (normalized counts)."""
        ck = self._ck(situation, action)
        with self._lock:
            d = dict(self.transitions.get(ck) or {})
        total = sum(d.values()) or 1
        return {k: v / total for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)}

    def snapshot(self):
        with self._lock:
            return {"contexts": len(self.transitions),
                    "transitions": sum(len(d) for d in self.transitions.values())}

    def _evict_if_needed(self):
        if len(self.transitions) <= self.max_contexts:
            return
        # drop the contexts with the fewest total observations (least-learned)
        victims = sorted(self.transitions.items(), key=lambda kv: sum(kv[1].values()))
        for k, _ in victims[: len(self.transitions) - self.max_contexts]:
            self.transitions.pop(k, None)
            self._sema.pop(k, None)

    def _load(self):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.transitions = json.load(f) or {}
            except Exception:  # noqa: BLE001
                self.transitions = {}

    def _save(self):
        if not self.path:
            return
        with self._lock:
            data = json.dumps(self.transitions, ensure_ascii=False)
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
        except Exception:  # noqa: BLE001
            return
        for _ in range(40):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.02)
            except Exception:  # noqa: BLE001
                return
