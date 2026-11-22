"""Adapters only need a RunContext: no chat, no server, no globals."""
import json
import os
import subprocess
import tempfile
import unittest

import tests.helpers  # noqa: F401  (puts the fake CLIs on PATH)
from agent_groupchat.adapters import ADAPTERS, RunContext
from agent_groupchat.roles import LEVELS


def make_ctx(folder, session=None):
    seen = {"sessions": [], "steps": [], "preview": ""}

    def preview(text, append=False):
        seen["preview"] = seen["preview"] + text if append else text

    def stream(cmd, on_event, stdin=None):
        p = subprocess.run(cmd, cwd=folder, capture_output=True, text=True, input=stdin)
        for line in p.stdout.splitlines():
            try:
                on_event(json.loads(line))
            except ValueError:
                on_event({"type": "_text", "text": line})
        return p.returncode, p.stderr[-800:].strip(), None

    ctx = RunContext(folder=folder, session=session, system_prompt="INTRO",
                     save_session=seen["sessions"].append, step=seen["steps"].append,
                     preview=preview, stream=stream, model="fake-model",
                     state_dir=os.path.join(folder, ".state"))
    return ctx, seen


class StandaloneAdapters(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        os.environ["FAKE_RULES"] = "[]"
        os.environ.pop("FAKE_LOG", None)

    def test_every_adapter_answers_and_reports_its_session(self):
        for bot, adapter in ADAPTERS.items():
            with self.subTest(bot=bot):
                ctx, seen = make_ctx(self.folder)
                text, meta = adapter.run(ctx, "hello", "talk")
                self.assertEqual(text, f"fake {bot} reply")
                self.assertEqual(len(seen["sessions"]), 1)
                self.assertTrue(seen["steps"])
                self.assertEqual(seen["preview"], f"fake {bot} reply")
                self.assertIn("tokens", meta)

    def test_every_adapter_reports_failures_as_error_text(self):
        os.environ["FAKE_RULES"] = json.dumps([{"exit": 1}])
        for bot, adapter in ADAPTERS.items():
            with self.subTest(bot=bot):
                text, _ = adapter.run(make_ctx(self.folder)[0], "hello", "talk")
                self.assertTrue(text.startswith("(error)"), text)

    def test_adapters_declare_their_basics(self):
        for bot, adapter in ADAPTERS.items():
            with self.subTest(bot=bot):
                self.assertEqual(adapter.id, bot)
                self.assertTrue(adapter.name and adapter.kind)
                self.assertRegex(adapter.color, r"^#[0-9a-f]{6}$")
                self.assertIn("TALK ONLY", adapter.mode_text("talk"))
                self.assertIn("abc", adapter.resume_hint("abc"))
                self.assertIn(adapter.skill_syntax, ("slash", "dollar", "none"))
                self.assertTrue(adapter.levels and set(adapter.levels) <= set(LEVELS))


if __name__ == "__main__":
    unittest.main()
