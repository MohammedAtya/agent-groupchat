"""The Antigravity (agy) adapter, against a fake `agy` (tests/bin/fake_agent.py antigravity)."""
import json
import os
import unittest

from tests.helpers import ChatTest, srv
from agent_groupchat.adapters import antigravity as agy
from agent_groupchat.adapters import base


def a(level=None, **kw):
    return {"adapter": "antigravity", **({"level": level} if level else {}), **kw}


class AntigravityAgent(ChatTest):
    def argv(self, i=-1):
        return self.calls("antigravity")[i]["argv"]

    def settings(self, argv):
        return json.loads(argv[argv.index("--settings") + 1])

    def test_each_level_denies_what_it_doesnt_allow(self):
        for level, denied in (("talk", {"read_file(*)", "write_file(*)", "command(*)"}),
                              ("read", {"write_file(*)", "command(*)"}),
                              ("edit", {"command(*)"})):
            with self.subTest(level=level):
                c = self.chat(agents=[a(level)])
                self.send(c, "hi")
                st = self.settings(self.argv())
                self.assertEqual(st["agentMode"], "")  # Default mode, never Auto
                self.assertLessEqual(denied, set(st["permissions"]["deny"]))
                if level == "edit":
                    self.assertNotIn("write_file(*)", st["permissions"]["deny"])
                if level != "talk":
                    self.assertNotIn("read_file(*)", st["permissions"]["deny"])

    def test_no_command_levels(self):
        for level in ("work", "auto"):
            with self.subTest(level=level), self.assertRaises(ValueError):
                srv.parse_agents([a(level)])
        c = self.chat(agents=[a(roles=["lead"])])  # the Lead role wants commands
        self.assertEqual(srv.level_info(c, "antigravity")["level"], "edit")
        self.send(c, "hi")
        self.assertIn("command(*)", self.settings(self.argv())["permissions"]["deny"])

    def test_both_googles_in_one_chat_keep_their_own_mention(self):
        # agy used to be named "Gemini": its id took @gemini away from the Gemini CLI agents
        c = self.chat(agents=[a(), {"adapter": "gemini"}])
        self.assertEqual([(x["id"], x["name"]) for x in c["agents"]],
                         [("antigravity", "Antigravity"), ("gemini-cli", "Gemini CLI")])
        al = srv.aliases(c)
        self.assertEqual((al["gemini"], al["antigravity"]), (["gemini-cli"], ["antigravity"]))

    def test_named_antigravity_and_mentionable(self):
        c = self.chat(agents=[a("read"), "claude"])
        self.assertEqual([x["id"] for x in c["agents"]], ["antigravity", "claude"])
        self.send(c, "@antigravity one")
        self.assertEqual(len(self.calls("antigravity")), 1)
        self.assertEqual(self.calls("claude"), [])

    def test_session_intro_and_resume(self):
        c = self.chat(agents=[a("read"), "claude"])
        self.send(c, "@antigravity one")
        first = self.argv()
        self.assertNotIn("--conversation", first)
        self.assertTrue(first[-1].startswith("--print=You are Antigravity"), first[-1])
        sid = c["sessions"]["antigravity"]
        self.assertTrue(sid)
        self.send(c, "@antigravity two")
        second = self.argv()
        self.assertEqual(second[second.index("--conversation") + 1], sid)
        self.assertEqual(second[-1], "--print=[User]: @antigravity two")

    def test_reply_is_the_last_message_with_tokens_and_files(self):
        self.rules({"style": "antigravity", "filler": "Let me look.",
                    "reply": "Added the function.", "write": {"a.py": "x\n"}})
        c = self.chat(agents=[a("edit")])
        self.send(c, "hi")
        (r,) = self.replies(c)
        self.assertEqual(r["text"], "Added the function.")
        self.assertEqual(r["meta"]["tokens"], [100, 4])
        self.assertEqual(r["meta"]["files"], ["a.py"])

    def test_errors(self):
        self.rules({"style": "antigravity", "exit": 3})
        c = self.chat(agents=[a("read")])
        self.send(c, "hi")
        (r,) = self.replies(c)
        self.assertTrue(r["text"].startswith("(error) quota exceeded"), r["text"])

    def test_turn_stopped_for_an_approval(self):
        self.rules({"style": "antigravity", "needs_approval": True})
        c = self.chat(agents=[a("edit")])
        self.send(c, "hi")
        (r,) = self.replies(c)
        self.assertTrue(r["text"].startswith("(error) Antigravity tried something that needs your "
                                             "approval"), r["text"])

    def test_long_prompt_goes_on_stdin(self):
        c = self.chat(agents=[a("read")])
        self.send(c, "x" * (base.ARGV_LIMIT + 10))
        run = self.calls("antigravity")[-1]
        self.assertEqual(run["argv"][-1], "--print=")
        self.assertIn("--input-format", run["argv"])
        msg = json.loads(run["stdin"])
        self.assertEqual(msg["event"], "user")
        self.assertIn("x" * 1000, msg["message"]["content"])
        self.assertEqual(self.replies(c)[0]["text"], "fake antigravity reply")

    def test_model(self):
        c = self.chat(agents=[a("read", model="gemini-4-neon-high")])
        self.send(c, "hi")
        self.assertEqual(self.argv()[self.argv().index("--model") + 1], "gemini-4-neon-high")

    def test_describe_tool(self):
        # a real absolute path on this OS (Python 3.13 on Windows: "/p" without a drive isn't one)
        f = os.path.abspath("p")
        path = os.path.join(f, "a.py")
        self.assertEqual(agy.describe_tool("read_file", {"Path": path}, f), "📖 reading a.py")
        self.assertEqual(agy.describe_tool("edit_file", {"Path": path}, f), "✏️ editing a.py")
        self.assertEqual(agy.describe_tool("run_command", {"CommandLine": "ls -la"}, f), "$ ls -la")


if __name__ == "__main__":
    unittest.main()
