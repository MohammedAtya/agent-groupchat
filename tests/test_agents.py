"""How each agent CLI is started and how its output is read.

These pin down today's exact command lines, so refactors can't silently change
what permissions an agent gets.
"""
import os
import re
import unittest

from tests.helpers import ChatTest, srv

READ = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Skill", "TodoWrite"]
EDIT = READ + ["Edit", "Write", "NotebookEdit"]
WORK = EDIT + ["Bash"]
EDIT_NAMES = ["Edit", "MultiEdit", "Write", "NotebookEdit"]
SANDBOX = '{"sandbox": {"enabled": true, "autoAllowBashIfSandboxed": true}}'
UUID = re.compile(r"^[0-9a-f-]{36}$")


def claude_args(argv):
    """Replace the parts that change every run with placeholders."""
    a = list(argv)
    for flag, ph in (("--append-system-prompt", "<intro>"), ("--session-id", "<sid>"),
                     ("--resume", "<sid>")):
        if flag in a:
            a[a.index(flag) + 1] = ph
    a[-1] = "<prompt>"
    return a


def claude_expected(perms, resume=False):
    return ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--append-system-prompt", "<intro>", "--resume" if resume else "--session-id", "<sid>",
            *perms, "--", "<prompt>"]


def codex_args(argv):
    a = list(argv)
    a[a.index("-o") + 1] = "<out>"
    if "resume" in a:
        a[a.index("resume") + 1] = "<sid>"
    a[-1] = "<prompt>"
    return a


def codex_conf(sandbox, auto=False):
    c = ["-c", f'sandbox_mode="{sandbox}"']
    if auto:
        return c + ["-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="auto_review"']
    return c + ["-c", 'approval_policy="never"']


SAFE_BASH = ["Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)", "Bash(git show:*)",
             "Bash(ls:*)"]
TEST_BASH = ["Bash(pytest:*)", "Bash(python -m pytest:*)", "Bash(python3 -m pytest:*)",
             "Bash(python -m unittest:*)", "Bash(python3 -m unittest:*)", "Bash(npm test:*)",
             "Bash(npm run test:*)", "Bash(cargo test:*)", "Bash(go test:*)"]
TALK = ["--permission-mode", "dontAsk", "--tools", ""]
READ_SANDBOX = ["--permission-mode", "dontAsk", "--settings", SANDBOX, "--allowedTools", *READ,
                *SAFE_BASH, *TEST_BASH, "--disallowedTools", *EDIT_NAMES]
READ_NO_SANDBOX = ["--permission-mode", "dontAsk", "--allowedTools", *READ, *SAFE_BASH,
                   "--disallowedTools", *EDIT_NAMES]
EDIT_ONLY = ["--permission-mode", "acceptEdits", "--allowedTools", *READ, "--disallowedTools", "Bash"]
WORK_FLAGS = ["--permission-mode", "acceptEdits", "--settings", SANDBOX, "--allowedTools", *WORK]
AUTO = ["--permission-mode", "auto"]


class ClaudeCommand(ChatTest):
    def run_one(self, level="read", text="@claude hi", **kw):
        c = self.chat(agents=[{"adapter": "claude", "level": level}, "codex"], **kw)
        self.send(c, text)
        return c

    def flags(self):
        return claude_args(self.calls("claude")[-1]["argv"])

    def test_each_level(self):
        for sandbox, level, perms in [
                (True, "talk", TALK), (True, "read", READ_SANDBOX), (False, "read", READ_NO_SANDBOX),
                (True, "edit", EDIT_ONLY), (True, "work", WORK_FLAGS),
                (False, "work", EDIT_ONLY),  # no sandbox: commands can't be confined -> edit only
                (True, "auto", AUTO), (False, "auto", AUTO)]:
            with self.subTest(level=level, sandbox=sandbox):
                self.sandbox = sandbox
                self.run_one(level)
                self.assertEqual(self.flags(), claude_expected(perms))

    def test_default_participant_works(self):
        c = self.chat()
        self.send(c, "@claude hi")
        self.assertEqual(self.flags(), claude_expected(WORK_FLAGS))

    def test_talk_only_lock_beats_everything(self):
        self.run_one("auto", lock=True)
        self.assertEqual(self.flags(), claude_expected(TALK))

    def test_cwd_is_the_project(self):
        self.run_one()
        self.assertEqual(self.calls("claude")[0]["cwd"], self.folder)

    def test_session_is_created_then_resumed(self):
        c = self.run_one("read")
        first = self.calls("claude")[0]["argv"]
        sid = first[first.index("--session-id") + 1]
        self.assertRegex(sid, UUID)
        self.assertEqual(c["sessions"]["claude"], sid)
        self.send(c, "@claude again")
        second = self.calls("claude")[1]["argv"]
        self.assertEqual(second[second.index("--resume") + 1], sid)
        self.assertNotIn("--session-id", second)

    def test_intro_and_prompt(self):
        self.run_one("read")
        argv = self.calls("claude")[0]["argv"]
        intro = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn("You are Claude, a member of a group chat with a human user and "
                      "these AI agents: Codex (OpenAI's coding agent).", intro)
        self.assertIn("([Codex])", intro)
        self.assertIn("@codex reaches that one only", intro)
        self.assertIn(f"Project folder: {self.folder}.", intro)
        self.assertIn("READ: you may read", intro)
        self.assertNotIn("sandbox mount points", intro)
        self.assertEqual(argv[-1], "[User]: @claude hi")

    def test_reply_and_stats(self):
        c = self.run_one("read")
        (r,) = self.replies(c, "claude")
        self.assertEqual(r["text"], "fake claude reply")
        self.assertEqual(r["meta"]["cost"], 0.0123)
        self.assertEqual(r["meta"]["tokens"], [115, 7])
        self.assertEqual(r["meta"]["for"], [0])
        self.assertEqual(c["messages"][0]["status"], {"claude": "done"})

    def test_failure_becomes_error_reply(self):
        self.rules({"style": "claude", "exit": 3})
        c = self.run_one("read")
        (r,) = self.replies(c, "claude")
        self.assertTrue(r["text"].startswith("(error)"), r["text"])
        self.assertIn("fake failure", r["text"])
        self.assertIsNone(c["sessions"]["claude"])  # never started, so nothing to resume
        self.assertEqual(c["messages"][0]["status"], {"claude": "error"})

    def test_edited_file_shows_in_stats_and_diff(self):
        self.rules({"style": "claude", "write": {"notes.txt": "hello\n"}})
        c = self.run_one("work")
        (r,) = self.replies(c, "claude")
        self.assertEqual(r["meta"]["files"], ["notes.txt"])
        self.assertEqual(r["meta"]["diff"], [1, 0])
        idx = c["messages"].index(r)
        with open(srv.diff_path(c["id"], idx)) as f:
            self.assertIn("+hello", f.read())


class CodexCommand(ChatTest):
    def run_one(self, level="read", text="@codex hi", **kw):
        c = self.chat(agents=["claude", {"adapter": "codex", "level": level}], **kw)
        self.send(c, text)
        return c

    def expected_new(self, conf):
        return ["exec", "--skip-git-repo-check", "--json", *conf, "-C", self.folder,
                "-o", "<out>", "<prompt>"]

    def test_each_level(self):
        for sandbox, level, conf in [
                (True, "talk", codex_conf("read-only")),  # can't hide files from Codex
                (True, "read", codex_conf("read-only")),
                (True, "work", codex_conf("workspace-write")),
                (False, "work", codex_conf("read-only")),
                (True, "auto", codex_conf("workspace-write", auto=True)),
                (False, "auto", codex_conf("read-only"))]:
            with self.subTest(level=level, sandbox=sandbox):
                self.sandbox = sandbox
                self.run_one(level)
                self.assertEqual(codex_args(self.calls("codex")[-1]["argv"]), self.expected_new(conf))

    def test_edit_level_is_refused(self):
        with self.assertRaises(ValueError):
            self.chat("edit", agents=["codex"])

    def test_first_prompt_carries_intro(self):
        self.run_one("read")
        prompt = self.calls("codex")[0]["argv"][-1]
        self.assertTrue(prompt.startswith("You are Codex, a member of a group chat with a human "
                                          "user and these AI agents: Claude (Anthropic's Claude "
                                          "Code)."), prompt)
        self.assertIn("sandbox mount points", prompt)
        self.assertTrue(prompt.endswith("\n\n[User]: @codex hi"), prompt)

    def test_session_is_saved_then_resumed(self):
        c = self.run_one("read")
        sid = c["sessions"]["codex"]
        self.assertTrue(sid.startswith("thread-"), sid)
        self.send(c, "@codex again")
        argv = self.calls("codex")[1]["argv"]
        self.assertEqual(codex_args(argv), ["exec", "--skip-git-repo-check", "--json",
                                            *codex_conf("read-only"), "-o", "<out>",
                                            "resume", "<sid>", "<prompt>"])
        self.assertEqual(argv[argv.index("resume") + 1], sid)
        self.assertEqual(argv[-1], "[User]: @codex again")  # no intro on resume

    def test_reply_and_stats(self):
        c = self.run_one("read")
        (r,) = self.replies(c, "codex")
        self.assertEqual(r["text"], "fake codex reply")
        self.assertEqual(r["meta"]["tokens"], [20, 3])
        self.assertNotIn("cost", r["meta"])

    def test_failure_becomes_error_reply(self):
        self.rules({"style": "codex", "exit": 2})
        c = self.run_one("read")
        (r,) = self.replies(c, "codex")
        self.assertTrue(r["text"].startswith("(error)"), r["text"])
        self.assertIn("fake failure", r["text"])

    def test_edited_file_shows_in_stats(self):
        self.rules({"style": "codex", "write": {"a.py": "x = 1\n"}})
        c = self.run_one("work")
        (r,) = self.replies(c, "codex")
        self.assertEqual(r["meta"]["files"], ["a.py"])


if __name__ == "__main__":
    unittest.main()
