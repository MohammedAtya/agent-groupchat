"""The Gemini CLI adapter, against a fake `gemini` (tests/bin/gemini)."""
import os
import unittest
from unittest import mock

from tests.helpers import ChatTest, srv
from agent_groupchat.adapters import gemini as gem


def g(level=None, **kw):
    return {"adapter": "gemini", **({"level": level} if level else {}), **kw}


def perms(argv):
    i = argv.index("--approval-mode")
    return argv[i:argv.index("--prompt")]


class GeminiAgent(ChatTest):
    def setUp(self):
        super().setUp()
        self.runtime = mock.patch.object(gem, "sandbox_runtime", return_value="docker")
        self.runtime.start()

    def tearDown(self):
        self.runtime.stop()
        super().tearDown()

    def argv(self, i=-1):
        return self.calls("gemini")[i]["argv"]

    def test_each_level(self):
        for level, expect in (("talk", ["--approval-mode", "default", "--policy", gem.TALK_POLICY]),
                              ("read", ["--approval-mode", "default"]),
                              ("edit", ["--approval-mode", "auto_edit"]),
                              ("work", ["--approval-mode", "yolo", "--sandbox"])):
            with self.subTest(level=level):
                c = self.chat(agents=[g(level)])
                self.send(c, "hi")
                self.assertEqual(perms(self.argv()), expect)
        self.assertTrue(os.path.exists(gem.TALK_POLICY))

    def test_work_needs_docker_or_podman(self):
        with mock.patch.object(gem, "sandbox_runtime", return_value=None):
            c = self.chat(agents=[g(roles=["lead"])])
            info = srv.level_info(c, "gemini-cli")
            self.assertEqual(info["level"], "edit")
            self.assertIn("Docker or Podman", info["limited"])
            self.send(c, "hi")
            self.assertNotIn("--sandbox", self.argv())

    def test_no_auto(self):
        with self.assertRaises(ValueError):
            srv.parse_agents([g("auto")])

    def test_session_intro_and_resume(self):
        c = self.chat(agents=[g("read"), "claude"])
        self.send(c, "@gemini one")
        first = self.argv()
        sid = first[first.index("--session-id") + 1]
        self.assertEqual(c["sessions"]["gemini-cli"], sid)
        self.assertIn("--skip-trust", first)
        self.assertTrue(first[-1].startswith("You are Gemini"), first[-1])
        self.send(c, "@gemini two")
        second = self.argv()
        self.assertEqual(second[second.index("--resume") + 1], sid)
        self.assertNotIn("--session-id", second)
        self.assertEqual(second[-1], "[User]: @gemini two")

    def test_reply_tokens_files(self):
        self.rules({"style": "gemini", "reply": "Added the function.", "write": {"a.py": "x\n"}})
        c = self.chat(agents=[g("edit")])
        self.send(c, "hi")
        (r,) = self.replies(c)
        self.assertEqual(r["text"], "Added the function.")
        self.assertEqual(r["meta"]["tokens"], [30, 4])
        self.assertEqual(r["meta"]["files"], ["a.py"])

    def test_errors(self):
        self.rules({"style": "gemini", "exit": 41})
        c = self.chat(agents=[g("read")])
        self.send(c, "hi")
        (r,) = self.replies(c)
        self.assertTrue(r["text"].startswith("(error) Gemini CLI isn't logged in"), r["text"])
        self.assertIn("quota exceeded", r["text"])

    def test_model(self):
        c = self.chat(agents=[g("read", model="gemini-2.5-pro")])
        self.send(c, "hi")
        self.assertEqual(self.argv()[self.argv().index("--model") + 1], "gemini-2.5-pro")


if __name__ == "__main__":
    unittest.main()
