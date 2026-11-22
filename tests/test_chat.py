"""Who answers when: routing, discussion rounds, hand-offs, Lead + Review, skills, Compact."""
import os
import unittest

from tests.helpers import ChatTest, srv


class Routing(ChatTest):
    def test_plain_message_goes_to_both(self):
        c = self.chat()
        self.send(c, "hello")
        self.assertEqual(sorted(m["who"] for m in self.replies(c)), ["claude", "codex"])
        self.assertEqual(c["messages"][0]["to"], ["claude", "codex"])
        self.assertEqual(c["messages"][0]["status"], {"claude": "done", "codex": "done"})

    def test_mention_goes_to_one(self):
        for who in ("claude", "codex"):
            with self.subTest(who=who):
                c = self.chat()
                self.send(c, f"@{who} only you")
                self.assertEqual([m["who"] for m in self.replies(c)], [who])

    def test_other_bot_sees_skipped_messages_later_as_context(self):
        c = self.chat()
        self.send(c, "@claude first")
        self.send(c, "@codex second")
        prompt = self.calls("codex")[0]["argv"][-1]
        self.assertIn("[User]: @claude first", prompt)
        self.assertIn("[Claude]: fake claude reply", prompt)
        self.assertIn("[User]: @codex second", prompt)

    def test_pass_is_not_posted(self):
        self.rules({"style": "codex", "reply": "PASS"})
        c = self.chat()
        self.send(c, "hello")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude"])
        self.assertEqual(c["messages"][0]["status"]["codex"], "passed")


class Discussion(ChatTest):
    def test_rounds_take_turns_and_stop_when_both_pass(self):
        self.rules({"if": "(Discussion round", "reply": "PASS"})
        c = self.chat(rounds=2)
        self.send(c, "hello")
        self.assertEqual(len(self.replies(c)), 2)
        # one round was tried by both, then it stopped because nobody spoke
        notes = [r for r in self.calls() if "(Discussion round" in r["argv"][-1]]
        self.assertEqual([r["style"] for r in notes], ["claude", "codex"])

    def test_rounds_continue_while_someone_speaks(self):
        self.rules({"style": "claude", "if": "(Discussion round", "reply": "one more thing"},
                   {"if": "(Discussion round", "reply": "PASS"})
        c = self.chat(rounds=2)
        self.send(c, "hello")
        # round 1: Claude speaks, Codex reads it and passes; round 2: nobody has anything
        # new to read, so nobody is called and the rounds end
        self.assertEqual([m["who"] for m in self.replies(c)][2:], ["claude"])
        self.assertEqual(self.replies(c)[2]["text"], "one more thing")
        self.assertEqual(len(self.calls()), 4)

    def test_no_rounds_for_single_target(self):
        c = self.chat(rounds=3)
        self.send(c, "@claude hi")
        self.assertEqual(len(self.calls()), 1)


class Handoffs(ChatTest):
    def test_reply_addressing_other_bot_reaches_it(self):
        self.rules({"style": "claude", "reply": "@codex can you check the tests?"})
        c = self.chat()
        self.send(c, "@claude hi")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude", "codex"])
        self.assertIn("(Another AI addressed you directly", self.calls("codex")[0]["argv"][-1])

    def test_name_with_comma_counts_as_addressing(self):
        self.rules({"style": "codex", "reply": "Claude, please write the file."})
        c = self.chat()
        self.send(c, "@codex hi")
        self.assertEqual([m["who"] for m in self.replies(c)], ["codex", "claude"])

    def test_no_handoff_when_user_wrote_to_both(self):
        self.rules({"style": "claude", "reply": "@codex your turn"})
        c = self.chat()
        self.send(c, "hello")
        self.assertEqual(len(self.calls("codex")), 1)


class Skills(ChatTest):
    def make_skill(self, base, name):
        d = os.path.join(self.folder, base, "skills", name)
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w") as f:
            f.write(f"---\nname: {name}\ndescription: test skill\n---\nbody\n")
        srv._skill_cache.clear()

    def test_claude_skill_runs_as_slash_command(self):
        self.make_skill(".claude", "gc-test-only-claude")
        c = self.chat()
        self.send(c, "/gc-test-only-claude my plan")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude"])
        self.assertEqual(self.calls("claude")[0]["argv"][-1], "/gc-test-only-claude my plan")

    def test_codex_skill_runs_as_dollar_mention(self):
        self.make_skill(".agents", "gc-test-only-codex")
        c = self.chat()
        self.send(c, "/gc-test-only-codex my plan")
        self.assertEqual([m["who"] for m in self.replies(c)], ["codex"])
        self.assertIn("[User]: $gc-test-only-codex my plan", self.calls("codex")[0]["argv"][-1])

    def test_skill_list_says_who_has_it(self):
        self.make_skill(".claude", "gc-test-both")
        self.make_skill(".agents", "gc-test-both")
        s = [s for s in srv.list_skills(self.folder) if s["name"] == "gc-test-both"]
        self.assertEqual(s[0]["adapters"], ["claude", "codex"])


class Compact(ChatTest):
    def test_summary_then_fresh_sessions(self):
        c = self.chat()
        self.send(c, "hello")
        old = dict(c["sessions"])
        self.rules({"style": "claude", "if": "(The user pressed Compact", "reply": "- summary"})
        srv.compact(c)
        self.assertEqual(c["sessions"], {"claude": None, "codex": None})
        self.assertEqual(c["old_sessions"], [old])
        self.assertEqual(c["messages"][-1]["meta"]["role"], "summary")
        self.send(c, "@codex continue")
        prompt = self.calls("codex")[-1]["argv"][-1]
        self.assertIn("You are Codex", prompt)  # a fresh session gets the intro again
        self.assertIn("[Chat summary]: - summary", prompt)


class Export(ChatTest):
    def test_markdown_names(self):
        c = self.chat()
        self.send(c, "hello")
        md = srv.export_md(c)
        self.assertIn("## You ·", md)
        self.assertIn("## Claude ·", md)
        self.assertIn("## Codex ·", md)


if __name__ == "__main__":
    unittest.main()
