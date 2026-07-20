"""Typed dashboard POST payload boundaries."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import dashboard
from typed_boundary import DashboardPayloadError, validate_dashboard_post_payload


class TestOriginFence(unittest.TestCase):
    """_origin_ok: the drive-by CSRF fence on state-changing POSTs. A cross-origin browser
    POST (any web page open on a LAN browser) must be refused; the dashboard's own
    same-origin fetches and headerless scripts/curl must pass."""

    def test_no_origin_header_passes(self):
        self.assertTrue(dashboard._origin_ok({}))  # curl / scripts send no Origin

    def test_same_origin_passes(self):
        self.assertTrue(dashboard._origin_ok(
            {"Origin": "http://sprinter:8099", "Host": "sprinter:8099"}))

    def test_same_origin_case_insensitive(self):
        self.assertTrue(dashboard._origin_ok(
            {"Origin": "http://Sprinter:8099", "Host": "sprinter:8099"}))

    def test_cross_origin_refused(self):
        self.assertFalse(dashboard._origin_ok(
            {"Origin": "http://evil.example", "Host": "sprinter:8099"}))

    def test_null_origin_refused(self):
        self.assertFalse(dashboard._origin_ok(
            {"Origin": "null", "Host": "sprinter:8099"}))

    def test_garbage_origin_refused(self):
        self.assertFalse(dashboard._origin_ok(
            {"Origin": "not a url", "Host": "sprinter:8099"}))


class TestDashboardPostPayloads(unittest.TestCase):
    def test_chat_payload_strips_and_keeps_valid_message(self):
        payload = validate_dashboard_post_payload("/api/chat", b'{"message":"  hello  "}')
        self.assertEqual(payload.message, "hello")

    def test_chat_payload_rejects_extra_field(self):
        with self.assertRaises(DashboardPayloadError) as cm:
            validate_dashboard_post_payload("/api/chat", b'{"message":"hi","surprise":true}')
        self.assertIn("surprise", str(cm.exception))

    def test_chat_payload_rejects_empty_message(self):
        with self.assertRaises(DashboardPayloadError):
            validate_dashboard_post_payload("/api/chat", b'{"message":"   "}')

    def test_reset_payload_allows_empty_default(self):
        payload = validate_dashboard_post_payload("/api/control/reset", b"")
        self.assertEqual(payload.mode, "rebirth")

    def test_reset_payload_rejects_unknown_mode(self):
        with self.assertRaises(DashboardPayloadError) as cm:
            validate_dashboard_post_payload("/api/control/reset", b'{"mode":"everything"}')
        self.assertIn("mode", str(cm.exception))

    def test_git_restore_payload_allows_last_good_default(self):
        for raw in (b"", b"{}", b'{"tag":""}'):
            with self.subTest(raw=raw):
                payload = validate_dashboard_post_payload("/api/git/restore", raw)
                self.assertEqual(payload.tag, "")

    def test_config_payload_requires_settings_object(self):
        payload = validate_dashboard_post_payload(
            "/api/config",
            b'{"settings":{"dashboard.port":8101},"apply":false}',
        )
        self.assertEqual(payload.settings, {"dashboard.port": 8101})
        self.assertFalse(payload.apply)
        with self.assertRaises(DashboardPayloadError):
            validate_dashboard_post_payload("/api/config", b'{"apply":false}')

    def test_privileged_payloads_require_ids(self):
        for path in ("/api/selfedit/apply", "/api/selfedit/reject"):
            with self.subTest(path=path):
                with self.assertRaises(DashboardPayloadError):
                    validate_dashboard_post_payload(path, b"{}")

    def test_invalid_json_fails_closed(self):
        with self.assertRaises(DashboardPayloadError) as cm:
            validate_dashboard_post_payload("/api/chat_hold", b"{bad json")
        self.assertEqual(str(cm.exception), "invalid json")


if __name__ == "__main__":
    unittest.main()
