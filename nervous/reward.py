"""The reward-learning keystone — the dopaminergic basal-ganglia loop (self-improvement over time).

Biology: the brain improves behaviour over time without rewiring its whole cortex — a dopaminergic
reward-prediction-error (RPE) signal (Schultz) trains a value cache in the basal ganglia, the amygdala
tags surprising/affective events for preferential storage, and the hippocampus REPLAYS those tagged
episodes during sleep to consolidate them into durable policy. eiDOS does the same, honestly within its
constraints: the mind (Gemma-4-12B) is FROZEN — we cannot gradient-update its weights — so the policy is
the LLM and the *learning* lives in the memory substrate:

  - cortex (flexible policy)      -> the LLM tick
  - basal ganglia (value cache)   -> a tiny tabular V(state,action) updated by Rescorla-Wagner/TD(0)
  - dopamine (RPE)                -> reward - predicted, broadcast on the bus; nudges the neuromod state
  - amygdala (emotional tag)      -> |RPE| x arousal -> consolidation priority
  - hippocampus + sleep replay    -> re-applies the top-tagged experiences, distils LESSONS
  - the lessons re-enter context  -> they bias the LLM's next action = the weight-free policy update

The reward itself is computed from signals the tick ALREADY produces (success, real progress, the change
in the felt body, mood valence, strain) — interoception defines what "good" means, so the creature learns
to keep its own body well. State-dependent: the value key is prefixed with the felt-state, so it learns
"when I feel strained, X helps" — which works even in creature mode (no objectives/plan).

Honest-now: this is tabular TD learning + LLM policy, NOT deep RL. Pure observer of outcomes; never acts,
never blocks the tick; fully guarded.
"""
import json
import math
import os
import re
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION

# Reward weights (the "what is good" function). Interoception/affect define wellbeing, so the creature
# learns to keep its own body at ease, make real progress, and not thrash.
W_SUCCESS = 0.40     # the action succeeded vs failed
W_PROGRESS = 0.35    # genuinely new knowledge / a new skill this tick (not re-confirming)
W_FELT = 0.30        # the felt body got better (less distress) vs worse — the homeostatic core
W_VALENCE = 0.15     # mood valence (neuromod affect)
W_STRAIN = 0.15      # frustration / rumination penalty
STRAIN_NORM = 3.0    # strain bump that counts as "fully strained"

# The success channel is a reward-PREDICTION-ERROR signal (Schultz/RPE, this module's own doctrine):
# it should fire only when the outcome was genuinely IN DOUBT. A structurally-riskless action — one
# whose success the tick hardcodes True because it has no failure mode (a bare thought, a chat reply)
# — carries no outcome surprise, so paying it the full +W_SUCCESS is a free +0.40 the value learner
# will (correctly) chase, collapsing behaviour onto the safest no-op (the 2026-07-13 "thought tends to
# go well — lean into it" fixed point, 28/85 ticks pure thought). For these, the success channel pays
# ZERO (not a penalty — a penalty would teach "thinking goes badly"); such a tick lives or dies on real
# progress, the felt body, and intrinsic curiosity alone, which is exactly what makes it connective
# tissue rather than the goal. Everything that can actually fail (bash, write_file, create_skill,
# read_file, objective work…) keeps the full ±W_SUCCESS.
CANT_FAIL_ACTIONS = frozenset({"thought", "chat_reply", "note_append", "message"})
# ^ note_append joined 2026-07-13: it is pure REFLECTION (write to my own notebook — never fails),
# exactly like thought. Left uncovered, it routed around the thought/chat_reply fix — a creature
# journaling "the wall is my horizon." booked the free +0.40 every tick, which distilled into a
# "lean into it" lesson that then coached the loop from the KV-stable head (the burrower's morose
# wall-fixation). A riskless action carries no outcome surprise, so it earns no success reward and
# never becomes a habit-lesson.

# SUCCESS-riskless INSPECTION reads (2026-07-14): actions that CAN fail (a missing file, a bad note
# name) but whose SUCCESS is riskless — the file/note/list was simply there, and nothing durable was
# learned by looking. Left uncovered, a SUCCESSFUL re-read booked the free +0.40 every time (an OS read
# of an existing file cannot fail: tools.py), building a positive value cache (V≈0.46) that pulled the
# creature back into re-reading its own files — the rotating re-read spiral. Asymmetric treatment: a
# successful read pays ZERO on the success channel (its worth must come from information-gain intrinsic,
# not from the act), while a genuine FAILURE (read of a file that isn't there) STILL books -W_SUCCESS —
# that error taught something real. Unlike CANT_FAIL_ACTIONS this is applied only when success is True.
SUCCESS_RISKLESS_ACTIONS = frozenset({
    "read_file", "note_read", "note_list", "check_tools", "list_skills", "objective_list"})

# Neither a riskless reflection nor a riskless inspection may distill into a coached lesson/habit — a
# "reliably read garden_summary.txt" habit is exactly the loop coaching itself from the KV-stable head.
_NO_HABIT_ACTIONS = CANT_FAIL_ACTIONS | SUCCESS_RISKLESS_ACTIONS


def _action_tool(action) -> str:
    """The bare tool name at the head of a value-key action string ('note_append {..}' -> 'note_append',
    'bash: ls' -> 'bash'), for filtering riskless-reflection actions out of distilled lessons."""
    m = re.match(r"[a-z_]+", str(action or ""))
    return m.group(0) if m else ""

# felt overall -> a 0..1 wellbeing scalar (more at ease = higher)
_WELLBEING = {"at ease": 1.0, "a little tense": 0.66, "strained": 0.33, "in distress": 0.0}


def wellbeing(overall):
    return _WELLBEING.get(overall, 0.66)   # unknown/absent -> neutral


class RewardLearner:
    def __init__(self, *, bus=None, neuromod=None, config=None, alpha=0.3, replay_alpha=0.15,
                 value_path=None, experience_path=None, lessons_path=None, habits_path=None,
                 max_values=2000, max_experiences=600, save_every=10,
                 lesson_min_count=3, lesson_min_abs=0.25, lessons_top=8):
        self.bus = bus
        self.neuromod = neuromod
        self.alpha = float(alpha)
        self.replay_alpha = float(replay_alpha)
        self.max_values = int(max_values)
        self.max_experiences = int(max_experiences)
        self.save_every = int(save_every)
        self.lesson_min_count = int(lesson_min_count)
        self.lesson_min_abs = float(lesson_min_abs)
        self.lessons_top = int(lessons_top)
        if config is not None:
            sd = config.state_dir
            value_path = value_path or str(sd / "learned_values.json")
            experience_path = experience_path or str(sd / "experience.jsonl")
            lessons_path = lessons_path or str(sd / "learned_lessons.json")
            habits_path = habits_path or str(sd / "learned_habits.json")
        self.value_path = value_path
        self.experience_path = experience_path
        self.lessons_path = lessons_path
        self.habits_path = habits_path

        self._lock = threading.Lock()
        self.values = {}             # key -> {"v","n","situation","action","t"}
        self._lessons = []           # list[str]
        self._habits = []            # list[str]
        self._experiences = []       # in-memory ring (mirrors the jsonl tail) for replay/tests
        self._prev_wellbeing = None  # felt wellbeing at the previous observe (for the delta)
        self._since_save = 0
        self.last = None             # last observe() result, for the monitor
        # Result-novelty ring (in-session): recent (situation, result-hash) pairs. An action whose
        # RESULT repeats one seen here taught/changed nothing, so its success channel pays 0 — the
        # VERB-AGNOSTIC form of the read-reward fix (a re-read via read_file / bash cat / a self-
        # authored skill, or a re-write of identical content, all pay 0 alike). Not persisted: a fresh
        # process re-learns within a few ticks; the loop it prevents is a within-session pathology.
        self._recent_results: list[str] = []
        self._recent_results_set: set[str] = set()
        self._max_recent_results = 300
        self._load()

    # ---- reward function -------------------------------------------------------------
    def reward_of(self, *, success, made_progress, felt_delta, valence, strain, intrinsic=0.0,
                  can_fail=True):
        """The scalar reward in [-1, 1]. Pure + testable. `can_fail` gates the success channel: a
        riskless action (can_fail=False) scores 0 on success — an outcome never in doubt is no signal."""
        r = (W_SUCCESS if success else -W_SUCCESS) if can_fail else 0.0
        if made_progress:
            r += W_PROGRESS
        r += W_FELT * float(felt_delta)
        r += W_VALENCE * float(valence)
        r -= W_STRAIN * min(1.0, max(0.0, float(strain)) / STRAIN_NORM)
        r += float(intrinsic)
        return max(-1.0, min(1.0, r))

    def _result_is_stale(self, situation, result_sig) -> bool:
        """True if this (situation, result) was produced recently — the action learned/changed
        nothing new. Records it either way (first occurrence returns False and pays; repeats return
        True and are gated). Bounded, in-session. Empty/None result_sig is never stale (skipped)."""
        if not result_sig:
            return False
        key = (situation or "") + "\x00" + str(result_sig)
        seen = key in self._recent_results_set
        if not seen:
            self._recent_results.append(key)
            self._recent_results_set.add(key)
            if len(self._recent_results) > self._max_recent_results:
                old = self._recent_results.pop(0)
                self._recent_results_set.discard(old)
        return seen

    # ---- the learning step (one per acting tick) -------------------------------------
    def observe(self, *, situation="", action="", success=True, made_progress=False,
                strain=0.0, intrinsic=0.0, tick=None, can_fail=True, result_sig=None):
        """Compute the reward + RPE for this tick's action, update the value cache, log the experience,
        and fire dopamine. Reads the felt body + mood from the bus/neuromod itself. Guarded by callers.
        `can_fail` gates the success channel; a structurally-riskless action (also caught by name via
        CANT_FAIL_ACTIONS as a backstop for direct callers) never books the free ±W_SUCCESS."""
        overall = self._current_felt()
        wb = wellbeing(overall)
        prev = self._prev_wellbeing if self._prev_wellbeing is not None else wb
        felt_delta = wb - prev
        valence = float(getattr(self.neuromod, "valence", 0.0) or 0.0)
        arousal = float(getattr(self.neuromod, "arousal", 0.3) or 0.3)

        _tool = _action_tool(action)
        could_fail = bool(can_fail) and (_tool not in CANT_FAIL_ACTIONS)
        # A successful inspection read is riskless — drop its free +W_SUCCESS — but a FAILED read still
        # books -W_SUCCESS (the error is a real signal). Asymmetric, unlike the CANT_FAIL set above.
        if could_fail and success and _tool in SUCCESS_RISKLESS_ACTIONS:
            could_fail = False
        # RESULT-NOVELTY (verb-agnostic): a successful action whose OUTPUT repeats one seen recently in
        # this situation taught/changed nothing — pay 0 on the success channel whatever the verb. This
        # is what the name-gate above could not catch: the re-read loop that re-formed via `bash cat`
        # and self-authored read-wrapper skills (both booked +0.40), and `bash sleep`/idle (constant
        # output). A genuinely-new read or a new-content write yields a novel result and still pays.
        # Record on EVERY successful action (even riskless ones) so matching works ACROSS verbs — a
        # read_file records content X; a later `bash cat X` then registers as stale and pays 0.
        stale = self._result_is_stale(situation, result_sig) if success else False
        if could_fail and stale:
            could_fail = False
        reward = self.reward_of(success=success, made_progress=made_progress,
                                felt_delta=felt_delta, valence=valence, strain=strain,
                                intrinsic=intrinsic, can_fail=could_fail)
        key = self._key(situation, action, overall)
        with self._lock:
            entry = self.values.get(key)
            predicted = entry["v"] if entry else 0.0
            rpe = reward - predicted
            v_new = predicted + self.alpha * rpe
            self.values[key] = {"v": round(v_new, 4), "n": (entry["n"] + 1 if entry else 1),
                                "situation": self._situation_label(situation, overall),
                                "action": action, "t": _now()}
            tag = abs(rpe) * (0.5 + 0.5 * arousal)
            rec = {"tick": tick, "key": key, "situation": self._situation_label(situation, overall),
                   "action": action, "reward": round(reward, 4), "predicted": round(predicted, 4),
                   "rpe": round(rpe, 4), "tag": round(tag, 4), "success": bool(success)}
            self._experiences.append(rec)
            if len(self._experiences) > self.max_experiences:
                self._experiences = self._experiences[-self.max_experiences:]
            self._prev_wellbeing = wb
            self.last = {"reward": round(reward, 4), "rpe": round(rpe, 4),
                         "predicted": round(predicted, 4), "tag": round(tag, 4)}
            self._evict_if_needed()
            self._since_save += 1
            do_save = self._since_save >= self.save_every

        self._append_experience(rec)
        if do_save:
            self._save_values()
            self._since_save = 0
        # dopamine: a reward-prediction-error spike, broadcast + nudging the neuromodulatory state
        self._fire_dopamine(reward, rpe, predicted)
        if self.neuromod is not None and hasattr(self.neuromod, "observe_reward"):
            try:
                self.neuromod.observe_reward(rpe, reward)
            except Exception:  # noqa: BLE001
                pass
        return self.last

    # ---- sleep replay / consolidation ------------------------------------------------
    def replay(self, *, top_k=24):
        """Dream replay (called from the sleep cycle, low-arousal only): re-apply the highest-tagged
        experiences (consolidation strengthens them), then distil durable LESSONS from the value cache.
        Returns {replayed, lessons}."""
        with self._lock:
            batch = sorted(self._experiences, key=lambda r: r.get("tag", 0.0), reverse=True)[:int(top_k)]
            for r in batch:
                entry = self.values.get(r["key"])
                if not entry:
                    continue
                entry["v"] = round(entry["v"] + self.replay_alpha * (r["reward"] - entry["v"]), 4)
            lessons = self._distill_lessons()
            self._lessons = lessons
        habits = self.habits()                 # takes the lock itself → compute outside the with-block
        with self._lock:
            self._habits = habits
        self._save_values()
        self._save_lessons()
        self._save_habits()
        return {"replayed": len(batch), "lessons": list(lessons), "habits": list(habits)}

    def _distill_lessons(self):
        # Never distill a lesson about a riskless-REFLECTION action (thought / chat_reply /
        # note_append). "When idle: 'note_append the wall is my horizon' tends to go well — lean into
        # it" is exactly the self-reinforcing loop coach we must not inject into the head. Their value
        # channel is already ~0 going forward, but this also drops any already-poisoned entries.
        cand = [e for e in self.values.values()
                if e["n"] >= self.lesson_min_count and abs(e["v"]) >= self.lesson_min_abs
                and _action_tool(e.get("action")) not in _NO_HABIT_ACTIONS]
        cand.sort(key=lambda e: abs(e["v"]) * math.log1p(e["n"]), reverse=True)
        out = []
        for e in cand[:self.lessons_top]:
            sit = e.get("situation") or "in general"
            act = e.get("action") or "that"
            if e["v"] > 0:
                out.append(f"When {sit}: \"{act}\" tends to go well (v={e['v']:+.2f}, n={e['n']}) — lean into it.")
            else:
                out.append(f"When {sit}: \"{act}\" tends to go badly (v={e['v']:+.2f}, n={e['n']}) — try another approach.")
        return out

    def habits(self, k=5, min_value=0.5, min_count=5):
        """Habit formation (basal-ganglia automatization): the FEW over-learned (situation→action)
        routines — high value, heavily reinforced — that have become reliable defaults. Surfaced as a
        distinct, stronger prior than the broader lessons, so the creature reaches for them without
        re-deliberating (freeing the slow core). The strongest are candidates for skill-compilation."""
        with self._lock:
            cand = [e for e in self.values.values()
                    if e["v"] >= float(min_value) and e["n"] >= int(min_count)
                    and _action_tool(e.get("action")) not in _NO_HABIT_ACTIONS]
        cand.sort(key=lambda e: e["v"] * math.log1p(e["n"]), reverse=True)
        out = []
        for e in cand[:int(k)]:
            sit = e.get("situation") or "in general"
            out.append(f"When {sit}: you reliably \"{e.get('action') or 'do that'}\" (v={e['v']:+.2f}, n={e['n']}).")
        return out

    def lessons(self, situation=None, k=6):
        """The learned-behaviour hints for context injection: lessons whose situation matches the current
        one first, then the strongest global ones. Read-only; cheap."""
        with self._lock:
            ls = list(self._lessons)
        if situation:
            s = str(situation).lower()
            ls.sort(key=lambda t: 0 if s and s in t.lower() else 1)
        return ls[:int(k)]

    # ---- helpers ---------------------------------------------------------------------
    def _current_felt(self):
        if self.bus is None:
            return None
        try:
            ev = self.bus.retained_snapshot(Kind.interoceptive, Modality.intero)
            if ev is None or not ev.payload_ref:
                return None
            raw = self.bus.payloads.get(ev.payload_ref)
            d = json.loads(raw.decode("utf-8")) if raw else None
            return d.get("overall") if isinstance(d, dict) else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _situation_label(situation, overall):
        sit = (situation or "").strip()
        feel = (overall or "").strip()
        if sit and feel:
            return f"{feel} · {sit}"
        return sit or feel or ""

    @staticmethod
    def _key(situation, action, overall):
        # felt-state prefix => state-dependent values (works even with no objective/plan)
        return f"{(overall or '?')}::{(situation or '')}::{(action or '')}"[:300]

    def _evict_if_needed(self):
        if len(self.values) <= self.max_values:
            return
        # drop the least-reinforced, least-recent entries (lowest n, then oldest)
        victims = sorted(self.values.items(), key=lambda kv: (kv[1]["n"], kv[1].get("t", 0)))
        for k, _ in victims[: len(self.values) - self.max_values]:
            self.values.pop(k, None)

    def snapshot(self):
        with self._lock:
            return {"values": len(self.values), "experiences": len(self._experiences),
                    "lessons": list(self._lessons), "habits": list(self._habits),
                    "last": dict(self.last) if self.last else None}

    def _fire_dopamine(self, reward, rpe, predicted):
        if self.bus is None:
            return
        try:
            payload = json.dumps({"reward": round(reward, 4), "rpe": round(rpe, 4),
                                  "predicted": round(predicted, 4)}, ensure_ascii=False).encode("utf-8")
            ev = NervousEvent(SCHEMA_VERSION, "reward", Kind.reward, Modality.system,
                              Delivery.fungible, salience=min(1.0, abs(rpe)), t=time.monotonic())
            self.bus.publish(ev, payload)
        except Exception:  # noqa: BLE001
            pass

    # ---- persistence (stdlib, atomic-ish; never raises) ------------------------------
    def _load(self):
        if self.value_path and os.path.exists(self.value_path):
            try:
                with open(self.value_path, encoding="utf-8") as f:
                    self.values = json.load(f) or {}
            except Exception:  # noqa: BLE001
                self.values = {}
        if self.lessons_path and os.path.exists(self.lessons_path):
            try:
                with open(self.lessons_path, encoding="utf-8") as f:
                    self._lessons = json.load(f) or []
            except Exception:  # noqa: BLE001
                self._lessons = []
        if self.habits_path and os.path.exists(self.habits_path):
            try:
                with open(self.habits_path, encoding="utf-8") as f:
                    self._habits = json.load(f) or []
            except Exception:  # noqa: BLE001
                self._habits = []

    def _atomic_write(self, path, text):
        if not path:
            return
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:  # noqa: BLE001
            return
        for _ in range(40):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.02)
            except Exception:  # noqa: BLE001
                return

    def _save_values(self):
        with self._lock:
            data = json.dumps(self.values, ensure_ascii=False)
        self._atomic_write(self.value_path, data)

    def _save_lessons(self):
        with self._lock:
            data = json.dumps(self._lessons, ensure_ascii=False)
        self._atomic_write(self.lessons_path, data)

    def _save_habits(self):
        with self._lock:
            data = json.dumps(self._habits, ensure_ascii=False)
        self._atomic_write(self.habits_path, data)

    def _append_experience(self, rec):
        if not self.experience_path:
            return
        try:
            with open(self.experience_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass


def _now():
    return int(time.time())
