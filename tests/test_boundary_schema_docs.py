"""Generated boundary schema artifact stays in sync with Pydantic models."""

import subprocess
import sys
import unittest
from pathlib import Path


class TestBoundarySchemaDocs(unittest.TestCase):
    def test_boundary_schema_docs_are_current(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "scripts/check_boundary_schemas.py"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
