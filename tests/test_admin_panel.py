"""Administrator approval panel (Pillars 5.2 dashboard surface) — offline endpoint tests.

Exercises the dashboard's Administrator seams in-process:
  - build_admin: pending proposals + per-tier autonomy books; flag off → empty/disabled;
  - typed POST payload boundaries for /api/admin/{approve,reject,revoke};
  - the real HTTP handler (dashboard._make_handler) served on an ephemeral localhost port:
    GET list, POST approve (with and without edit), POST reject, POST revoke, token gating.

No services / live ports / GPU — temp workspaces, ephemeral 127.0.0.1 sockets only.
"""

import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import administrator
from administrator import AdminState
import dashboard
import quests
from typed_boundary import DashboardPayloadError, validate_dashboard_post_payload


class _Config:
    """Minimal Config stand-in: the paths + flags the admin panel endpoints read."""
    def __init__(self, root: Path, *, admin_on: bool = True, token: str = ""):
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.pillars_administrator_enabled = admin_on
        self.dashboard_token = token

    @property
    def state_dir(self) -> Path:
        return self.workspace / "state"

    @property
    def knowledge_dir(self) -> Path:
        return self.workspace / "knowledge"


def _seed_pending(config, pid: str = "adm_t2_first_trusted", tier: int = 2) -> None:
    """Plant one pending proposal record in the Administrator's books (the check_in shape)."""
    state = AdminState(config)
    state.proposals[pid] = {
        "id": pid,
        "directive": "Earn trust at the new tier. Author nothing; harden what exists.",
        "tier": tier, "reward_xp": 40, "expiry_hours": 0,
        "criteria": {"path": "skills.tiers.2.trusted", "op": ">=", "value": 1},
        "status": "pending", "created_ts": "2026-07-04T00:00:00Z", "resolved_ts": None,
        "narrator": "The next door does not open for the unproven.", "event": "sleep_complete",
    }
    state.save()


# ============================================================================================
# build_admin — the GET payload builder
# ============================================================================================
class TestBuildAdmin(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _Config(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_flag_off_disabled_and_empty(self):
        cfg = _Config(self.tmp / "off", admin_on=False)
        _seed_pending(cfg)  # even with books on disk, the flag gates the panel
        d = dashboard.build_admin(cfg)
        self.assertFalse(d["enabled"])
        self.assertEqual(d["proposals"], [])
        self.assertEqual(d["autonomy"], [])

    def test_empty_books(self):
        d = dashboard.build_admin(self.config)
        self.assertTrue(d["enabled"])
        self.assertEqual(d["proposals"], [])
        self.assertEqual(d["autonomy"], [])

    def test_pending_proposal_and_tier_row(self):
        _seed_pending(self.config)
        d = dashboard.build_admin(self.config)
        self.assertEqual(len(d["proposals"]), 1)
        p = d["proposals"][0]
        self.assertEqual(p["id"], "adm_t2_first_trusted")
        self.assertEqual(p["tier"], 2)
        self.assertEqual(p["criteria"]["op"], ">=")
        # The proposal's tier appears in the autonomy table even with no decisions yet.
        self.assertEqual([a["tier"] for a in d["autonomy"]], [2])
        row = d["autonomy"][0]
        self.assertEqual(row["decisions"], 0)
        self.assertIsNone(row["approval_rate"])
        self.assertFalse(row["auto_issue"])
        self.assertFalse(row["revoked"])

    def test_autonomy_books_rendered(self):
        state = AdminState(self.config)
        state.autonomy["3"] = {"decisions": [1, 1, 1, 1, 1, 0], "revoked": False}
        state.autonomy["4"] = {"decisions": [], "revoked": True}
        state.save()
        d = dashboard.build_admin(self.config)
        by_tier = {a["tier"]: a for a in d["autonomy"]}
        self.assertEqual(by_tier[3]["approvals"], 5)
        self.assertEqual(by_tier[3]["decisions"], 6)
        self.assertAlmostEqual(by_tier[3]["approval_rate"], round(5 / 6, 3))
        self.assertTrue(by_tier[3]["auto_issue"])   # 5/6 ≥ 0.8 over ≥ min sample
        self.assertTrue(by_tier[4]["revoked"])
        self.assertFalse(by_tier[4]["auto_issue"])


# ============================================================================================
# Typed POST payload boundaries
# ============================================================================================
class TestAdminPostPayloads(unittest.TestCase):
    def test_approve_requires_id(self):
        with self.assertRaises(DashboardPayloadError):
            validate_dashboard_post_payload("/api/admin/approve", b"{}")

    def test_approve_plain(self):
        p = validate_dashboard_post_payload("/api/admin/approve", b'{"id":"p1"}')
        self.assertEqual(p.id, "p1")
        self.assertIsNone(p.edit)

    def test_approve_edit_allows_editable_fields_only(self):
        p = validate_dashboard_post_payload(
            "/api/admin/approve", b'{"id":"p1","edit":{"reward_xp":10,"directive":"d"}}')
        self.assertEqual(p.edit, {"reward_xp": 10, "directive": "d"})
        with self.assertRaises(DashboardPayloadError) as cm:
            validate_dashboard_post_payload(
                "/api/admin/approve", b'{"id":"p1","edit":{"status":"approved"}}')
        self.assertIn("status", str(cm.exception))

    def test_reject_reason_defaults_empty(self):
        p = validate_dashboard_post_payload("/api/admin/reject", b'{"id":"p1"}')
        self.assertEqual(p.reason, "")

    def test_revoke_requires_positive_int_tier(self):
        p = validate_dashboard_post_payload("/api/admin/revoke", b'{"tier":2}')
        self.assertEqual(p.tier, 2)
        for raw in (b"{}", b'{"tier":0}', b'{"tier":"2x"}'):
            with self.subTest(raw=raw):
                with self.assertRaises(DashboardPayloadError):
                    validate_dashboard_post_payload("/api/admin/revoke", raw)


# ============================================================================================
# The real HTTP handler, in-process on an ephemeral localhost port
# ============================================================================================
class _ServerMixin:
    token = ""
    admin_on = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _Config(self.tmp, admin_on=self.admin_on, token=self.token)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._make_handler(self.config))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            hdrs = {"Content-Type": "application/json"}
            hdrs.update(headers or {})
            conn.request(method, path, json.dumps(body) if body is not None else None, hdrs)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()


class TestAdminEndpoints(_ServerMixin, unittest.TestCase):
    def test_list_endpoint(self):
        _seed_pending(self.config)
        status, d = self._request("GET", "/api/admin/list")
        self.assertEqual(status, 200)
        self.assertTrue(d["enabled"])
        self.assertEqual(len(d["proposals"]), 1)
        self.assertEqual(d["proposals"][0]["id"], "adm_t2_first_trusted")

    def test_approve_issues_quest_and_credits_tier(self):
        _seed_pending(self.config)
        status, d = self._request("POST", "/api/admin/approve", {"id": "adm_t2_first_trusted"})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["quest_id"], "adm_t2_first_trusted")
        # The quest crossed the wall into the System's queue…
        queued = quests.System(self.config).store.queue()
        self.assertEqual([q.id for q in queued], ["adm_t2_first_trusted"])
        # …and the decision credited the proposing tier's autonomy record.
        state = AdminState(self.config)
        self.assertEqual(state.autonomy["2"]["decisions"], [1])
        self.assertEqual(state.proposals["adm_t2_first_trusted"]["status"], "approved")

    def test_approve_with_edit_overrides_quest_window_fields(self):
        _seed_pending(self.config)
        status, d = self._request("POST", "/api/admin/approve", {
            "id": "adm_t2_first_trusted",
            "edit": {"directive": "Edited directive.", "reward_xp": 15},
        })
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])
        q = quests.System(self.config).store.queue()[0]
        self.assertEqual(q.directive, "Edited directive.")
        self.assertEqual(q.reward["amount"], 15)

    def test_approve_unknown_id_fails_closed(self):
        status, d = self._request("POST", "/api/admin/approve", {"id": "nope"})
        self.assertEqual(status, 200)
        self.assertFalse(d["ok"])
        self.assertEqual(quests.System(self.config).store.queue(), [])

    def test_approve_bad_edit_field_rejected_400(self):
        _seed_pending(self.config)
        status, d = self._request("POST", "/api/admin/approve",
                                  {"id": "adm_t2_first_trusted", "edit": {"status": "approved"}})
        self.assertEqual(status, 400)
        self.assertFalse(d["ok"])
        # Nothing crossed the wall; the proposal is still pending.
        self.assertEqual(AdminState(self.config).proposals["adm_t2_first_trusted"]["status"], "pending")

    def test_reject_debits_tier_and_keeps_the_wall_shut(self):
        _seed_pending(self.config)
        status, d = self._request("POST", "/api/admin/reject",
                                  {"id": "adm_t2_first_trusted", "reason": "too vague"})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(quests.System(self.config).store.queue(), [])
        state = AdminState(self.config)
        self.assertEqual(state.proposals["adm_t2_first_trusted"]["status"], "rejected")
        self.assertEqual(state.proposals["adm_t2_first_trusted"]["reject_reason"], "too vague")
        self.assertEqual(state.autonomy["2"]["decisions"], [0])

    def test_revoke_clears_the_books(self):
        state = AdminState(self.config)
        state.autonomy["3"] = {"decisions": [1, 1, 1, 1, 1, 1], "revoked": False}
        state.save()
        self.assertTrue(administrator.tier_has_autonomy(self.config, 3))
        status, d = self._request("POST", "/api/admin/revoke", {"tier": 3})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])
        self.assertFalse(administrator.tier_has_autonomy(self.config, 3))
        self.assertEqual(AdminState(self.config).autonomy["3"], {"decisions": [], "revoked": True})

    def test_revoke_bad_tier_400(self):
        status, d = self._request("POST", "/api/admin/revoke", {"tier": 0})
        self.assertEqual(status, 400)
        self.assertFalse(d["ok"])


class TestAdminEndpointsFlagOff(_ServerMixin, unittest.TestCase):
    admin_on = False

    def test_flag_off_list_disabled(self):
        status, d = self._request("GET", "/api/admin/list")
        self.assertEqual(status, 200)
        self.assertFalse(d["enabled"])

    def test_flag_off_posts_noop(self):
        _seed_pending(self.config)
        for path, body in (("/api/admin/approve", {"id": "adm_t2_first_trusted"}),
                           ("/api/admin/reject", {"id": "adm_t2_first_trusted"}),
                           ("/api/admin/revoke", {"tier": 2})):
            with self.subTest(path=path):
                status, d = self._request("POST", path, body)
                self.assertEqual(status, 200)
                self.assertFalse(d["ok"])
        # Nothing changed: still pending, nothing crossed, no decisions recorded.
        state = AdminState(self.config)
        self.assertEqual(state.proposals["adm_t2_first_trusted"]["status"], "pending")
        self.assertEqual(state.autonomy.get("2", {}).get("decisions", []), [])
        self.assertEqual(quests.System(self.config).store.queue(), [])


class TestAdminTokenGate(_ServerMixin, unittest.TestCase):
    token = "sekrit"

    def test_posts_401_without_token(self):
        _seed_pending(self.config)
        for path, body in (("/api/admin/approve", {"id": "adm_t2_first_trusted"}),
                           ("/api/admin/reject", {"id": "adm_t2_first_trusted"}),
                           ("/api/admin/revoke", {"tier": 2})):
            with self.subTest(path=path):
                status, d = self._request("POST", path, body)
                self.assertEqual(status, 401)
        self.assertEqual(AdminState(self.config).proposals["adm_t2_first_trusted"]["status"], "pending")

    def test_post_ok_with_token_header(self):
        _seed_pending(self.config)
        status, d = self._request("POST", "/api/admin/reject", {"id": "adm_t2_first_trusted"},
                                  headers={"X-EiDOS-Token": "sekrit"})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])

    def test_get_list_is_read_only_ungated(self):
        status, d = self._request("GET", "/api/admin/list")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
