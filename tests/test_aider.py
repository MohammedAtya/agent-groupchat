"""The Aider adapter, against a fake `aider` (tests/bin/aider)."""
import os
import subprocess
import unittest

from tests.helpers import ChatTest, srv
from agent_groupchat.adapters.aider import SAFE_FLAGS, new_reply


def aider(level=None, **kw):
    return {"adapter": "aider", "model": "sonnet", **({"level": level} if level else {}), **kw}


class AiderAgent(ChatTest):
    def argv(self, i=-1):
        return self.calls("aider")[i]["argv"]

    def test_ask_mode_and_safety_flags(self):
        c = self.chat(agents=[aider("read"), "claude"])
        self.send(c, "@aider hi")
        argv = self.argv()
        self.assertEqual(argv[argv.index("--chat-mode") + 1], "ask")
        for flag in SAFE_FLAGS:
            self.assertIn(flag, argv)
        self.assertIn("--yes-always", argv)
        self.assertIn("--no-auto-commits", argv)
        self.assertIn("--no-suggest-shell-commands", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertIn("--no-git", argv)  # the test folder isn't a git repo
        hist = argv[argv.index("--chat-history-file") + 1]
        self.assertTrue(hist.startswith(os.path.join(srv.DATA_DIR, "sessions")))  # not the project
        self.assertEqual(os.listdir(self.folder), [])  # Aider wrote nothing into the project

    def test_reply_comes_from_the_history_file(self):
        self.rules({"style": "aider", "reply": "Use a **dict** here."})
        c = self.chat(agents=[aider()])
        self.send(c, "hi")
        (r,) = self.replies(c)
        self.assertEqual(r["text"], "Use a **dict** here.")

    def test_first_message_has_intro_then_history_is_restored(self):
        c = self.chat(agents=[aider(), "claude"])
        self.send(c, "@aider one")
        self.assertNotIn("--restore-chat-history", self.argv())
        hist = os.path.join(srv.DATA_DIR, "sessions", f"aider-{c['sessions']['aider']}.chat.md")
        with open(hist) as f:
            self.assertIn("#### You are Aider", f.read())  # intro in the first message
        self.send(c, "@aider two")
        self.assertIn("--restore-chat-history", self.argv())
        (_, second) = self.replies(c)
        self.assertEqual(second["text"], "fake aider reply")  # only this turn's reply

    def test_participant_gets_code_mode(self):
        c = self.chat(agents=[aider()])  # Participant wants work; Aider's best is edit
        self.send(c, "hi")
        self.assertEqual(self.argv()[self.argv().index("--chat-mode") + 1], "code")

    def test_edit_level_uses_code_mode_and_reports_files(self):
        self.rules({"style": "aider", "write": {"app.py": "x = 1\n"}})
        c = self.chat(agents=[aider("edit")])
        self.send(c, "hi")
        self.assertEqual(self.argv()[self.argv().index("--chat-mode") + 1], "code")
        (r,) = self.replies(c)
        self.assertEqual(r["meta"]["files"], ["app.py"])

    def test_git_repo_keeps_the_repo_map(self):
        subprocess.run(["git", "init", "-q", self.folder], check=True)
        c = self.chat(agents=[aider()])
        self.send(c, "hi")
        self.assertNotIn("--no-git", self.argv())
        self.assertIn("--no-auto-commits", self.argv())

    def test_failed_model_call_is_an_error_even_with_exit_0(self):
        self.rules({"style": "aider", "exit": "zero"})
        c = self.chat(agents=[aider()])
        self.send(c, "hi")
        (r,) = self.replies(c)
        self.assertTrue(r["text"].startswith("(error)"), r["text"])
        self.assertIn("litellm.APIError", r["text"])
        self.assertIsNone(c["sessions"]["aider"])

    def test_needs_a_model(self):
        with self.assertRaises(ValueError) as e:
            srv.parse_agents([{"adapter": "aider"}])
        self.assertIn("needs a model", str(e.exception))

    def test_levels(self):
        for level in ("work", "auto"):
            with self.subTest(level=level), self.assertRaises(ValueError):
                srv.parse_agents([aider(level)])
        c = self.chat(agents=[aider(roles=["lead"]), "claude"])  # Lead wants work
        info = srv.level_info(c, "aider")
        self.assertEqual((info["level"], info["requested"]), ("edit", "work"))
        self.assertIn("never allowed to run commands", info["limited"])

    def test_slash_prompt_is_not_an_aider_command(self):
        c = self.chat(agents=[aider(roles=["tester"]), aider(roles=["lead"], name="Aider Lead")])
        self.send(c, "build")
        # the forced Tester turn has no chat messages, only the note: it must not start with "/"
        self.assertEqual(len(self.calls("aider")), 2)


class HistoryParsing(unittest.TestCase):
    def test_new_reply(self):
        text = ("\n# aider chat started at 2026-11-22\n\n#### [User]: hi\n#### more\n\n"
                "Here is the fix.\n\nIt works.\n\n> Applied edit to app.py\n>\n")
        self.assertEqual(new_reply(text), "Here is the fix.\n\nIt works.")


if __name__ == "__main__":
    unittest.main()
