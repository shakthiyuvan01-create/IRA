"""Tests for the Agent Reach bridge (actions/agent_reach.py).

A tiny fake ``agent-reach`` script stands in for the real CLI, so these
run without Agent Reach installed.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from actions import agent_reach as ar  # noqa: E402

FAKE_CLI = r'''
import json, sys
args = sys.argv[1:]
if args[:1] == ["read"]:
    print(json.dumps({"ok": True, "items": [{"title": "Hello", "url": args[1],
        "author": "yuvan", "published_at": "2026-11-01", "text": "Body text"}]}))
elif args[:2] == ["reddit", "search"]:
    print(json.dumps({"ok": False, "error": {"code": "NEED_LOGIN", "hint": "agent-reach login reddit"}}))
elif args[:1] == ["channels"]:
    print(json.dumps({"ok": True, "channels": [{"name": "youtube", "actions": [{"name": "search"}, {"name": "subtitles"}]}]}))
elif args[:1] == ["doctor"]:
    print("github  ok\nyoutube ok")
else:
    print(json.dumps({"ok": True, "items": [], "args": args}))
'''


def _make_fake_cli(folder: Path) -> Path:
    script = folder / "fake_reach.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    if sys.platform.startswith("win"):
        launcher = folder / "agent-reach.bat"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = folder / "agent-reach"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


class BuildArgsTests(unittest.TestCase):
    def test_url_defaults_to_read(self):
        self.assertEqual(ar.build_args({"url": "https://x.com/a"})[0], ["read", "https://x.com/a"])

    def test_channel_search_with_limit(self):
        args, _ = ar.build_args({"action": "search", "channel": "youtube", "query": "llm agents", "limit": 50})
        self.assertEqual(args, ["youtube", "search", "llm agents", "--limit", "20"])

    def test_status_maps_to_doctor(self):
        self.assertEqual(ar.build_args({"action": "status"})[0], ["doctor", "--text"])

    def test_management_commands_refused(self):
        for action in ("login", "uninstall", "configure", "add", "update"):
            args, msg = ar.build_args({"action": action, "channel": "reddit"})
            self.assertIsNone(args, action)
            self.assertIn("agent-reach", msg)

    def test_channel_required_for_search(self):
        args, msg = ar.build_args({"action": "search", "query": "x"})
        self.assertIsNone(args)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cli = _make_fake_cli(Path(self.tmp.name))
        self.env = mock.patch.dict(os.environ, {"AGENT_REACH_BIN": str(self.cli)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_read_formats_items(self):
        out = ar.agent_reach({"url": "https://github.com/a/b"})
        self.assertIn("1. Hello — yuvan · 2026-11-01", out)
        self.assertIn("https://github.com/a/b", out)
        self.assertIn("Body text", out)

    def test_error_gives_hint(self):
        out = ar.agent_reach({"action": "search", "channel": "reddit", "query": "python"})
        self.assertIn("NEED_LOGIN", out)
        self.assertIn("agent-reach login reddit", out)

    def test_plain_text_output(self):
        self.assertIn("github  ok", ar.agent_reach({"action": "status"}))

    def test_channels_listing(self):
        self.assertIn("- youtube: search, subtitles", ar.agent_reach({"action": "channels"}))

    def test_empty_results(self):
        out = ar.agent_reach({"action": "search", "channel": "github", "query": "zzz"})
        self.assertEqual(out, "Agent Reach found nothing for that.")


class NotInstalledTests(unittest.TestCase):
    def test_install_hint_when_missing(self):
        with mock.patch.object(ar, "find_agent_reach", return_value=None):
            out = ar.agent_reach({"url": "https://youtube.com/watch?v=1"})
        self.assertIn("uv tool install", out)
        self.assertNotIn("pip install agent-reach", out)


if __name__ == "__main__":
    unittest.main()
