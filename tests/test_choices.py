"""Decision buttons: an agent ends its reply with 'CHOICES: a | b' and the user gets buttons."""
import unittest

from tests.helpers import ChatTest, srv


class SplitChoices(unittest.TestCase):
    def test_parsed(self):
        cases = {
            "Pick one.\nCHOICES: Yes | No | Show the diff first": ("Pick one.", ["Yes", "No", "Show the diff first"]),
            "Which?\n**CHOICES:** `Postgres` | `SQLite`\n": ("Which?", ["Postgres", "SQLite"]),
            "Hm.\nchoices: A | a | B": ("Hm.", ["A", "B"]),  # duplicates dropped
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(srv.split_choices(text), want)

    def test_not_choices(self):
        for text in ("Just text", "x\nCHOICES: only one", "x\nCHOICES: a|b|c|d|e|f",
                     "x\nCHOICES: ok | " + "y" * 81, "CHOICES: a | b\nmore text after it", ""):
            with self.subTest(text=text):
                self.assertEqual(srv.split_choices(text), (text, None))


class ChoicesInChat(ChatTest):
    def test_reply_gets_buttons_and_loses_the_line(self):
        self.rules({"style": "claude", "reply": "Two ways to do it.\nCHOICES: Fast | Safe"})
        c = self.chat()
        self.send(c, "@claude how?")
        m = self.replies(c, "claude")[0]
        self.assertEqual(m["text"], "Two ways to do it.")
        self.assertEqual(m["meta"]["choices"], ["Fast", "Safe"])
        self.assertIn("Choices: [Fast] · [Safe]", srv.export_md(c))

    def test_agents_are_told_how(self):
        c = self.chat()
        self.assertIn("CHOICES: first option | second option", srv.intro("claude", c))

    def test_failed_or_bare_replies_are_left_alone(self):
        self.rules({"style": "claude", "reply": "CHOICES: Fast | Safe"})
        c = self.chat()
        self.send(c, "@claude how?")
        m = self.replies(c, "claude")[0]
        self.assertEqual(m["text"], "CHOICES: Fast | Safe")
        self.assertNotIn("choices", m.get("meta") or {})

    def test_clicking_answers_that_agent_only(self):
        self.rules({"style": "claude", "if": "how?", "reply": "Which?\nCHOICES: Fast | Safe"},
                   {"style": "claude", "reply": "Going with it."})
        c = self.chat()
        self.send(c, "@claude how?")
        self.send(c, "@claude Safe")  # what the button sends
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude", "claude"])
        self.assertEqual(self.calls("codex"), [])


if __name__ == "__main__":
    unittest.main()
