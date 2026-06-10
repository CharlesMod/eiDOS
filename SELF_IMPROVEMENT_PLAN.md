# eiDOS In-App Self-Improvement — Unified Implementation Plan

> **RECONCILIATION (2026-06-10, eiDOS v2 phase 0n).** This plan was written for an
> adversary-resistant build. What shipped is a deliberate **accident-safety subset**
> (Dean's decision; see ARCHITECTURE_PRINCIPLES.md #3, selfedit.py:12, git_safety.py:7):
> git-reversible protection against a *confused* agent, not a wall against a malicious one.
> Read the plan below with these standing amendments:
>
> - **Group A (OS isolation, localhost bind, ACLs, state relocation, HMAC) is DEFERRED by
>   decision**, not pending. The "must land before self_edit_enabled" gate at the bottom of
>   this doc is superseded for the accident-safety posture.
> - **Branch discipline: commits land on `main`** (Dean, 2026-06-09). The `eidos-self`
>   branch was never used; its config knob was removed in v2 phase 0k.
> - **Auth posture (v2 phase 8): keep 0.0.0.0 + enforce one token uniformly on EVERY
>   state-changing POST** — including `/api/control/*`, `/api/chat`, `/api/speech/*`,
>   which today are ungated even when a token is set.
> - **Still genuinely missing (v2 phase 8 builds them):** the post-apply health-probe leg
>   (`pending_apply` marker → `applied_ok` breadcrumb → heartbeat-newer-than-baseline →
>   auto-rollback; the `self_edit_health_probe_s` knob exists and is read by nothing), and
>   the §7 test suite for selfedit/git_safety/watchdog-rollback.
> - The watchdog's plain crash-respawn does NOT boot paused (only operator start and
>   apply/restore restarts do) — that is intentional continuity, and docs that said
>   otherwise have been corrected.

## 1. Architecture & Safety Boundary

**Core principle: eiDOS PROPOSES, the operator-controlled dashboard APPLIES.** The system splits along the existing trust boundary between two processes:

- **eiDOS (`eidos.py`, untrusted child)** — only ever *writes proposal blobs into `workspace/proposals/`* via sanctioned tools (`propose_self_edit`, `propose_skill`, `update_self_guide`). It never touches real source, never runs git, never restarts itself, never writes any privileged flag. It only *reads* gates (`chat_hold.json`, self-guide live file).
- **Dashboard (`dashboard.py`, port 8099, operator-controlled parent)** — the *only* actor that validates, shows Dean a diff, git-checkpoints, copies staged files onto the live tree, commits, and restarts eiDOS by killing its PID so the existing `_watchdog_loop` respawns it (start-PAUSED). It owns `git_safety.py` / `selfedit.py`.

**Five existing safety invariants are preserved, not bypassed:** `protected_patterns`, kill-switch (`_ctrl_stop` taskkill `/T /F`), start-PAUSED (`_ctrl_start` writes `paused`), watchdog `eidos.should_run` flag, and the 5-in-180s crash-loop lockout (`dashboard.py:2255`).

**The safety boundary is enforced at the OS/filesystem layer, not by regex.** The risk reviews *verified* the design's central claim is false: `protected_patterns` (`config.toml:45-64`) does **not** block `git`, `Set-Content`, `Remove-Item`, `echo >>`, or `Invoke-WebRequest` to localhost. Regex command-blocking is bypassable (env vars, encoding, aliasing, `subprocess`, `getattr`, base64) and a skill can `open(...).write(...)` arbitrary source. Therefore the **primary control is: run `eidos.py` under a separate low-privilege Windows account with READ-ONLY ACLs on the Kairos source tree and on the dashboard's authoritative state dir; only `workspace/proposals/` and runtime workspace state are writable.** In-process allowlists, `RESERVED_NAMES`, and `protected_patterns` additions are *defense-in-depth only*.

**Authoritative state moves out of eiDOS-writable space.** `last_good`, `.rollback_attempted`, the self-edit `pending_apply` marker, and the kill-switch/pause/`should_run`/pid sentinels move into a dashboard-only ACL'd dir (`STATE_DIR`, e.g. `C:/Users/cmod/llm/Kairos/.dashboard_state/`) that eiDOS cannot write. `last_good` is HMAC-signed or derived only from git refs eiDOS cannot push.

**Off-limits files** (hard-coded `frozenset` literal in *both* `selfedit.py`/`git_safety.py` and the dashboard apply allowlist; config may only *narrow*, never *widen*): `dashboard.py`, `git_safety.py`, `selfedit.py`, `safety.py`, `atomicio.py`, `config.py`, `config.toml`, `.gitignore`, `llm.py`, and `skills.py` (the validator for the other untrusted code path — editing it is privilege escalation).

**Git discipline:** all self-edit commits land on a local `eidos-self` branch; **never `git push`** (asserted by test); **never `git reset --hard`** (use `git revert` or per-file `git checkout <sha> -- <target>`); scope commits to the single target file with `:(exclude)workspace/` so `goal.md` and the 8 currently-dirty files are never swept or clobbered.

---

## 2. Build Order

Ordered so the **git safety net, auth/isolation, and listening hold land BEFORE any self-code-editing is enabled** (`self_edit_enabled=False` until step group F passes its tests).

### Group A — Harden the boundary (prerequisite for everything)
1. **OS isolation.** Create a low-privilege Windows account for `eidos.py`; set READ-ONLY ACLs on all `*.py` under `Kairos/`, `config.toml`, `.gitignore`, and on the new `STATE_DIR`. Grant write only to `workspace/` minus `STATE_DIR`. Update `deploy/eidos.service` (or the nssm service) to run as that account; dashboard runs as the owner account.
2. **Dashboard auth + localhost bind.** In `dashboard.py :: main()` (~2342) change `ThreadingHTTPServer(('0.0.0.0', port), …)` → `'127.0.0.1'`. Add a shared-secret token loaded from a file readable only by the operator account (never under `workspace/`, never eiDOS-readable). Add a `_require_auth(self)` helper checked at the top of every **state-changing POST** (`/api/git/*`, `/api/selfedit/*`, `/api/proposals/*`, `/api/self_guide`, `/api/control/*`, `/api/chat_hold`), plus an `Origin`/CSRF check. Return 401 on failure.
3. **Process-wide lock.** Add `_LIFECYCLE_LOCK = threading.RLock()` module-level in `dashboard.py`. Acquire it at the top of every apply/restore/checkpoint/approve/auto-rollback path and in the watchdog respawn/rollback branch. Reject a second privileged op in flight with 409.
4. **State relocation.** Add `Config.state_dir` property → `STATE_DIR` (NOT under `workspace/`). Move `eidos.should_run`, `paused`, `eidos.pid` reads/writes (`_eidos_should_run_path:2130`, `_ctrl_start:2155`, `_ctrl_stop:2166`, `_ctrl_pause:2200`) to `state_dir`. Update `eidos.py` pause check (`421`) accordingly. **Test:** assert no eiDOS-callable tool can write any path under `state_dir` or any `*.py`/`config.toml`.
5. **Defense-in-depth regex.** Add to `config.toml` `protected_patterns`: `\bgit\b`, `Set-Content`, `Add-Content`, `Out-File`, `Remove-Item`, `\brm\b`, `del\b`, `Move-Item`, redirection `>>?\s`, `Invoke-WebRequest`, `Invoke-RestMethod`, `iwr`, `irm`, `curl`, `wget`, and the dashboard `host:port`.

### Group B — Git safety net (`git_safety.py`)
6. **New file `C:/Users/cmod/llm/Kairos/git_safety.py`** (off-limits; imported by `dashboard.py` only). Functions: `_repo_root()`, `_run_git(config, *args, timeout=30)` (`subprocess.run`, `cwd=repo root`, `shell=False`, no network), `ensure_self_branch(config)` (idempotent, refuses dirty HEAD), `last_good_tag(config)`, `make_checkpoint(config, label)`, `git_log_summary(config)`, `prune_checkpoints(config, keep=20)` (never prunes active `last_good`). All wrap try/except → `{ok:False,error}`, never raise to handler; full traceback logged server-side.
7. **`config.py`** — add fields after `mock_mode` (~133): `git_safety_enabled: bool=True`, `git_self_branch:str='eidos-self'`. Add `git_protect_paths` as a **code-resident frozenset** (config may only intersect). Add properties `proposals_dir`, `state_dir`, `git_state_dir`. Parse `[git]` in `load_config()` with `.get()` defaults. `config.toml` — add `[git]` section.
8. **`make_checkpoint` correctness:** single-file scope `git add -- <target>` (or `git add -A :(exclude)workspace/` for the operator's intentional first checkpoint), commit on `eidos-self`, create lightweight tag `eidos-good-<ts>`, write its name **atomically** (`atomicio.replace_with_retry`) to `state_dir/last_good` with an HMAC sidecar. **Fail closed if working tree dirty** (require operator commit/stash first).

### Group C — Proposal staging tool (`propose_self_edit`) + shared schema
9. **New file `C:/Users/cmod/llm/Kairos/selfedit.py`** (off-limits; dashboard-side privileged helpers `apply`/`autorollback` never called from a tool). eiDOS-side `propose(config, target_file, new_content, rationale, base_sha)`: canonicalize `target_file` against repo root (reject `..`, absolute, drive-letter, UNC, symlink; case-insensitive PROTECT match; prefix-match protected dirs; must be git-tracked `.py` in the editable allowlist and NOT off-limits), `py_compile` in-memory, compute unified diff, write `workspace/proposals/<id>.staged.py` + `<id>.diff` + manifest entry `status='pending'`, `base_sha=HEAD`. **No `_dry_run` execution at propose time** (see step 24). Manifest helpers mirror `skills.py`, using `replace_with_retry` + a cross-process lockfile.
10. **`tools.py`** — add `tool_propose_self_edit(args, config)` near `tool_write_file` (~291) as a thin wrapper to `selfedit.propose`; arg shape `{target_file:str, new_content:str (FULL file), rationale:str, base_sha?:str}`. Add read-only `tool_list_self_edit_proposals`. Register both in `TOOLS` (887-905). Add both names to `skills.RESERVED_NAMES` (`skills.py:40`). **Also add a `RESERVED_NAMES` basename guard inside `tool_write_file`** rejecting writes to `self_guide.md`, `goal.md`, and any path resolving under `state_dir` (closes the parallel write-path hole).
11. **`prompts.py`** — one line to `SYSTEM_PROMPT_BRIEFING` standing orders: "To change your own source use `propose_self_edit(target_file, new_content, rationale)`. It stages a proposal; Dean approves and the dashboard applies + restarts you. You cannot edit source, run git, or restart yourself."

### Group D — Listening hold (lands before self-edit; pure idle gate)
12. **`config.py`** — `chat_hold_ttl_s: float=60.0`, `chat_hold_max_continuous_s: float=300.0`, optional `chat_hold_path` property under `state_dir`. Load from `[tick]`/`[chat]` with env override.
13. **`dashboard.py` `do_POST`** — add `/api/chat_hold` (before `/api/control/start` ~2076): parse `{held}`; on `true` write `state_dir/chat_hold.json` `{held, ts, first_held_ts, source:'chat_focus'}` via `replace_with_retry` **carrying forward `first_held_ts` if the file already exists and is fresh**; on `false` unlink. **Wrap write+unlink in try/except OSError**, FileNotFoundError-on-unlink == success, return `{ok:false}` instead of 500. Optional debug `GET /api/chat_hold`.
14. **`eidos.py`** — add `_chat_hold_active(config)->bool` near `_has_pending_interventions` (~144): read `state_dir/chat_hold.json`; return `False` on missing/corrupt/not-held; `age=min(now-ts, now-mtime)`; **`if age<0: return False`** (backward-clock clamp); `if age>ttl: return False`; **`if now-first_held_ts > chat_hold_max_continuous_s: return False`** (hard ceiling); return `False` if `_has_pending_interventions(config)`. **eiDOS does NOT unlink** (single-writer invariant — dashboard owns the file lifecycle).
15. **Loop integration** — in `run_loop` add local `listening_state=False` (~358); insert listening gate **AFTER the operator-pause block** (after 442, before goal read 444): if held → `write_activity('listening', detail=f'listening {int(age)}s')` once, `logger.info` resume notes (NOT `append_observation` — avoids context pollution), back off sleep from 2s→5s once stable, `continue`. On the **operator-pause path, reset `listening_state=False`** so the bubble can't be left blue under a red pause. Early-wake: in `_interruptible_sleep` after the intervention check (~157) add `if _chat_hold_active(config): break`.
16. **UI** — focus/blur listeners on `#chat-input` (~1964) POSTing the hold + a 20s while-focused refresh interval; `sendChat()` POSTs `held:false` after a successful send. Add `listening` branches to `updateThoughtBubble`/`updateToolBubble` (blue `#33bbff`, glyph, `tb-pulse`), `.state-listening` CSS. **Do NOT add `listening` to the 500ms fast-poll `active` set (1543)** — it is idle; keep slow cadence. Refresh listening activity heartbeat every ~15s in-gate so a real wedge is distinguishable from a healthy hold.

### Group E — Git apply/restore/auto-rollback endpoints + watchdog
17. **`dashboard.py` `do_GET`** — add `/api/git/log` → `git_safety.git_log_summary(config)`.
18. **`dashboard.py` `do_POST`** (under lock + auth) — `/api/git/checkpoint` → `checkpoint_endpoint`; `/api/git/apply` body `{proposal_id}` → `selfedit.apply`; `/api/git/restore` body `{tag?}` → `restore_last_good`. All call the **same private restart helper** used by `_ctrl_stop`/`_spawn_eidos`.
19. **Restart helper** — write `paused` (boot PAUSED), ensure `should_run` armed, `taskkill /PID <pid> /T /F`, **immediately unlink `eidos.pid` and invalidate `_pid_cache`** so `_ctrl_pid_alive` is deterministically False; watchdog respawns. Poll `tasklist` to confirm dead before respawn (no duplicate eidos).
20. **`restore_last_good`** — per-file `git checkout <tag> -- <file>` over tracked files **excluding PROTECT_PATHS**; **also `git rm`/delete files created after the tag** (not protected) so the tree truly matches; run a `python -c "import dashboard, eidos"` subprocess **smoke check before respawn** — refuse to boot a tree that fails; commit `restore: rolled back to <tag>`; restart.
21. **Watchdog auto-rollback — implemented INSIDE `_watchdog_loop`** (where the local `restarts` list lives, `dashboard.py:2241`), NOT in a separate module. On ≥5 restarts/180s: if `last_good` resolvable AND `.rollback_attempted` (in `state_dir`) not set AND a wall-clock cap (≥N rollbacks/hour) not exceeded → set flag, `git checkout`-revert source minus PROTECT_PATHS to `last_good`, `_watchdog_note`, **clear `restarts=[]` in this same scope**, require the respawn to survive a minimum stable window (~60s) before `restarts` may re-grow, re-arm `should_run`, respawn. Else stand down (unlink `should_run`). **Mandatory `PushNotification` on every rollback and stand-down** (console tail + tag). Clear `.rollback_attempted` on next operator `_ctrl_start`.
22. **`last_good` advancement gated** — only the PRE-change checkpoint is tagged automatically; `last_good` advances to a POST-change commit **only after operator "mark healthy" + M stable ticks**, never auto-advance (prevents lowering the floor to a bad-but-non-crashing change).

### Group F — Gated self-code-edit pipeline (enable LAST)
23. **`config.py`** — `[self_edit]` section: `self_edit_enabled:bool=False` (opt-in), `self_edit_editable_files` (eidos.py, prompts.py, context.py, tools.py, parser.py, persona.py, memory.py, compaction.py, knowledge.py, telemetry.py, env_snapshot.py, rotation.py, session.py, ascii_art.py — **NOT skills.py**), off-limits frozenset (code-resident), `self_edit_max_proposal_bytes:int=200000`, `self_edit_health_probe_s:int=90`, `self_edit_max_pending:int=5`.
24. **`selfedit.apply(config, proposal_id)`** (dashboard-only, under lock) — re-canonicalize + re-validate path; re-`py_compile`; **reject if working tree dirty in off-limits/non-target files** (`git status --porcelain`); re-verify `base_sha == HEAD` (else stale → reject); `make_checkpoint` (single-file scope); **write `state_dir/selfedit.pending_apply` `{id, prev_sha, baseline_heartbeat_ts, deadline_epoch}` BEFORE copying**; copy staged→live via `replace_with_retry`; hash-verify live==staged; commit `self-edit apply <id> prev=<sha>` (prev_sha in message for workspace-wipe recovery); mark `applying`; restart. If propose-time `_dry_run` is needed, run it here (post-approval) in the sandboxed low-priv account, network-denied, tmpdir cwd, resource-limited, throwaway config.
25. **`eidos.py` liveness breadcrumb** — in `run_loop` **after `recover()` returns but BEFORE the LLM-health-wait and BEFORE the pause `continue` (line 433)**, if `pending_apply` exists write `state_dir/selfedit.applied_ok` with the id (a paused eiDOS never reaches the post-tick heartbeat, so probe must key on an early breadcrumb). eiDOS does no privileged work.
26. **Watchdog health-probe window** — on respawn, if `pending_apply` present, require `applied_ok` (created post-deletion by the new boot) **and** a heartbeat ts **strictly newer than `baseline_heartbeat_ts`** within `self_edit_health_probe_s`; treat `paused`-reached-loop as healthy. On timeout OR crash-loop → `selfedit.autorollback` (terminal: `git revert`/single-file checkout to `prev_sha`, status `rolled_back`, clear marker, **DISARM `should_run`** — no auto-re-probe, no git loop, PushNotification).
27. **Dashboard reconcile on boot** — in `main()` **before arming watchdog/`serve_forever`**: if an unresolved `pending_apply` past deadline exists (or git log shows an apply commit lacking a healthy boot), autorollback immediately. Mark all other pending proposals on the same target `needs_rebase` on a successful apply.
28. **Endpoints** — `GET /api/selfedit/list`, `GET /api/selfedit/diff?id=` (render from CURRENT live file at approve time, not propose-time `.diff`), `POST /api/selfedit/approve {id}`, `POST /api/selfedit/reject {id, reason?}`, `GET /api/selfedit/status`. HTML "Self-Edit Proposals" panel with `confirm()` on Approve. **Approval authority is a dashboard-held signature/log in `state_dir`, NOT the proposal JSON `status`** (eiDOS can write any file under `workspace/`); approve only hot-loads if a valid operator signature over the exact code-hash exists.
29. **Tests** (`tests/test_selfedit.py`) green → flip `self_edit_enabled=True`.

### Group G — Living self-guide (Tier 1)
30–35. See §6.

### Group H — Chat-coached skills (Tier 2)
36–40. See §6.

---

## 3. Emergency Git Tracking + Restore

**Checkpoint strategy.** Every operator-approved apply is bracketed: `make_checkpoint('pre-apply')` commits the current single target file on the local `eidos-self` branch, creates lightweight tag `eidos-good-<ts>`, and atomically records the tag name to `state_dir/last_good` (HMAC sidecar). Checkpoints use single-file scope (`git add -- <target>`, `:(exclude)workspace/`) so `goal.md` and unrelated dirty files are never swept. Pruning keeps the last 20 `eidos-good-*` tags and **never** the active `last_good`. First checkpoint fails closed on a dirty tree.

**Restore control/endpoint.** `POST /api/git/restore {tag?}` (auth + lock) → `restore_last_good`: resolve tag (default `last_good`), per-file `git checkout <tag> -- <file>` over tracked files **excluding PROTECT_PATHS**, **delete post-tag-created non-protected files** so the tree truly matches, run the `import dashboard, eidos` **smoke check before respawn**, commit `restore: rolled back to <tag>`, then restart via the shared paused-restart helper. A manual "Restore last good" button + git-state span in the dashboard control row drives it; `GET /api/git/log` feeds the panel.

**Watchdog crash-loop auto-revert.** Implemented inside `_watchdog_loop` (step 21): ≥5 restarts/180s → one-shot rollback to `last_good` (guarded by `.rollback_attempted` in `state_dir`, plus a hard cap of N rollbacks/wall-clock-hour so a deleted/forged flag can't loop), revert source minus PROTECT_PATHS, clear the in-scope `restarts` list, require a 60s stable window before re-counting, re-arm `should_run`, respawn, and **PushNotification** the operator. If still crash-looping (flag set) → stand down (unlink `should_run`) + notify. PROTECT_PATHS (`dashboard.py`, `git_safety.py`, `selfedit.py`, config/safety) are **never** reverted — a malicious old checkpoint cannot downgrade the kill-switch or the rollback machinery.

---

## 4. Gated Self-Code-Edit Pipeline (end to end)

```
PROPOSE (eidos)  →  REVIEW (operator)  →  APPROVE  →  CHECKPOINT  →  APPLY
                                                                       ↓
                          AUTO-ROLLBACK ←─ unhealthy ─── RESTART (paused) ─── healthy → mark applied
```

1. **Propose** — eiDOS calls `propose_self_edit{target_file, new_content (FULL), rationale, base_sha}` → `selfedit.propose`: canonicalize + allowlist/off-limits check, `py_compile`, diff, write `workspace/proposals/<id>.{staged.py,diff}` + manifest `pending`. Caps: `max_pending=5`, `max_proposal_bytes` enforced **before** disk write, disk-space precheck. eiDOS blocks here.
2. **Review** — `GET /api/selfedit/list` surfaces pending; `GET /api/selfedit/diff?id=` renders the diff **from the current live file at approve time** (TOCTOU-safe). UI flags `stale` when `base_sha != HEAD`.
3. **Approve** — `POST /api/selfedit/approve {id}` (auth + lock). Authority = dashboard signature over the code-hash in `state_dir`, not the proposal `status`.
4. **Compile-check + apply transaction** (`selfedit.apply`, atomic, single-file scoped): re-`py_compile`; reject if tree dirty in off-limits/non-target files; re-verify `base_sha==HEAD`; checkpoint; **write `pending_apply` marker BEFORE copy**; copy staged→live (`replace_with_retry`); hash-verify; commit `self-edit apply <id> prev=<sha>`.
5. **Commit** — checkpoint + apply commits are **local only on `eidos-self`** (no push — asserted by test).
6. **Restart** — shared helper: write `paused`, arm `should_run`, `taskkill /T /F`, unlink pid + invalidate cache; watchdog respawns on new code, PAUSED.
7. **Auto-rollback** — watchdog enters health-probe: needs `applied_ok` + heartbeat ts > `baseline` within 90s (paused-reached-loop counts healthy). On timeout/crash-loop → `selfedit.autorollback` (terminal `git revert`/single-file checkout to `prev_sha`, smoke-check, disarm `should_run`, PushNotification). Dashboard `main()` reconciles a dangling `pending_apply` on its own restart before arming the watchdog.

**Off-limits file list** (code-resident frozenset, config may only narrow): `dashboard.py`, `git_safety.py`, `selfedit.py`, `safety.py`, `atomicio.py`, `config.py`, `config.toml`, `.gitignore`, `llm.py`, **`skills.py`**. Enforced at propose, at apply re-validation, and ultimately by the read-only ACL on the source tree.

---

## 5. Listening Hold (soft pause)

**Flag.** `state_dir/chat_hold.json` `{held, ts, first_held_ts, source}` — **written only by the dashboard** (single-writer invariant), read-only to eiDOS. Freshness = `min(now-ts, now-mtime) ≤ chat_hold_ttl_s` (60s); negative age → stale (clamp); `now-first_held_ts > chat_hold_max_continuous_s` (300s) → force-expire regardless of refresh (defeats a stuck/hostile refresher pinning the loop).

**Wiring.** `POST /api/chat_hold {held}` (auth, try/except-wrapped, idempotent unlink). Focus listener on `#chat-input` + 20s while-focused refresh (carries `first_held_ts` forward); blur and `sendChat` post `held:false`.

**Loop integration.** `_chat_hold_active(config)` placed in the gate **after operator-pause, before goal-read** (`eidos.py` after 442). The gate `continue`s at the top of the next iteration → the in-flight tick finishes, no new generation starts. `_interruptible_sleep` early-wakes on the hold (`~157`). A pending intervention overrides the hold (`_has_pending_interventions` short-circuits).

**Activity state.** `write_activity('listening', detail='listening Ns')` refreshed ~15s in-gate (distinguishes healthy hold from a wedge). Blue `#33bbff` bubble, distinct from red `paused`. Operator-pause path resets `listening_state` so the bubble can't latch blue under a red pause. Resume notes use `logger.info`, not `append_observation` (no context pollution).

**Auto-expire.** TTL (60s, cooperative client walks away) + hard ceiling (300s, any client). eiDOS treats stale as absent; the dashboard owns unlink. Fails open to autonomy on any anomaly (corrupt, negative age, missing). Never touches watchdog/should_run/paused/source/git.

---

## 6. Living Self-Guide (Tier 1) + Tier 2 Reuse

### Tier 1 — `self_guide.md`
Operator-owned standing-directives doc (~1500 char), injected every tick. Two asymmetric write paths: Dean edits directly via dashboard textarea (canonical); eiDOS only **proposes** via `update_self_guide` into a staging file.

30. **New files** `workspace/self_guide.md` (live, Dean-owned, seeded with Identity / How I think / Hard limits / Focus / Lessons), `workspace/self_guide_proposed.md` (staging, never injected), `workspace/self_guide_proposals.jsonl` (audit, **rotated** like `observations.jsonl` to avoid disk-fill).
31. **`config.py`** — `context_self_guide_max_chars:int=1500`, `self_guide_max_bytes:int=4000`, properties `self_guide_path`, `self_guide_proposed_path`, `self_guide_proposals_path`. Confirm budget fits the ~6500 ceiling (Mission 2000 / Plan 800 / Subgoals 1500 / Intelligence 1200 already sum high → consider 1000).
32. **`memory.py`** — `read_self_guide(config)`: **`read_text(encoding='utf-8', errors='replace')` wrapped in `try/except (FileNotFoundError, UnicodeError, OSError) → ''`** (one non-cp1252 byte must not brick every tick). `write_self_guide` / `write_self_guide_proposal`: atomic `mkstemp`+`replace_with_retry`, `encoding='utf-8'`.
33. **`context.py` `_assemble_briefing`** — inject the self-guide **AFTER mission/plan/conversation** (not at `durable[1]`) so `_enforce_ceiling` (293-308, trims from the bottom) drops the self-guide *first* and never starves Mission/Conversation; header labels it "Dean-owned, you may PROPOSE via `update_self_guide`". `_truncate` to budget. Skip if empty. Patch `prompts._assemble_legacy` for parity.
34. **`tools.py`** — `tool_update_self_guide{content? | note?, rationale?, source_tick?}` writes **only** `self_guide_proposed.md` + appends jsonl; never `write_self_guide`; never delegates to `tool_write_file`; rate-limited (ignore if identical to current proposed). **Add `self_guide.md`/`goal.md` basename guard to `tool_write_file`** (closes the parallel un-gated write path — the real anti-brick fix).
35. **`dashboard.py`** — `GET /api/self_guide` (content + proposed + has_proposal + mtime; wrap reads in try/except, never `exists()`-then-read), `POST /api/self_guide {content}` (auth, atomic save, clears pending), `POST /api/self_guide/reject`. Prefer **manual review-and-save over one-click `/approve`**; if `/approve` is added, make it **content-addressed** (GET returns proposed hash; approve includes it; 409 if changed — TOCTOU defense). "Self-Guide" panel polled every 10s. **No endpoint here touches `_spawn_eidos`/`_ctrl_*`/`taskkill`** — pure file IO picked up next tick.

### Tier 2 — Chat-coached skills/nuggets (reuses the Tier-3 staging contract)
Tier 2 **converges on the same `workspace/proposals/` staging format and dashboard approval UI** built for self-edit rather than inventing a parallel mechanism.

36. **`context.py`** — `classify_intervention(content)` (cheap regex, COACHING vs CHAT); render `[Dean COACHING @ …]`; inject `read_coaching_directive(config)` (≤600 char, `try/except → ''`, mtime-cached) after the Conversation block. **Provenance: `dean_coached` derives ONLY from a dashboard `/api/chat`-ingress marker eiDOS cannot forge** (eiDOS can drop files into `interventions_dir`), never from filename/content.
37. **`tools.py`** — `tool_propose_skill{skill_name, skill_code, description, args_schema?, kind?}` → validates via `skills._validate_source` (AST/syntax only at propose time; **`_dry_run` deferred to post-approval, sandboxed** — staging must be inert) and writes a proposal to `proposals_dir`; never `skills._activate`. **Remove `create_skill`/`edit_skill` from the agent-facing `TOOLS` entirely** (make them the private impl callable only by the verified approve handler); drop `allow_direct_skill_writes`. Add `propose_skill` to `RESERVED_NAMES`.
38. **`dashboard.py`** — `GET /api/proposals`, `POST /api/proposals/approve {id}`, `POST /api/proposals/reject {id, reason?}` (auth + lock). Approve is the **only** hot-load site; re-validate against off-limits + path denylist; authority = signature in `state_dir`, not JSON `status`. **Resolve the cross-process activation gap:** the dashboard's `skills._activate` mutates the *dashboard's* TOOLS, not the running eiDOS — implement either an in-process `reload_active_skills()` diffing manifest mtime each tick in `run_loop`, or an approval-triggered controlled restart via the watchdog path. Do not write a "live next tick" observation until one exists.
39. **`prompts.py`** — "When Dean coaches you": a COACHING message is a standing instruction — persist it (`propose_skill` for a tool, `memorize` for a fact) and confirm via `<reply>`; **exactly one persistence action** per message; if unsure, ask. `memorize` stays immediate (reversible knowledge, never code).
40. **Manifest race fix** — convert `skills._save_manifest` (`80-84`) to `replace_with_retry` + cross-process lockfile (now written by both dashboard-approve and live-eidos `_record_invocation`); make approval atomic (skill file + single manifest swap, unlink-on-failure). **Poison-skill quarantine:** on crash-loop stand-down, disable in the manifest the skill version activated just before the deaths; `load_active_skills` isolates each load so one bad skill can't crash startup.

---

## 7. Test Plan

**Boundary / isolation (Group A)**
- Assert no eiDOS-callable tool (`write_file`, `bash`, `create_skill`, `propose_self_edit`, `update_self_guide`) can write any `*.py` under `KAIROS_DIR`, `config.toml`, or any path under `state_dir`. Run as the low-priv account.
- Every state-changing POST returns 401 without the token; dashboard refuses non-127.0.0.1 binds.
- `protected_patterns` blocks `git`, `Set-Content`, `Remove-Item`, `Invoke-WebRequest http://127.0.0.1:8099/...`.

**git_safety**
- Dirty tree → `enable`/first checkpoint fails closed.
- Pruning never removes active `last_good`; restore excludes PROTECT_PATHS; restore deletes post-tag files and the tree hashes equal to the tag.
- `import dashboard, eidos` smoke check blocks respawn on a broken tree.
- `selfedit.py`/`git_safety.py` contain no `git push` (asserted).

**Self-edit**
- Off-limits rejection (`dashboard.py`, `skills.py`), path-traversal (`./config.py`, `sub/../dashboard.py`, `Dashboard.py`, drive-letter, UNC, symlink), `py_compile` rejection, manifest round-trip, apply transaction ordering (mock git + taskkill), `base_sha` stale → reject, `autorollback` restores `prev_sha` and disarms `should_run`.
- STOP during health-probe → source rolled back, eiDOS NOT respawned, marker cleared.
- Crash between copy and marker-write is detectable (marker written first); dashboard `main()` reconcile rolls it back.

**Watchdog**
- `restarts` cleared in-scope after rollback; one-shot guard prevents re-rollback; wall-clock cap stands down; `PushNotification` fires on rollback + stand-down.

**Listening hold**
- Hostile refresher capped at `chat_hold_max_continuous_s`; backward-clock → stale; corrupt JSON → fails open; pending intervention overrides; operator-pause resets `listening_state`; eiDOS performs no writes except none (single-writer); no source/git/watchdog touched.

**Self-guide**
- Round-trip an emoji and a cp1252-incompatible char through propose→approve→inject without raising.
- `write_file{path:'self_guide.md'}` → `success=False`.
- Under-ceiling stress: self-guide trimmed before Mission/Conversation.
- `/approve` rejects (409) if proposed file changed after the diff GET.

**Tier 2**
- `propose_skill` never activates; staging executes no code (dry-run deferred).
- No agent-reachable path to `skills._activate` except verified approve.
- Forged `[Dean COACHING]` file in `interventions_dir` classifies as `self`, never `dean_coached`.
- Manifest `replace_with_retry` survives concurrent approve + `_record_invocation` (hammer test).
- Poison skill disabled on crash-loop; `load_active_skills` survives one bad skill.

---

## 8. Must-Fix Risks + Mitigations (deduped, by severity)

**CRITICAL**
1. **Regex command-blocking is NOT a security boundary (verified false claim).** `protected_patterns` allows `git`, `Set-Content`, `Remove-Item`, `echo >>`. → **OS-level isolation:** eiDOS runs under a low-priv account with READ-ONLY ACLs on the source tree + `state_dir`; regex/allowlists are defense-in-depth only. (Group A.1, A.5)
2. **Unauthenticated 0.0.0.0 dashboard = no approval gate.** All `/api/git/*`, `/api/selfedit/*`, `/api/proposals/*`, `/api/self_guide`, `/api/control/*` reachable by any LAN host. → Bind 127.0.0.1 + operator token + CSRF on every state-changing POST. (A.2)
3. **eiDOS self-approves via localhost POST** (`Invoke-WebRequest http://127.0.0.1:8099/api/git/apply`). → Token unreadable by eiDOS account + firewall/ACL the port from eiDOS + add the verbs to `protected_patterns`. (A.2, A.5)
4. **Approval authority lives in eiDOS-writable files** (`status='applied'` in proposal JSON; `last_good`/`.rollback_attempted` under gitignored `workspace/`). eiDOS forges them → infinite rollback or recovery to attacker code. → Move authority to a dashboard-only signed log/`state_dir`; HMAC `last_good`; hard cap rollbacks/hour. (A.4, B.8)
5. **Parallel un-gated write paths defeat every staging invariant** — `tool_write_file{path:'self_guide.md'}`, `create_skill` hot-load, `bash` writing source, editing `skills.py` to remove its own validator. → Basename guard in `tool_write_file`; remove `create_skill`/`edit_skill` from agent TOOLS; `skills.py` off-limits; ACL is the real backstop. (A.4, F.23, H.37)
6. **Cross-process activation gap** — dashboard `skills._activate`/TOOLS mutation does NOT reach the running eiDOS; "live next tick" is false. → In-process manifest-diff hot-reload or approval-triggered controlled restart; don't claim live until one exists. (H.38)
7. **Watchdog crash-loop state is in-memory** (`restarts` local, lost on dashboard restart) → bricked code respawns forever, rollback never fires. → Persist probe/rollback/crash-loop state in the `pending_apply` marker; reconcile in `main()` before arming watchdog. (E.21, F.27)

**HIGH**
8. **`git reset --hard` clobbers tracked `goal.md` + sweeps the 8 dirty files.** → Use `git revert`/single-file `git checkout`; scope commits to one file with `:(exclude)workspace/`; block apply on a dirty off-limits/non-target tree. (B.8, F.24)
9. **Auto-rollback impossible as specced** (separate module can't reset dashboard's local `restarts`). → Implement inside `_watchdog_loop`; gate respawn on a minimum stable window; make rollback terminal. (E.21)
10. **PAUSED/stale-heartbeat misread health** — paused eiDOS never writes post-tick heartbeat; old `heartbeat.json` (tick 747) looks fresh. → Early `applied_ok` breadcrumb before the pause `continue`; require heartbeat ts strictly > a pre-kill baseline. (F.25, F.26)
11. **No locking + ThreadingHTTPServer races** on git state, `last_good`, pid lifecycle, manifest. → `_LIFECYCLE_LOCK` RLock around all privileged ops; 409 on concurrent; `replace_with_retry` for markers; convert `skills._save_manifest` + cross-process lockfile. (A.3, H.40)
12. **Inconsistent/incomplete restore** — per-file checkout leaves post-tag files; protected/unprotected mismatch won't import. → Delete post-tag files; `import` smoke check before respawn. (E.20)
13. **Path-validation bypass** (substring vs canonical; `Dashboard.py`, symlink, UNC). → Canonicalize + case-insensitive PROTECT match + prefix-match dirs at BOTH propose and apply. (C.9, F.24)
14. **Kill-switch during probe leaves bricked source for next start.** → Check `pending_apply` BEFORE the `if not should_run: continue` early-out; rollback source even when disarmed, but don't resurrect. (F.26)
15. **`read_self_guide`/`read_goal` UnicodeDecodeError brick** — one emoji crashes every tick → watchdog stand-down. → `encoding='utf-8', errors='replace'`, catch `(FileNotFoundError, UnicodeError, OSError)`. (G.32)
16. **Self-guide context starvation** — injected at top, `_enforce_ceiling` trims Mission/Conversation first. → Inject after mission/plan/conversation; verify budget. (G.33)
17. **Propose-time arbitrary code execution** — `_dry_run` runs untrusted code before approval. → Defer dry-run to post-approval, sandboxed. (F.24, H.37)
18. **Forged `[Dean COACHING]` interventions** launder self-edits as operator-coached. → Provenance from an unforgeable `/api/chat` ingress marker only. (H.36)
19. **Listening-hold remote starvation** — TTL only protects cooperative clients; stuck refresher pins the loop. → Hard `chat_hold_max_continuous_s` ceiling via carried-forward `first_held_ts`. (D.14)
20. **Dirty-tree first checkpoint contaminates the restore floor.** → Fail closed on dirty tree; require operator commit/stash. (B.8)

**MEDIUM**
21. Auto-rollback at 3am with no operator (availability brick) → keep start-PAUSED + mandatory PushNotification. (E.21)
22. `last_good` auto-advance lowers the safety floor → advance only on operator "mark healthy" + M ticks. (E.22)
23. Approve TOCTOU (proposal swapped after diff read) → content-addressed `/approve` (409 on hash mismatch); prefer manual review-and-save. (G.35)
24. Listening single-writer violation (eiDOS unlinks stale flag) races dashboard refresh → eiDOS read-only; dashboard owns lifecycle. (D.14)
25. Broad try/except hides partial apply → make apply/restore transactional; roll back to checkpoint on mid-sequence failure; log full traceback server-side. (B)
26. Marker-after-copy power-loss window → write `pending_apply` BEFORE copy; embed `prev_sha` in commit message. (F.24)
27. Observation-log pollution from listening/coaching flapping → `logger.info` not `append_observation`; rate-limit; reset `listening_state` on pause. (D.15)

**LOW**
28. Checkpoint/tag accumulation → prune keep-N, never the active `last_good` (tested). (B.6)
29. `self_guide_proposals.jsonl` unbounded growth → rotate like `observations.jsonl`; dedupe identical proposals. (G.30)
30. Backward-clock makes hold stick → clamp negative age to stale. (D.14)
31. `git clean -x` wipes gitignored markers → store `prev_sha` redundantly in the apply commit message. (F.24)
32. Legacy-mode silent divergence (self-guide/coaching inactive if `briefing_model=False`) → patch `_assemble_legacy` for parity. (G.33)

---

## 9. Out of Scope / Deferred

- **Promote `eidos-self` → `main`** (a "promote to main" dashboard button); all self-edits stay local on `eidos-self` forever for now. No `git push` to either remote (`eidos`/`origin`).
- **Auto-resume-after-rollback policy** — default is start-PAUSED + notify; an operator-preconfigured "auto-resume if smoke check passes" is deferred.
- **Model-authored unified-diff apply** — full-file replacement only (safer); diff-apply deferred.
- **Routing `memorize` through staging** — stays immediate (reversible knowledge, never code).
- **Promotion pipeline** from repeatedly-recalled knowledge nuggets into self-guide "Lessons that became rules" — manual/proposal-only for Tier 1.
- **3-way merge on stale `base_sha`** — default is REFUSE + re-propose.
- **Diff-preview unified-diff rendering polish, presence breadcrumb** for "you have a pending proposal" — nice-to-have, not blocking.
- **Annotated-tag/reflog/gc hygiene** beyond keep-N pruning — operator-driven only.
- **Sandboxing the dry-run to a container/VM** (beyond the low-priv account + network-deny + resource limits) — deferred.

---

Note: the most load-bearing finding across all four risk reviews is that **the four `0.0.0.0`-no-auth + regex-only-blocking holes are real and verified, not hypothetical** — every "operator approves" gate is currently a no-op that eiDOS itself can POST through. Group A (OS isolation + auth + localhost bind + state relocation + lock) is the true prerequisite and must land and pass its tests before `self_edit_enabled` is ever flipped to `True`.