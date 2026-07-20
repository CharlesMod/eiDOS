#!/usr/bin/env bash
# Scheduled experience-curve run (WISDOM_PLAN §4). The 3-arm curve (naive-12b / wise-12b /
# naive-27b) needs the GPU to itself — the 27b arm evicts gemma — and the harness REFUSES while
# the eidos loop is live (WIS6). So this wrapper: pauses the creature, waits for its heartbeat to
# go stale, runs the curve, then restores the creature to living. Installed as the one-shot
# eidos-wisdom-curve.timer ~1 week after the 2026-07-20 wisdom activation, to capture the first
# wise-vs-naive datapoint. Safe to run by hand any time: `bash scripts/run_wisdom_curve.sh`.
set -uo pipefail
REPO=/home/cmod/Documents/Software/eiDOS
DASH=http://127.0.0.1:8099
cd "$REPO" || exit 1
LOG="$REPO/workspace/wisdom_curve_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$REPO/workspace"

log() { echo "[$(date -Is)] $*" | tee -a "$LOG"; }

restore() {
  # ALWAYS bring the creature back, even if the curve errored or was interrupted.
  log "restoring creature (start + resume)"
  curl -s -X POST "$DASH/api/control/start"  >/dev/null 2>&1
  sleep 8
  curl -s -X POST "$DASH/api/control/resume" >/dev/null 2>&1
  log "done — curve results in state/wisdom_curve.jsonl; full log at $LOG"
}
trap restore EXIT

log "pausing the creature for an exclusive-GPU measurement"
curl -s -X POST "$DASH/api/control/stop" >/dev/null 2>&1
# Wait past the harness's heartbeat-freshness window (WIS6 reads heartbeat.json; >90s stale = stopped).
sleep 100

log "running the 3-arm experience curve (naive-12b / wise-12b / naive-27b)"
PYTHONUTF8=1 "$REPO/.venv/bin/python" wisdom_curve.py --run >> "$LOG" 2>&1
rc=$?
log "curve exited rc=$rc"
# restore() fires on EXIT
exit "$rc"
