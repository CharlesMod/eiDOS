"""Pillars 0.4: workspace backups (backup.py) — snapshot / rotation / restore-verify.

eiDOS runs on frozen weights, so `workspace/` IS the individual. These tests pin the life-insurance
contract on a TINY FAKE workspace built in a temp dir (never the live workspace): a snapshot excludes
regenerable caches, restore-verify passes on a good snapshot, a deliberately-corrupted snapshot fails
verify with a clear reason, and daily×N / weekly×M rotation keeps the right set.
"""

import gzip
import json
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import backup
from config import Config


def _cfg() -> Config:
    c = Config()
    # Anchor the workspace under a fresh temp root; backup_dir() is a SIBLING of the workspace, so
    # give the workspace a parent dir of its own to keep the backups isolated per test.
    root = Path(tempfile.mkdtemp())
    c.workspace_dir = str(root / "workspace")
    c.workspace.mkdir(parents=True, exist_ok=True)
    return c


def _build_fake_workspace(ws: Path) -> None:
    """Populate a minimal but representative workspace: precious files + regenerable caches."""
    (ws / "goal.md").write_text("# mission\nrun the house\n", encoding="utf-8")
    (ws / "self_guide.md").write_text("# directives\ncall him Boss\n", encoding="utf-8")
    (ws / "persona.json").write_text(json.dumps({"name": "eiDOS", "level": 3, "xp": 250}), encoding="utf-8")
    (ws / "episodes.jsonl").write_text(
        '{"situation": "s1", "outcome": "ok"}\n{"situation": "s2", "outcome": "fix"}\n',
        encoding="utf-8")
    (ws / "observations.jsonl").write_text('{"tick": 1}\n\n{"tick": 2}\n', encoding="utf-8")

    # knowledge: precious index + REGENERABLE embedding cache
    kdir = ws / "knowledge"
    kdir.mkdir()
    (kdir / "index.json").write_text(json.dumps([{"id": "k1", "content_preview": "fact"}]), encoding="utf-8")
    (kdir / "vectors.npy").write_bytes(b"\x93NUMPY-fake-vector-bytes")
    (kdir / "vector_ids.json").write_text(json.dumps(["k1"]), encoding="utf-8")

    # skills: precious manifest
    sdir = ws / "skills"
    sdir.mkdir()
    (sdir / "_index.json").write_text(json.dumps({"greet__1": {"name": "greet"}}), encoding="utf-8")
    (sdir / "greet__1.py").write_text("def tool_greet(): return 'hi'\n", encoding="utf-8")
    (sdir / ".dryrun.py").write_text("# transient harness\n", encoding="utf-8")

    # lifecycle markers (must NOT be snapshotted)
    (ws / "eidos.pid").write_text("12345", encoding="utf-8")
    (ws / "eidos.should_run").write_text("1", encoding="utf-8")
    (ws / "eidos_spawn.ts").write_text("1700000000", encoding="utf-8")

    # __pycache__ + .pyc (bytecode; must NOT be snapshotted)
    pdir = ws / "home" / "__pycache__"
    pdir.mkdir(parents=True)
    (pdir / "status.cpython-311.pyc").write_bytes(b"\x00compiled")
    (ws / "home" / "status.py").write_text("print('home')\n", encoding="utf-8")


def _members(snap_path: Path) -> set:
    """Set of workspace-relative member paths inside a snapshot tarball."""
    with tarfile.open(snap_path, "r:gz") as tar:
        names = tar.getnames()
    rels = set()
    for n in names:
        parts = n.split("/", 1)
        if len(parts) == 2 and parts[1]:
            rels.add(parts[1])
    return rels


class TestSnapshotExclusions(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        _build_fake_workspace(self.cfg.workspace)

    def test_snapshot_includes_precious_excludes_caches(self):
        snap = backup.snapshot(self.cfg)
        self.assertTrue(snap.exists())
        rels = _members(snap)

        # precious files present
        for want in ("goal.md", "self_guide.md", "persona.json", "episodes.jsonl",
                     "knowledge/index.json", "skills/_index.json", "skills/greet__1.py"):
            self.assertIn(want, rels, f"expected {want} in snapshot")

        # regenerable caches / markers absent
        for gone in ("knowledge/vectors.npy", "knowledge/vector_ids.json",
                     "eidos.pid", "eidos.should_run", "eidos_spawn.ts",
                     "skills/.dryrun.py",
                     "home/__pycache__/status.cpython-311.pyc"):
            self.assertNotIn(gone, rels, f"did not expect {gone} in snapshot")

    def test_snapshot_written_into_sibling_backup_dir(self):
        snap = backup.snapshot(self.cfg)
        self.assertEqual(snap.parent, backup.backup_dir(self.cfg))
        # backup dir is a SIBLING of the workspace, not inside it
        self.assertNotIn(self.cfg.workspace.name, backup.backup_dir(self.cfg).parts[-1:])
        self.assertEqual(snap.parent.parent, self.cfg.workspace.parent)


class TestRestoreVerify(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        _build_fake_workspace(self.cfg.workspace)

    def test_good_snapshot_passes(self):
        snap = backup.snapshot(self.cfg)
        result = backup.verify(self.cfg, snap)
        self.assertTrue(result.ok, f"expected pass, got: {result}")
        self.assertEqual(result.reasons, [])
        # It actually performed the meaningful checks, not just extraction.
        joined = " ".join(result.checks)
        self.assertIn("tar extracted cleanly", joined)
        self.assertIn("persona.json parses as JSON", joined)
        self.assertIn("episodes.jsonl line-parses as JSONL", joined)

    def test_verify_default_picks_newest(self):
        backup.snapshot(self.cfg)
        result = backup.verify(self.cfg)  # no path → newest
        self.assertTrue(result.ok, str(result))

    def test_corrupted_json_fails_with_reason(self):
        snap = backup.snapshot(self.cfg)
        corrupted = self._corrupt_member(snap, "persona.json", b"{ this is not json")
        result = backup.verify(self.cfg, corrupted)
        self.assertFalse(result.ok)
        self.assertTrue(any("persona.json" in r and "JSON" in r for r in result.reasons),
                        f"expected a persona.json JSON-parse reason, got: {result.reasons}")

    def test_corrupted_jsonl_fails_with_reason(self):
        snap = backup.snapshot(self.cfg)
        corrupted = self._corrupt_member(
            snap, "episodes.jsonl", b'{"ok": 1}\nNOT-JSON-LINE\n')
        result = backup.verify(self.cfg, corrupted)
        self.assertFalse(result.ok)
        self.assertTrue(any("episodes.jsonl" in r for r in result.reasons),
                        f"expected an episodes.jsonl reason, got: {result.reasons}")

    def test_truncated_tarball_fails(self):
        snap = backup.snapshot(self.cfg)
        # Lop off the tail so the gzip/tar stream is incomplete.
        data = snap.read_bytes()
        broken = snap.with_name("broken.tar.gz")
        broken.write_bytes(data[: len(data) // 2])
        result = backup.verify(self.cfg, broken)
        self.assertFalse(result.ok)
        self.assertTrue(any("extract" in r.lower() for r in result.reasons),
                        f"expected an extract-failure reason, got: {result.reasons}")

    def test_missing_snapshot_fails(self):
        result = backup.verify(self.cfg, self.cfg.workspace.parent / "nope.tar.gz")
        self.assertFalse(result.ok)
        self.assertTrue(any("does not exist" in r for r in result.reasons))

    def _corrupt_member(self, snap: Path, rel: str, new_bytes: bytes) -> Path:
        """Rebuild `snap` into a new tarball with one member's bytes replaced. Returns new path."""
        out = snap.with_name("corrupt.tar.gz")
        with tarfile.open(snap, "r:gz") as src, tarfile.open(out, "w:gz") as dst:
            for m in src.getmembers():
                if m.name.endswith("/" + rel):
                    m2 = tarfile.TarInfo(name=m.name)
                    m2.size = len(new_bytes)
                    m2.mtime = m.mtime
                    import io
                    dst.addfile(m2, io.BytesIO(new_bytes))
                else:
                    f = src.extractfile(m)
                    dst.addfile(m, f)
        return out


class TestRotation(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.bdir = backup.backup_dir(self.cfg)
        self.bdir.mkdir(parents=True, exist_ok=True)

    def _touch(self, ts: str) -> Path:
        """Create a valid (tiny but real) snapshot tarball stamped at `ts` (YYYYMMDD-HHMMSS)."""
        p = self.bdir / f"{backup.SNAPSHOT_PREFIX}{ts}{backup.SNAPSHOT_SUFFIX}"
        with tarfile.open(p, "w:gz") as tar:
            info = tarfile.TarInfo(name="workspace/persona.json")
            import io
            payload = b"{}"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return p

    def test_parse_ts_roundtrip_and_reject(self):
        self.assertIsNotNone(backup._parse_ts("workspace-20260701-120000.tar.gz"))
        self.assertIsNotNone(backup._parse_ts("workspace-20260701-120000-2.tar.gz"))  # disambiguated
        self.assertIsNone(backup._parse_ts("random.tar.gz"))
        self.assertIsNone(backup._parse_ts("workspace-notadate.tar.gz"))

    def test_daily_and_weekly_retention(self):
        # 40 distinct consecutive days (~6 weeks), one snapshot each. With daily_keep=7 the daily
        # window only reaches back 7 days, so weekly retention MUST rescue older weeks' reps that
        # fall outside it — the grandfather-father-son property this rotation is for.
        self.cfg.pillars_backup_daily_keep = 7
        self.cfg.pillars_backup_weekly_keep = 8
        base_epoch = time.mktime(time.strptime("20260501-100000", backup.TS_FMT))
        made = []
        for d in range(40):
            ts = time.strftime(backup.TS_FMT, time.localtime(base_epoch + d * 86400))
            made.append((ts, self._touch(ts)))

        removed = backup.prune(self.cfg)
        survivors = {p.name for p in self.bdir.iterdir()}

        # The 7 newest days are all kept.
        newest_7 = {p.name for _, p in made[-7:]}
        self.assertTrue(newest_7.issubset(survivors), "the 7 newest daily snapshots must survive")

        # Rotation actually pruned older, non-representative days.
        self.assertTrue(removed, "expected some snapshots pruned")

        # Weekly retention rescues the newest snapshot of each distinct ISO-week (up to 8), and at
        # least one such rescue is OLDER than the 7-day daily window (proving weekly earns its keep).
        by_week = {}
        for ts, p in made:  # made is chronological (oldest→newest)
            wk = time.strftime("%G-W%V", time.strptime(ts, backup.TS_FMT))
            by_week[wk] = p  # last write per week = newest of that week
        week_reps = {p.name for p in by_week.values()}
        self.assertTrue(week_reps.issubset(survivors),
                        "every week's newest snapshot must survive weekly retention")
        older_than_daily = week_reps - newest_7
        self.assertTrue(older_than_daily,
                        "expected weekly reps beyond the daily window")
        self.assertTrue(older_than_daily.issubset(survivors),
                        "weekly retention must keep week-reps that the daily window does not cover")

    def test_newest_always_kept(self):
        self.cfg.pillars_backup_daily_keep = 0
        self.cfg.pillars_backup_weekly_keep = 0
        p = self._touch("20260601-100000")
        backup.prune(self.cfg)
        self.assertTrue(p.exists(), "the single newest snapshot is never pruned")

    def test_one_per_day_dedupes_to_newest(self):
        self.cfg.pillars_backup_daily_keep = 14
        self.cfg.pillars_backup_weekly_keep = 8
        early = self._touch("20260601-080000")
        late = self._touch("20260601-200000")
        backup.prune(self.cfg)
        # Same calendar day → daily bucket keeps only the newest; weekly keeps newest of the week too.
        self.assertTrue(late.exists())
        self.assertFalse(early.exists(), "older same-day snapshot should be pruned")


if __name__ == "__main__":
    unittest.main()
