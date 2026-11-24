"""Run: python3 -m unittest discover -s tests -v"""
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import vigil_guard as g  # noqa: E402

P = "mcp__plugin_vigil-defender_vigil__"


class Decide(unittest.TestCase):
    def test_reads_are_silent(self):
        for t in g.READ_ONLY:
            self.assertIsNone(g.decide({"tool_name": P + t}), t)

    def test_known_writes_ask(self):
        for t in ("create_case", "update_case", "close_case", "start_run",
                  "send_directive", "approve_action", "reject_action",
                  "create_approval_action", "add_case_ioc"):
            out = g.decide({"tool_name": P + t})
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask", t)

    def test_unknown_tool_asks(self):
        out = g.decide({"tool_name": P + "brand_new_tool"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_official_plugin_prefix(self):
        self.assertIsNone(g.decide({"tool_name": "mcp__plugin_vigil-soc_vigil__get_case"}))
        self.assertIsNotNone(g.decide({"tool_name": "mcp__plugin_vigil-soc_vigil__update_case"}))


class Cli(unittest.TestCase):
    def run_guard(self, stdin):
        return subprocess.run([sys.executable, str(ROOT / "scripts/vigil_guard.py")],
                              input=stdin, capture_output=True, text=True)

    def test_garbage_fails_closed(self):
        r = self.run_guard("not json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_read_has_empty_output(self):
        r = self.run_guard(json.dumps({"tool_name": P + "list_findings"}))
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class Manifest(unittest.TestCase):
    def test_json_files_parse(self):
        for f in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "hooks/hooks.json"):
            json.loads((ROOT / f).read_text())

    def test_no_hardcoded_secrets(self):
        m = (ROOT / ".claude-plugin/plugin.json").read_text()
        self.assertIn("${user_config.credential}", m)
        self.assertTrue(json.loads(m)["userConfig"]["credential"]["sensitive"])

    def test_agent_tools_are_read_only(self):
        text = (ROOT / "agents/investigator.md").read_text()
        line = next(l for l in text.splitlines() if l.startswith("tools:"))
        for tool in line[len("tools:"):].split(","):
            short = tool.strip().rsplit("__", 1)[-1]
            self.assertIn(short, g.READ_ONLY, short)


if __name__ == "__main__":
    unittest.main()
