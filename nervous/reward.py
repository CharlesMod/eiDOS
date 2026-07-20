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

# --- Habituation / novelty pressure (SOTA#1 direction: repetition must not pay full price forever) ---
# The freebies above (riskless names, result-novelty) zero a WHOLE class of no-ops, but they do not
# touch a genuine tool call whose success channel keeps paying full every time the SAME action-shape
# hits the SAME target — `cat garden.txt` #500 booked the same +W_SUCCESS as #1, so rehearsal stayed
# as rewarding as exploration. Habituation is the missing pressure: the success contribution of an
# (action-signature, target) pair DECAYS toward a floor with each fresh repeat, and the count RECOVERS
# toward zero over wall-clock time (dishabituation — a long-unseen action feels novel again). A brand-
# new signature or target always pays full, so exploring new shapes/targets out-competes rehearsal by
# construction — no line here names "explore" or checks for a "garden"; it is pure generic pressure.
# All DECLARED knobs (PILLARS_PLAN §0.4), each with a one-line justification:
HABIT_FLOOR = 0.30           # a saturated repeat still pays this FRACTION of W_SUCCESS (never 0 — the
                             # action still works; we only strip the novelty premium, not the outcome)
HABIT_DECAY_PER_REP = 0.55   # geometric factor per effective repeat: scale = floor + (1-floor)·d^reps.
                             # 0.55 → ~2 reps to reach the half-way point, ~6 to approach the floor:
                             # fast enough to break a tight loop, slow enough a couple retries pay well
HABIT_RECOVERY_S = 1800.0    # wall-clock seconds to shed ONE repeat of accumulated habituation (30 min):
                             # a target untouched for this long feels one step newer again (event-free,
                             # computed lazily from elapsed time at next touch — no polling timer)
HABIT_MAX_PAIRS = 4000       # bounded persisted state: evict the least-recent pairs past this (like the
                             # value cache) so the file can't grow without limit over a long life

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


# A raw action label reaching this module carries the tool call VERBATIM — for a `write_file` that is
# the whole content payload (`write_file {"content": "The Nursery: From Seed to Sprout...", ...}`). A
# lesson distilled from that label would embed and then COACH that exact content string back into the
# head ("write_file '<the fiction>' tends to go well — lean into it"), which is how the loop taught
# itself its own self-referential fiction. The learning layer must generalize over ACTION SHAPE only:
# the tool plus the SIGNATURE of its arguments (which keys, of which kind), never any content value.
# So the value key, every distilled lesson/habit, and the habituation counter below all key on the
# shape returned here — a small write_file may be a good habit; a specific STRING never is.
def _summarize_json_arg(val) -> str:
    """A CONTENT-FREE description of one argument value: its shape/kind, never its text. A string is
    reported only by a coarse length bucket ('str:s|m|l'), a number as 'num', containers by size —
    so `{"content": "The Nursery..."}` and `{"content": "grocery list"}` share a signature but no
    payload ever leaks into it."""
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, (int, float)):
        return "num"
    if isinstance(val, str):
        n = len(val)
        bucket = "s" if n <= 24 else ("m" if n <= 200 else "l")
        return f"str:{bucket}"
    if isinstance(val, (list, tuple)):
        return f"list[{len(val)}]"
    if isinstance(val, dict):
        return "obj{" + ",".join(sorted(str(k) for k in val)) + "}"
    if val is None:
        return "null"
    return "val"


def action_signature(action) -> str:
    """Reduce an action label to its ACTION SHAPE — tool + argument signature — stripping every content
    value. This is what the value cache keys on and what lessons/habits render, so learning generalizes
    over *what kind of thing was done*, never over a specific content string.

      write_file {"content":"The Nursery...","path":"garden.txt"} -> write_file(content=str:l,path=str:s)
      note_append {"text":"the wall is my horizon"}               -> note_append(text=str:s)
      bash: cat garden.txt                                        -> bash: cat garden.txt

    For `bash`, the command text IS the shape (the verb `cat` and its already digit-collapsed operands,
    via context._norm_cmd upstream) and carries no free-form authored payload, so it is kept as-is. For
    a structured tool call, the trailing JSON object is replaced by its per-key kind signature. If the
    args don't parse as JSON we fall back to just the tool name — never the raw (possibly content-
    bearing) tail. Deterministic, pure, and safe on any input."""
    s = str(action or "").strip()
    if not s:
        return ""
    tool = _action_tool(s)
    # bash: keep the command shape (already normalized upstream); it has no authored-content payload.
    if tool == "bash":
        return s
    # Structured tool call: "tool {json}" (the eidos.py _act_readable form). Replace the object with a
    # content-free key→kind signature. A terse-truncated tail may not parse — fall back to tool only.
    brace = s.find("{")
    if brace == -1:
        return tool or s
    head = s[:brace].strip() or tool
    try:
        args = json.loads(s[brace:])
    except (ValueError, TypeError):
        return head or tool
    if not isinstance(args, dict):
        return head or tool
    if not args:
        return f"{head}()"
    parts = ",".join(f"{k}={_summarize_json_arg(args[k])}" for k in sorted(args))
    return f"{head}({parts})"


def action_target(action) -> str:
    """A content-free TARGET handle for an action: the *thing acted on*, so habituation can tell
    'this same shape against the SAME target again' (rehearsal) from 'this shape against a NEW target'
    (exploration). For bash it is the normalized command tail (the operands); for a structured call it
    is the value of the first path/target-like key REDUCED to its stem (never an authored content
    string). Never carries a free-form content payload. Empty when there is no natural target."""
    s = str(action or "").strip()
    if not s:
        return ""
    tool = _action_tool(s)
    if tool == "bash":
        # the command minus its leading verb — the operands are the target ('cat garden.txt' -> 'garden.txt')
        body = s.split(":", 1)[1].strip() if ":" in s else s
        parts = body.split(None, 1)
        return parts[1] if len(parts) > 1 else body
    brace = s.find("{")
    if brace == -1:
        return ""
    try:
        args = json.loads(s[brace:])
    except (ValueError, TypeError):
        return ""
    if not isinstance(args, dict):
        return ""
    # Prefer an explicit locator key; these NAME a target without being free-form authored content.
    for k in ("path", "file", "filename", "name", "target", "skill", "url", "note", "key"):
        if k in args and isinstance(args[k], (str, int, float)):
            v = str(args[k]).strip()
            if v:
                # collapse volatile scalars so garden#.txt variants share a target, but keep the stem
                return re.sub(r"\d+", "#", v)[:80]
    return ""


def normalize_result(text: str) -> str:
    """Collapse volatile scalars in a tool's output BEFORE it is hashed into a result-novelty
    signature, so an action whose only 'novelty' is a changing number reads as the SAME result and
    pays 0 on the success channel. This closes the reward freebie the exact-byte signature left open:
    a `date`/`uptime`/`echo $RANDOM`/growing-log probe, and — critically — EVERY async `bash`, whose
    dispatch ack embeds a fresh job PID (`j12345`) each tick so an identical command looked novel
    forever. Collapse digit runs and whitespace (mirrors context._norm_cmd) but keep the rest and do
    NOT truncate, so genuinely-different textual content still reads as novel."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text)).strip()

# felt overall -> a 0..1 wellbeing scalar (more at ease = higher)
_WELLBEING = {"at ease": 1.0, "a little tense": 0.66, "strained": 0.33, "in distress": 0.0}


def wellbeing(overall):
    return _WELLBEING.get(overall, 0.66)   # unknown/absent -> neutral


class RewardLearner:
    def __init__(self, *, bus=None, neuromod=None, config=None, alpha=0.3, replay_alpha=0.15,
                 value_path=None, experience_path=None, lessons_path=None, habits_path=None,
                 habituation_path=None, max_values=2000, max_experiences=600, save_every=10,
                 lesson_min_count=3, lesson_min_abs=0.25, lessons_top=8,
                 habituation_enabled=False, habit_floor=HABIT_FLOOR,
                 habit_decay_per_rep=HABIT_DECAY_PER_REP, habit_recovery_s=HABIT_RECOVERY_S,
                 habit_max_pairs=HABIT_MAX_PAIRS):
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
        # Habituation knobs — read off config when present (flag-dark like its reward-adjacent
        # neighbours), else the module defaults / explicit kwargs (tests pass them directly).
        if config is not None:
            habituation_enabled = getattr(config, "nervous_habituation_enabled", habituation_enabled)
            habit_floor = getattr(config, "nervous_habituation_floor", habit_floor)
            habit_decay_per_rep = getattr(config, "nervous_habituation_decay_per_rep", habit_decay_per_rep)
            habit_recovery_s = getattr(config, "nervous_habituation_recovery_s", habit_recovery_s)
        self.habituation_enabled = bool(habituation_enabled)
        self.habit_floor = max(0.0, min(1.0, float(habit_floor)))
        self.habit_decay_per_rep = max(0.0, min(1.0, float(habit_decay_per_rep)))
        self.habit_recovery_s = max(1.0, float(habit_recovery_s))
        self.habit_max_pairs = int(habit_max_pairs)
        if config is not None:
            sd = config.state_dir
            value_path = value_path or str(sd / "learned_values.json")
            experience_path = experience_path or str(sd / "experience.jsonl")
            lessons_path = lessons_path or str(sd / "learned_lessons.json")
            habits_path = habits_path or str(sd / "learned_habits.json")
            habituation_path = habituation_path or str(sd / "habituation.json")
        self.value_path = value_path
        self.experience_path = experience_path
        self.lessons_path = lessons_path
        self.habits_path = habits_path
        self.habituation_path = habituation_path

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
        # Habituation ledger (persisted): (signature\x00target) -> {"reps": float, "t": epoch_seconds}.
        # `reps` is the time-decayed count of recent same-shape-same-target successes; it drives the
        # success-channel attenuation and recovers toward 0 between touches. Bounded + fail-open.
        self._habituation: dict = {}
        self._habit_since_save = 0
        self._load()

    # ---- reward function -------------------------------------------------------------
    def reward_of(self, *, success, made_progress, felt_delta, valence, strain, intrinsic=0.0,
                  can_fail=True, success_scale=1.0):
        """The scalar reward in [-1, 1]. Pure + testable. `can_fail` gates the success channel: a
        riskless action (can_fail=False) scores 0 on success — an outcome never in doubt is no signal.
        `success_scale` in [floor,1] attenuates only the SUCCESS reward (habituation: the Nth identical
        success is worth less than the first) — it never touches the failure penalty, so a failure
        always teaches fully; a repeated success just stops paying its full novelty premium."""
        if can_fail:
            r = (W_SUCCESS * max(0.0, min(1.0, float(success_scale))) if success else -W_SUCCESS)
        else:
            r = 0.0
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

    # ---- habituation / novelty pressure ----------------------------------------------
    @staticmethod
    def _habit_key(sig, target) -> str:
        return f"{sig or ''}\x00{target or ''}"

    def _decayed_reps(self, entry, now) -> float:
        """The stored repeat count, recovered for the wall-clock time since it was last touched
        (dishabituation): shed one rep per `habit_recovery_s` elapsed, floored at 0. Event-free —
        the passage of time IS the recovery; we read it lazily instead of ticking a timer."""
        reps = float(entry.get("reps", 0.0))
        elapsed = max(0.0, float(now) - float(entry.get("t", now)))
        return max(0.0, reps - elapsed / self.habit_recovery_s)

    def _habituation_scale(self, sig, target, *, success, record=True):
        """The success-channel multiplier in [floor, 1] for this (signature, target). A never-seen or
        long-unseen pair returns ~1.0 (full novelty premium); each fresh REPEAT drives it geometrically
        toward the floor. When `record` is True (the live path) the successful touch is counted and the
        pair's timestamp advanced; a FAILURE never accrues habituation (only rewards habituate, so we
        never dull a warning). Bounded, pure of any content."""
        if not self.habituation_enabled:
            return 1.0
        key = self._habit_key(sig, target)
        now = _now()
        entry = self._habituation.get(key)
        reps_now = self._decayed_reps(entry, now) if entry else 0.0
        # scale is computed from the reps ALREADY accumulated (before this touch) so the first
        # occurrence of a fresh pair always pays full.
        scale = self.habit_floor + (1.0 - self.habit_floor) * (self.habit_decay_per_rep ** reps_now)
        if record and success:
            self._habituation[key] = {"reps": round(reps_now + 1.0, 4), "t": now}
            self._evict_habituation_if_needed()
        return max(self.habit_floor, min(1.0, scale))

    def _evict_habituation_if_needed(self):
        if len(self._habituation) <= self.habit_max_pairs:
            return
        # drop the least-recently-touched pairs (they are also the most recovered → least informative)
        victims = sorted(self._habituation.items(), key=lambda kv: kv[1].get("t", 0))
        for k, _ in victims[: len(self._habituation) - self.habit_max_pairs]:
            self._habituation.pop(k, None)

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

        # The action reaches us as a content-bearing label; everything the LEARNER keeps must be the
        # content-free ACTION SHAPE (tool + arg signature) so no content payload ever enters the value
        # cache, a lesson, or a habit. `sig` keys the value cache (so write_file<fiction> and
        # write_file<grocery list> collapse to one shape-level entry) and renders in lessons; `target`
        # (also content-free) distinguishes same-shape-same-target rehearsal from same-shape-new-target
        # exploration for the habituation pressure.
        sig = action_signature(action)
        target = action_target(action)
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
        # HABITUATION: attenuate the success channel by how rehearsed this (shape, target) is. The Nth
        # identical success against the same target pays a shrinking fraction of the novelty premium; a
        # new shape or new target pays full, so exploration out-competes rehearsal. A failure never
        # habituates (record only on success). Applied under the lock so the count update is atomic.
        with self._lock:
            success_scale = self._habituation_scale(sig, target, success=success, record=could_fail)
        reward = self.reward_of(success=success, made_progress=made_progress,
                                felt_delta=felt_delta, valence=valence, strain=strain,
                                intrinsic=intrinsic, can_fail=could_fail, success_scale=success_scale)
        key = self._key(situation, sig, overall)
        with self._lock:
            entry = self.values.get(key)
            predicted = entry["v"] if entry else 0.0
            rpe = reward - predicted
            v_new = predicted + self.alpha * rpe
            self.values[key] = {"v": round(v_new, 4), "n": (entry["n"] + 1 if entry else 1),
                                "situation": self._situation_label(situation, overall),
                                "action": sig, "t": _now()}
            tag = abs(rpe) * (0.5 + 0.5 * arousal)
            rec = {"tick": tick, "key": key, "situation": self._situation_label(situation, overall),
                   "action": sig, "reward": round(reward, 4), "predicted": round(predicted, 4),
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
            self._habit_since_save += 1
            do_save_habit = self.habituation_enabled and self._habit_since_save >= self.save_every

        self._append_experience(rec)
        if do_save_habit:
            self._save_habituation()
            self._habit_since_save = 0
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
        if self.habituation_enabled:
            self._save_habituation()
            self._habit_since_save = 0
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
            # Render the ACTION SHAPE only — re-run through action_signature so even a legacy value file
            # (whose "action" still holds a raw content-bearing label) can never emit a content payload
            # back into the coaching head. A lesson coaches "this KIND of action lands", never a string.
            act = action_signature(e.get("action")) or "that"
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
            # ACTION SHAPE only (see _distill_lessons) — a habit is "reach for this KIND of action",
            # never "reach for this exact content string".
            act = action_signature(e.get("action")) or "do that"
            out.append(f"When {sit}: you reliably \"{act}\" (v={e['v']:+.2f}, n={e['n']}).")
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
                    "habituation_pairs": len(self._habituation),
                    "habituation_enabled": self.habituation_enabled,
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
        if self.habituation_path and os.path.exists(self.habituation_path):
            try:
                with open(self.habituation_path, encoding="utf-8") as f:
                    d = json.load(f)
                self._habituation = d if isinstance(d, dict) else {}
            except Exception:  # noqa: BLE001 - fail-open: a corrupt/missing ledger is an empty one
                self._habituation = {}

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

    def _save_habituation(self):
        if not self.habituation_path:
            return
        with self._lock:
            data = json.dumps(self._habituation, ensure_ascii=False)
        self._atomic_write(self.habituation_path, data)

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
