"""CLI helpers: org search, Claude config install (merge + backup)."""
import json
import tempfile
import unittest
from pathlib import Path

from epic_patient_fhir.cli import SERVER_NAME, install_claude
from epic_patient_fhir.orgs import find_orgs, resolve_org


class OrgSearch(unittest.TestCase):
    def test_bundled_list_loaded(self):
        self.assertGreater(len(find_orgs(None, limit=1000)), 100)

    def test_resolve(self):
        self.assertEqual(resolve_org("adventhealth").name, "AdventHealth")
        self.assertEqual(resolve_org("AdventHealth").key, "adventhealth")
        self.assertTrue(resolve_org("sandbox").sandbox)


class InstallClaude(unittest.TestCase):
    def test_creates_new_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "Claude" / "claude_desktop_config.json"
            _, backup = install_claude(cfg)
            self.assertIsNone(backup)
            data = json.loads(cfg.read_text())
            self.assertEqual(data["mcpServers"][SERVER_NAME]["args"], ["-m", "epic_patient_fhir.mcp_server"])

    def test_merges_and_backs_up(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "claude_desktop_config.json"
            cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
            _, backup = install_claude(cfg)
            data = json.loads(cfg.read_text())
            self.assertIn("other", data["mcpServers"])
            self.assertIn(SERVER_NAME, data["mcpServers"])
            self.assertEqual(data["theme"], "dark")
            self.assertEqual(json.loads(backup.read_text())["mcpServers"], {"other": {"command": "x"}})

    def test_refuses_broken_json(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "claude_desktop_config.json"
            cfg.write_text("{not json")
            with self.assertRaises(SystemExit):
                install_claude(cfg)
            self.assertEqual(cfg.read_text(), "{not json")


if __name__ == "__main__":
    unittest.main()
