"""Settings menu plumbing: the curated schema (settings_schema) and the config.local.toml overlay
(config.save_overrides + load precedence). Pins: the UI payload maps onto config.toml's shape, secrets
aren't echoed/overwritten, bad types are rejected, and the overlay overrides the base without touching it.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
import settings_schema


class TestSchema(unittest.TestCase):
    def test_current_settings_grouped_with_values(self):
        c = config.Config()
        groups = settings_schema.current_settings(c)
        self.assertTrue(any(g["group"].startswith("Model") for g in groups))
        llm = next(g for g in groups if g["group"].startswith("Model"))["fields"]
        url = next(f for f in llm if f["id"] == "llm.url")
        self.assertEqual(url["value"], c.llm_url)

    def test_secret_not_echoed(self):
        c = config.Config()
        c.dashboard_token = "supersecret"
        groups = settings_schema.current_settings(c)
        tok = next(f for g in groups for f in g["fields"] if f["id"] == "dashboard.token")
        self.assertEqual(tok["value"], "********")

    def test_build_overrides_maps_to_toml_shape(self):
        ov, err = settings_schema.build_overrides({
            "llm.url": "http://x:1/v1", "llm.temperature": "0.7",
            "creature_mode": True, "nervous.power_enabled": "on"})
        self.assertEqual(err, [])
        self.assertEqual(ov["llm"], {"url": "http://x:1/v1", "temperature": 0.7})
        self.assertEqual(ov["creature_mode"], True)            # top-level key, not under a section
        self.assertEqual(ov["nervous"], {"power_enabled": True})

    def test_masked_secret_is_skipped(self):
        ov, err = settings_schema.build_overrides({"dashboard.token": "********"})
        self.assertEqual(ov, {})                               # unchanged mask must not overwrite the token
        self.assertEqual(err, [])

    def test_bad_type_reported_not_raised(self):
        ov, err = settings_schema.build_overrides({"llm.max_tokens": "twelve"})
        self.assertEqual(ov, {})
        self.assertTrue(err and "max_tokens" in err[0])

    def test_unknown_id_ignored(self):
        ov, err = settings_schema.build_overrides({"not.a.real.field": 1})
        self.assertEqual((ov, err), ({}, []))


class TestOverlay(unittest.TestCase):
    def _base(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "config.toml"
        p.write_text('[llm]\nurl = "http://base:1"\nmodel = "base-model"\n', encoding="utf-8")
        return p

    def test_overlay_overrides_base_without_touching_it(self):
        base = self._base()
        before = base.read_text(encoding="utf-8")
        config.save_overrides({"llm": {"url": "http://override:9"}}, path=str(base))
        c = config.load_config(str(base))
        self.assertEqual(c.llm_url, "http://override:9")       # overlay wins
        self.assertEqual(c.llm_model, "base-model")            # base value preserved
        self.assertEqual(base.read_text(encoding="utf-8"), before)  # base file untouched
        self.assertTrue(base.with_name("config.local.toml").exists())

    def test_overlay_alone_no_base(self):
        d = tempfile.mkdtemp()
        base = Path(d) / "config.toml"            # never created
        config.save_overrides({"llm": {"model": "solo"}}, path=str(base))
        c = config.load_config(str(base))
        self.assertEqual(c.llm_model, "solo")     # loads from overlay even with no base config.toml

    def test_save_merges_successive_writes(self):
        base = self._base()
        config.save_overrides({"llm": {"temperature": 0.9}}, path=str(base))
        config.save_overrides({"llm": {"top_k": 40}}, path=str(base))
        c = config.load_config(str(base))
        self.assertEqual(c.llm_temperature, 0.9)  # first write survives the second
        self.assertEqual(c.llm_top_k, 40)


if __name__ == "__main__":
    unittest.main()
