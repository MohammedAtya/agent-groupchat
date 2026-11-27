"""The Antigravity (agy) adapter, against a fake `agy` (tests/bin/fake_agent.py antigravity)."""
import json
import os
import unittest
from unittest import mock

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

    def test_commands_only_in_its_terminal_sandbox(self):
        with mock.patch.object(agy, "sandbox_problem", return_value=""):
            c = self.chat(agents=[a(roles=["lead"])])  # the Lead role wants commands
            self.assertEqual(srv.level_info(c, "antigravity")["level"], "work")
            self.send(c, "hi")
        argv = self.argv()
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        st = self.settings(argv)
        self.assertEqual(st["toolPermission"], "proceed-in-sandbox")
        # no allow rule (it would let commands bypass the sandbox), and empty lists so rules saved
        # in the user's own agy settings can't apply either
        self.assertEqual(st["permissions"], {"allow": [], "ask": [], "deny": ["mcp(*)"]})

    def test_every_level_overrides_saved_allow_and_ask_rules(self):
        for level in ("talk", "read", "edit", "work"):
            with self.subTest(level=level):
                p = json.loads(agy.settings(level))["permissions"]
                self.assertEqual((p["allow"], p["ask"]), ([], []))

    def test_no_commands_when_the_sandbox_cant_start(self):
        why = "Ubuntu's AppArmor blocks Antigravity's terminal sandbox"
        with mock.patch.object(agy, "sandbox_problem", return_value=why):
            c = self.chat(agents=[a(roles=["lead"])])
            info = srv.level_info(c, "antigravity")
            self.assertEqual(info["level"], "edit")
            self.assertIn("AppArmor", info["limited"])
            self.send(c, "hi")
        argv = self.argv()
        self.assertNotIn("--sandbox", argv)
        self.assertIn("command(*)", self.settings(argv)["permissions"]["deny"])

    def test_no_auto_level(self):
        with self.assertRaises(ValueError):
            srv.parse_agents([a("auto")])

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


class SandboxCheck(unittest.TestCase):
    """agy runs commands UNSANDBOXED when its sandbox can't start: only offer "work" when it can."""
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.sysctl = os.path.join(self.dir, "restrict")
        self.profiles = os.path.join(self.dir, "apparmor.d")
        os.makedirs(self.profiles)
        self.exe = os.path.join(self.dir, "agy")
        open(self.exe, "w").close()
        os.chmod(self.exe, 0o755)
        self.clone, self.maxns = os.path.join(self.dir, "clone"), os.path.join(self.dir, "maxns")
        self.patches = [mock.patch.object(agy, "USERNS_SYSCTL", self.sysctl),
                        mock.patch.object(agy, "USERNS_CLONE", self.clone),
                        mock.patch.object(agy, "MAX_USERNS", self.maxns),
                        mock.patch.object(agy, "APPARMOR_DIR", self.profiles),
                        mock.patch.object(agy.platform, "system", return_value="Linux"),
                        mock.patch.object(agy.shutil, "which", return_value=self.exe)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        import shutil
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def restrict(self, on):
        with open(self.sysctl, "w") as f:
            f.write("1\n" if on else "0\n")

    def test_no_restriction(self):
        self.restrict(False)
        self.assertEqual(agy.sandbox_problem(), "")

    def test_restricted_without_a_profile(self):
        self.restrict(True)
        self.assertIn("AppArmor", agy.sandbox_problem())

    def test_restricted_with_a_profile_for_agy(self):
        self.restrict(True)
        with open(os.path.join(self.profiles, "agy"), "w") as f:
            f.write(f"profile agy {self.exe} flags=(unconfined) {{\n  userns,\n}}\n")
        self.assertEqual(agy.sandbox_problem(), "")

    def test_a_profile_for_another_program_doesnt_count(self):
        self.restrict(True)
        with open(os.path.join(self.profiles, "bwrap"), "w") as f:
            f.write("profile bwrap /usr/bin/bwrap flags=(unconfined) {\n  userns,\n}\n")
        self.assertIn("AppArmor", agy.sandbox_problem())

    def profile(self, text, name="agy"):
        with open(os.path.join(self.profiles, name), "w") as f:
            f.write(text)

    def test_what_doesnt_count_as_a_profile_for_agy(self):
        self.restrict(True)
        cases = {
            "only in a comment": f"# profile agy {self.exe} {{ userns, }}\n",
            "deny userns": f"profile agy {self.exe} flags=(unconfined) {{\n  deny userns,\n}}\n",
            "another path that starts the same": f"profile agy {self.exe}-old flags=(unconfined) {{\n  userns,\n}}\n",
            "no userns rule": f"profile agy {self.exe} flags=(unconfined) {{\n  file,\n}}\n",
            "userns only in a comment": f"profile agy {self.exe} {{\n  # userns,\n}}\n",
        }
        for why, text in cases.items():
            with self.subTest(why=why):
                self.profile(text)
                self.assertIn("AppArmor", agy.sandbox_problem())

    def test_backups_and_disabled_profiles_dont_count(self):
        self.restrict(True)
        good = f"profile agy {self.exe} flags=(unconfined) {{\n  userns,\n}}\n"
        for name in ("agy.dpkg-old", "agy~", ".agy", "agy.bak"):
            with self.subTest(name=name):
                for n in os.listdir(self.profiles):
                    p = os.path.join(self.profiles, n)
                    if os.path.isfile(p):
                        os.remove(p)
                self.profile(good, name)
                self.assertIn("AppArmor", agy.sandbox_problem())
        self.profile(good, "agy")
        os.makedirs(os.path.join(self.profiles, "disable"), exist_ok=True)
        open(os.path.join(self.profiles, "disable", "agy"), "w").close()
        self.assertIn("AppArmor", agy.sandbox_problem())  # disabled
        os.remove(os.path.join(self.profiles, "disable", "agy"))
        self.assertEqual(agy.sandbox_problem(), "")

    def test_user_namespaces_switched_off(self):
        self.restrict(False)
        for f in (self.clone, self.maxns):
            with self.subTest(f=os.path.basename(f)):
                with open(f, "w") as fh:
                    fh.write("0\n")
                self.assertIn("user namespaces", agy.sandbox_problem())
                os.remove(f)
        self.assertEqual(agy.sandbox_problem(), "")

    def test_other_systems(self):
        with mock.patch.object(agy.platform, "system", return_value="Darwin"):
            self.assertEqual(agy.sandbox_problem(), "")
        with mock.patch.object(agy.platform, "system", return_value="Windows"):
            self.assertIn("isn't available", agy.sandbox_problem())


if __name__ == "__main__":
    unittest.main()
