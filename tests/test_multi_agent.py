"""Chats with any number of agents, including several copies of the same AI."""
import json
import os
import re
import unittest

from tests.helpers import ChatTest, srv


def caller(call):
    """Which agent made this run: the name in its intro ("You are Claude 2, ...")."""
    a = call["argv"]
    text = a[a.index("--append-system-prompt") + 1] if "--append-system-prompt" in a else a[-1]
    m = re.match(r"You are (.+?), a member", text)
    return m.group(1) if m else None


class Naming(unittest.TestCase):
    def test_single_copy_keeps_plain_name(self):
        agents = srv.make_agents([{"adapter": "claude"}, {"adapter": "codex"}])
        self.assertEqual([(a["id"], a["name"]) for a in agents],
                         [("claude", "Claude"), ("codex", "Codex")])

    def test_copies_are_numbered(self):
        agents = srv.make_agents([{"adapter": "claude"}, {"adapter": "claude"}, {"adapter": "codex"}])
        self.assertEqual([(a["id"], a["name"]) for a in agents],
                         [("claude-1", "Claude 1"), ("claude-2", "Claude 2"), ("codex", "Codex")])

    def test_custom_and_duplicate_names_stay_unique(self):
        agents = srv.make_agents([{"adapter": "claude", "name": "Claude Tester"},
                                  {"adapter": "codex", "name": "Helper"},
                                  {"adapter": "claude", "name": "helper"}])
        self.assertEqual([(a["id"], a["name"]) for a in agents],
                         [("claude-tester", "Claude Tester"), ("helper", "Helper"),
                          ("helper-2", "helper 2")])

    def test_validation(self):
        for bad in ([], [{"adapter": "nope"}], [{"adapter": "claude", "name": "x" * 41}],
                    [{"adapter": "claude", "name": "[System]"}],
                    [{"adapter": "claude", "name": "---"}],
                    [{"adapter": "claude", "model": "gpt 5; rm -rf"}],
                    [{"adapter": "claude"}] * (srv.MAX_AGENTS + 1), "claude"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    srv.parse_agents(bad)
        ok = srv.parse_agents([{"adapter": "claude", "name": " Claude Lead ", "model": "opus"}])
        self.assertEqual(ok, [{"adapter": "claude", "name": "Claude Lead", "model": "opus"}])


class ThreeAgents(ChatTest):
    def make(self, **kw):
        return self.chat(agents=["claude", "claude", "codex"], **kw)

    def test_everyone_answers_in_separate_sessions(self):
        c = self.make()
        self.send(c, "hello")
        self.assertEqual(sorted(m["who"] for m in self.replies(c)), ["claude-1", "claude-2", "codex"])
        self.assertEqual(sorted(caller(r) for r in self.calls()), ["Claude 1", "Claude 2", "Codex"])
        sids = [c["sessions"][b] for b in ("claude-1", "claude-2", "codex")]
        self.assertEqual(len(set(sids)), 3)
        self.assertTrue(all(sids))

    def test_intro_names_the_others(self):
        c = self.make()
        self.send(c, "@claude-1 hi")
        (call,) = self.calls()
        a = call["argv"]
        intro = a[a.index("--append-system-prompt") + 1]
        self.assertIn("You are Claude 1,", intro)
        self.assertIn("Claude 2 (Anthropic's Claude Code, a separate session of the same AI as you)",
                      intro)
        self.assertIn("Codex (OpenAI's coding agent)", intro)
        self.assertIn("([Claude 2], [Codex])", intro)

    def test_messages_are_labeled_with_agent_names(self):
        c = self.make()
        self.send(c, "@claude-2 first")
        self.send(c, "@codex second")
        prompt = self.calls("codex")[0]["argv"][-1]
        self.assertIn("[Claude 2]: fake claude reply", prompt)

    def test_mentions(self):
        cases = {"@claude-2 hi": ["claude-2"], "@claude2 hi": ["claude-2"],
                 "@Claude-1 and @codex": ["claude-1", "codex"],
                 "@claude everyone named claude": ["claude-1", "claude-2"],
                 "mail me@claude-2.com": []}
        c = self.make()
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(srv.mentioned(c, text), want)

    def test_mention_reaches_only_that_copy(self):
        c = self.make()
        self.send(c, "@claude-2 just you")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude-2"])
        self.assertEqual([caller(r) for r in self.calls()], ["Claude 2"])

    def test_handoff_by_name(self):
        self.rules({"if": "You are Claude 1", "reply": "Claude 2, please run the tests."})
        c = self.make()
        self.send(c, "@claude-1 start")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude-1", "claude-2"])

    def test_discussion_includes_everyone(self):
        self.rules({"if": "(Discussion round", "reply": "PASS"})
        c = self.make(rounds=1)
        self.send(c, "hello")
        notes = [caller(r) or r["style"] for r in self.calls() if "(Discussion round" in r["argv"][-1]]
        self.assertEqual(notes, ["Claude 1", "Claude 2", "codex"])  # resumed Codex: no intro

    def test_lead_is_reviewed_by_the_next_agent(self):
        c = self.make(mode="work", lead="claude-2")
        self.send(c, "build it")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude-2", "codex"])
        self.assertIn("then Codex reviews it", self.calls()[0]["argv"][-1])
        self.assertEqual(c["messages"][0]["to"], ["claude-2", "codex"])

    def test_reviewer_wraps_around(self):
        c = self.make()
        self.assertEqual(srv.reviewer_of(c, "codex"), "claude-1")
        self.assertIsNone(srv.reviewer_of(c, "nobody"))

    def test_skill_goes_to_every_copy_that_has_it(self):
        d = os.path.join(self.folder, ".claude", "skills", "gc-test-multi")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w") as f:
            f.write("---\nname: gc-test-multi\n---\n")
        srv._skill_cache.clear()
        c = self.make()
        self.send(c, "/gc-test-multi go")
        self.assertEqual(sorted(m["who"] for m in self.replies(c)), ["claude-1", "claude-2"])

    def test_compact_resets_every_session(self):
        c = self.make()
        self.send(c, "hello")
        self.rules({"if": "(The user pressed Compact", "reply": "- summary"})
        srv.compact(c)
        self.assertEqual(c["sessions"], {"claude-1": None, "claude-2": None, "codex": None})
        self.assertEqual(c["messages"][-1]["who"], "claude-1")  # the first agent summarizes

    def test_export_uses_agent_names(self):
        c = self.make()
        self.send(c, "hello")
        md = srv.export_md(c)
        self.assertIn("## Claude 2 ·", md)


class SingleAgent(ChatTest):
    def test_alone(self):
        c = self.chat(agents=["codex"])
        self.send(c, "hello")
        self.assertEqual([m["who"] for m in self.replies(c)], ["codex"])
        prompt = self.calls()[0]["argv"][-1]
        self.assertIn("a human user and no other AI agents", prompt)
        self.assertIsNone(srv.reviewer_of(c, "codex"))


class Models(ChatTest):
    def test_each_copy_gets_its_model(self):
        c = self.chat(agents=[{"adapter": "claude", "model": "opus"},
                              {"adapter": "claude"},
                              {"adapter": "codex", "model": "gpt-5.1-codex"}])
        self.send(c, "hello")
        by = {caller(r): r["argv"] for r in self.calls()}
        a1 = by["Claude 1"]
        self.assertEqual(a1[a1.index("--model") + 1], "opus")
        self.assertLess(a1.index("--model"), a1.index("--"))
        self.assertNotIn("--model", by["Claude 2"])
        cx = by["Codex"]
        self.assertEqual(cx[cx.index('model="gpt-5.1-codex"') - 1], "-c")

    def test_codex_model_also_on_resume(self):
        c = self.chat(agents=[{"adapter": "codex", "model": "o3"}])
        self.send(c, "one")
        self.send(c, "two")
        argv = self.calls()[1]["argv"]
        self.assertIn("resume", argv)
        self.assertLess(argv.index('model="o3"'), argv.index("resume"))


class OldChats(ChatTest):
    def test_chat_saved_before_agent_lists_loads_as_claude_and_codex(self):
        cid = "oldchat00001"
        with open(srv.chat_path(cid), "w") as f:
            json.dump({"id": cid, "title": "old", "folder": self.folder, "mode": "talk",
                       "rounds": 0, "created": 0, "updated": 0, "messages": [
                           {"who": "user", "text": "/x hi", "ts": 0, "to": ["claude"],
                            "status": {"claude": "done"},
                            "skill": {"name": "x", "args": "hi", "bots": ["claude"]}}],
                       "seen": {"claude": 1, "codex": 0},
                       "sessions": {"claude": "abc", "codex": None}}, f)
        srv.load_all()
        c = srv.chats.pop(cid)
        self.assertEqual(srv.agent_ids(c), ["claude", "codex"])
        self.assertEqual(c["sessions"]["claude"], "abc")
        self.assertEqual(srv.pending(c)[0], [])  # old "bots" skill format still routes
        os.remove(srv.chat_path(cid))


if __name__ == "__main__":
    unittest.main()
