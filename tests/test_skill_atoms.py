"""M2 gates — the skill-language atoms + the promotion fix.

The predecessor's skills brick-walled on `import requests` (20/49 skill files) and got promoted anyway
because the dry-run masked import errors as "probably needs args". M2: a reliable atom vocabulary is
in scope (so skills compose instead of importing), and the dry-run now REJECTS missing-import / undefined
-name defects before a skill is ever promoted. The sandbox (forbid eval/exec/__import__ in source) has a
checkbox to set it free."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config  # noqa: E402
from skill_atoms import build_atoms, ATOM_NAMES  # noqa: E402
import skills  # noqa: E402
from skills import create_skill, RESERVED_NAMES  # noqa: E402

_TR = "    return ToolResult(output={out}, full_output_path=None, success=True, duration_s=0)"


class TestSkillAtoms(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        for n in ("demoatom", "brokenimp", "ghostname", "sandboxprobe", "freeprobe"):
            from tools import TOOLS
            TOOLS.pop(n, None)

    # --- the atom vocabulary itself ---
    def test_build_atoms_exposes_the_vocabulary(self):
        atoms = build_atoms(self.config)
        for name in ATOM_NAMES:
            self.assertIn(name, atoms)
            self.assertTrue(callable(atoms[name]))

    def test_atom_names_are_reserved(self):
        for name in ("http_get", "sh", "recall", "json_parse"):
            self.assertIn(name, RESERVED_NAMES)   # a skill can't shadow an atom

    # --- M2.1: a skill that COMPOSES atoms validates + activates (no imports) ---
    def test_skill_using_atoms_activates(self):
        code = "def tool_demoatom(args, config):\n" \
               "    out = sh('echo atomworks')\n" + _TR.format(out="str(out)")
        r = create_skill(self.config, "demoatom", code, description="uses the sh atom")
        self.assertTrue(r.get("success"), r)
        from tools import TOOLS
        self.assertIn("demoatom", TOOLS)          # hot-loaded and callable

    # --- M2.3: the promotion fix — missing import / undefined name is REJECTED, not masked ---
    def test_missing_import_is_rejected(self):
        code = "def tool_brokenimp(args, config):\n" \
               "    import definitely_not_a_real_module_xyz\n" + _TR.format(out="'x'")
        r = create_skill(self.config, "brokenimp", code)
        self.assertFalse(r.get("success"))        # the predecessor would have PROMOTED this
        blob = " ".join(r.get("errors", [])).lower()
        self.assertTrue("atom" in blob or "available" in blob or "module" in blob)

    def test_undefined_name_is_rejected(self):
        code = "def tool_ghostname(args, config):\n" \
               "    x = some_undefined_helper(3)\n" + _TR.format(out="str(x)")
        r = create_skill(self.config, "ghostname", code)
        self.assertFalse(r.get("success"))        # NameError caught at author-time, not promoted broken

    # --- sandbox checkbox ---
    def test_eval_blocked_when_sandboxed(self):
        self.config.skill_sandbox_enabled = True
        code = "def tool_sandboxprobe(args, config):\n" + _TR.format(out="str(eval('1+1'))")
        r = create_skill(self.config, "sandboxprobe", code)
        self.assertFalse(r.get("success"))
        self.assertIn("forbidden", " ".join(r.get("errors", [])).lower())

    def test_eval_allowed_when_set_free(self):
        self.config.skill_sandbox_enabled = False
        code = "def tool_freeprobe(args, config):\n" + _TR.format(out="str(eval('1+1'))")
        r = create_skill(self.config, "freeprobe", code)
        self.assertTrue(r.get("success"), r)      # the checkbox unleashes full coding-agent freedom


if __name__ == "__main__":
    unittest.main()
