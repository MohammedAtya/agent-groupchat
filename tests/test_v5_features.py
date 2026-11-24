"""Loose ends, the hand-off pause and ⚖ Debate (SPEC §6.4, §7.5, §7.6)."""
import os
import unittest
import unittest.mock

from tests.helpers import ChatTest, srv
from tests.test_race import RULES as RACE_RULES, RaceTest


def a(adapter, *roles, **kw):
    return {"adapter": adapter, "roles": list(roles), **kw}


def prompt(call):
    return call["stdin"] or call["argv"][-1]


class SplitLooseEnds(unittest.TestCase):
    def test_parsed(self):
        cases = {
            "Done.\nLOOSE ENDS:\n- no tests for empty input\n- TODO in api.py":
                ("Done.", ["no tests for empty input", "TODO in api.py"]),
            "Done.\n**Loose ends:**\n1. retry is hard-coded\n2) `config` isn't validated":
                ("Done.", ["retry is hard-coded", "`config` isn't validated"]),
            "Done.\nLOOSE ENDS:\n- none": ("Done.", None),  # the list goes, nothing to show
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(srv.split_loose_ends(text), want)

    def test_not_loose_ends(self):
        for text in ("Just text", "LOOSE ENDS:\n- a\nand then more prose", "Done.\nLOOSE ENDS:",
                     "x\nLOOSE ENDS:\n" + "\n".join(f"- {i}" for i in range(16))):
            with self.subTest(text=text):
                self.assertEqual(srv.split_loose_ends(text), (text, None))

    def test_with_choices_after_them(self):
        reply = "Built it.\nLOOSE ENDS:\n- no docs\nCHOICES: Ship | Polish"
        meta = {}
        self.assertEqual(srv.take_endings(reply, meta), "Built it.")
        self.assertEqual(meta, {"choices": ["Ship", "Polish"], "loose_ends": ["no docs"]})


class LooseEndsInTheWorkflow(ChatTest):
    def test_lead_is_asked_and_checkers_are_told(self):
        self.rules({"if": "Workflow · Lead", "reply": "Built it.\nLOOSE ENDS:\n- no tests for empty input"},
                   {"if": "Workflow · Reviewer", "reply": "Fine.\nVERDICT: LGTM"})
        c = self.chat(agents=[a("claude", "lead"), a("codex", "reviewer")])
        self.send(c, "build it")
        lead = self.replies(c, "claude")[0]
        self.assertEqual((lead["text"], lead["meta"]["loose_ends"]), ("Built it.", ["no tests for empty input"]))
        self.assertIn("LOOSE ENDS:", prompt(self.calls("claude")[0]))
        self.assertIn("They listed these loose ends; check whether they matter: no tests for empty input.",
                      prompt(self.calls("codex")[0]))


class LooseEndsInARace(RaceTest):
    def test_copy_card_shows_them(self):
        self.rules({"style": "codex", "if": "(⚡ Race: you and", "write": {"app.py": "codex\n"},
                    "reply": "Done.\nLOOSE ENDS:\n- error handling is minimal"}, *RACE_RULES)
        c, r = self.race()
        self.assertEqual(srv.race_view(c)["copies"]["codex"]["loose_ends"], ["error handling is minimal"])
        racer = next(x for x in self.calls("codex") if "(⚡ Race: you and" in prompt(x))
        self.assertIn("LOOSE ENDS:", prompt(racer))


class HandoffPause(ChatTest):
    def test_says_it_paused_instead_of_stopping_silently(self):
        self.rules({"if": "You are Claude 1", "reply": "Claude 2, please start."},
                   {"if": "You are Claude 2", "reply": "Codex, your turn."},
                   {"style": "codex", "reply": "Claude 2, your turn."})
        c = self.chat(agents=["claude", "claude", "codex"])
        self.send(c, "@claude-1 go")
        agents = [m["who"] for m in self.replies(c) if m["who"] != "system"]
        self.assertEqual(agents, ["claude-1", "claude-2", "codex", "claude-2"])  # 1 + 3 hand-offs
        note = c["messages"][-1]
        self.assertEqual(note["who"], "system")
        self.assertIn("Paused after 3 hand-offs", note["text"])
        self.assertIn("Codex was addressed last", note["text"])

    def test_no_note_when_they_stop_by_themselves(self):
        self.rules({"if": "You are Claude 1", "reply": "Claude 2, please start."},
                   {"if": "You are Claude 2", "reply": "Done."})
        c = self.chat(agents=["claude", "claude", "codex"])
        self.send(c, "@claude-1 go")
        self.assertFalse([m for m in c["messages"] if m["who"] == "system"])


class Debate(ChatTest):
    def team(self):
        return self.chat(agents=[a("claude"), a("codex"), a("claude", "critic")])

    def test_stances_rounds_and_verdict(self):
        self.rules({"if": "verdict: you are the judge", "reply": "Go with SQLite.\nCHOICES: SQLite | Postgres"},
                   {"if": "round 2 of 2", "reply": "Rebuttal."},
                   {"if": "round 1 of 2", "reply": "Opening."})
        c = self.team()
        srv.start_debate(c, "SQLite or Postgres?")
        self.assertEqual(c["debate"]["stances"], {"claude-1": "For", "codex": "Against", "claude-2": "Neutral"})
        self.assertEqual(c["debate"]["judge"], "claude-2")  # the Critic
        srv.worker(c)
        roles = [(m["who"], m["meta"]["role"]) for m in self.replies(c)]
        self.assertEqual(sorted(roles[:3]), sorted([("claude-1", "⚖ For"), ("codex", "⚖ Against"),
                                                    ("claude-2", "⚖ Neutral")]))
        self.assertEqual(sorted(roles[3:6]), sorted(roles[:3]))
        self.assertEqual(roles[6], ("claude-2", "⚖ Verdict"))
        self.assertEqual(self.replies(c)[-1]["meta"]["choices"], ["SQLite", "Postgres"])
        self.assertIsNone(c["debate"])
        rebut = [prompt(x) for x in self.calls() if "round 2 of 2" in prompt(x)]
        self.assertEqual(len(rebut), 3)
        for p in rebut:  # each one answers the other two openings
            self.assertEqual(p.count(": Opening."), 2, p)

    def test_everyone_is_capped_at_read(self):
        seen = []
        real = srv.run_agent

        def spy(chat, bot, prompt_, level, **kw):
            seen.append(level)
            return real(chat, bot, prompt_, level, **kw)
        c = self.chat(agents=[a("claude", "lead"), a("codex")])
        srv.run_agent = spy
        try:
            srv.start_debate(c, "Tabs or spaces?")
            srv.worker(c)
        finally:
            srv.run_agent = real
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(lv in ("read", "talk") for lv in seen), seen)

    def test_checks_and_no_retry(self):
        solo = self.chat(agents=["claude"])
        self.assertIn("2 or more agents", srv.debate_check(solo))
        with self.assertRaises(ValueError):
            srv.start_debate(solo, "x")
        self.rules({"style": "codex", "exit": 3})
        c = self.chat()
        srv.start_debate(c, "Is it worth it?")
        srv.worker(c)
        failed = next(i for i, m in enumerate(c["messages"]) if m["who"] == "codex")
        self.assertIn("new ⚖ Debate", srv.retry_block(c, failed))

    def test_stop_ends_it(self):
        c = self.team()
        srv.start_debate(c, "x")
        srv.halted.add(c["id"])
        srv.worker(c)
        self.assertIsNone(c["debate"])
        self.assertEqual(self.calls(), [])

    def test_debate_message_is_not_answered_directly(self):
        c = self.team()
        srv.start_debate(c, "x")
        self.assertEqual(srv.route(c, c["messages"][-1]), ([], False))



class CodexRound1Fixes(RaceTest):
    def test_copy_loose_ends_with_choices_after_them(self):
        self.rules({"style": "codex", "if": "(⚡ Race: you and", "write": {"app.py": "codex\n"},
                    "reply": "Done.\nLOOSE ENDS:\n- no docs\nCHOICES: Ship | Polish"}, *RACE_RULES)
        c, r = self.race()
        self.assertEqual(r["copies"]["codex"]["loose_ends"], ["no docs"])
        text = c["messages"][r["copies"]["codex"]["msg"]]["text"]
        self.assertEqual(text, "Done.\n\nCHOICES: Ship | Polish")

    def test_handoff_limit_counts_each_handoff(self):
        self.rules({"if": "You are Claude 1", "reply": "@claude-2 @codex please both look."},
                   {"if": "You are Claude 2", "reply": "Codex, your turn."},
                   {"style": "codex", "reply": "Claude 2, your turn."})
        c = self.chat(agents=["claude", "claude", "codex"])
        self.send(c, "@claude-1 go")
        agents = [m["who"] for m in self.replies(c) if m["who"] != "system"]
        self.assertEqual(len(agents), 1 + srv.MAX_HANDOFFS)
        self.assertIn("Paused after 3 hand-offs", c["messages"][-1]["text"])

    def test_no_debate_while_a_split_waits(self):
        c = self.chat(agents=["claude", "codex"])
        with unittest.mock.patch.object(srv, "split_waiting", return_value=True):
            self.assertIn("wait for your OK", srv.debate_check(c))

    def test_a_message_during_a_debate_is_answered_on_its_own(self):
        self.rules({"style": "claude", "if": "side question", "reply": "Side answer."},
                   {"if": "verdict", "reply": "Verdict."}, {"if": "round", "reply": "Point."})
        c = self.chat(agents=["claude", "codex"])
        srv.start_debate(c, "Big question?")
        srv.post_user_message(c, "@claude side question", None, reply_to="claude")
        srv.worker(c)
        self.assertIn("Side answer.", [m["text"] for m in self.replies(c, "claude")])
        for call in self.calls("codex"):  # the debate never took it in
            self.assertNotIn("side question", prompt(call))

    def test_diff_of_a_file_without_final_newline(self):
        before = srv.snapshot(self.folder)
        with open(os.path.join(self.folder, "app.py"), "w") as f:
            f.write("changed")
        with open(os.path.join(self.folder, "app.py"), "rb") as f:
            self.assertFalse(f.read().endswith(b"\n"))
        diff = srv.make_diff(before, ["app.py"], self.folder)
        self.assertIn("-orig\n+changed\n\\ No newline at end of file\n", diff)


class StopKeepsAWaitingSplit(ChatTest):
    def test_only_a_running_race_or_split_stops(self):
        for stage, after in (("await", "await"), ("parts", "stopped"), ("done", "done")):
            with self.subTest(stage=stage):
                c = self.chat()
                c["race"] = {"kind": "split", "stage": stage, "copies": {}, "msg": 0}
                srv.halted.add(c["id"])
                srv.worker(c)
                self.assertEqual(c["race"]["stage"], after)
                c["race"] = None
                srv.halted.discard(c["id"])


    def test_go_ahead_after_stopping_something_else(self):
        c = self.chat()
        c["race"] = {"kind": "split", "stage": "await", "copies": {}, "msg": 0, "racers": [],
                     "parts": {}}
        srv.add_msg(c, "user", "split it")
        srv.halted.add(c["id"])  # Stop pressed on an unrelated reply meanwhile
        self.assertTrue(srv.split_go(c))
        self.assertNotIn(c["id"], srv.halted)
        c["race"] = None
        c["flow"] = {"msg": 0, "stages": ["plan", "await", "work"], "i": 1, "fix": 0, "waiting": True}
        srv.halted.add(c["id"])
        self.assertTrue(srv.go_ahead(c))
        self.assertNotIn(c["id"], srv.halted)
        c["flow"] = None


    def test_stop_keeps_a_waiting_plan(self):
        for waiting, kept in ((True, True), (False, False)):
            with self.subTest(waiting=waiting):
                c = self.chat()
                srv.add_msg(c, "user", "plan it")
                c["flow"] = {"msg": 0, "stages": ["plan", "await", "work"], "i": 1, "fix": 0,
                             "waiting": waiting}
                srv.halted.add(c["id"])
                srv.worker(c)
                self.assertEqual(c["flow"] is not None, kept)
                srv.halted.discard(c["id"])
                c["flow"] = None



class StopWhilePlanWaits(ChatTest):
    def run_case(self, side_to):
        self.rules({"if": "Workflow · Planner", "reply": "1. Do the thing."},
                   {"if": "Workflow · Lead", "reply": "Built it."},
                   {"if": "Workflow · Reviewer", "reply": "Fine.\nVERDICT: LGTM"},
                   {"if": "quick question", "reply": "Answer."})
        c = self.chat(agents=[a("claude", "planner"), a("claude", "lead"), a("codex", "reviewer")])
        self.send(c, "build the thing")
        self.assertTrue(c["flow"]["waiting"])
        srv.post_user_message(c, f"@{side_to} quick question")
        side = len(c["messages"]) - 1
        srv.halted.add(c["id"])  # you pressed Stop before / while it was answered
        srv.worker(c)
        self.assertTrue(c["flow"]["waiting"])
        self.reset_calls()
        self.assertTrue(srv.go_ahead(c))
        srv.worker(c)
        lead = [x for x in self.calls("claude") if "Workflow · Lead" in prompt(x)]
        self.assertEqual(len(lead), 1)
        self.assertIn("build the thing", prompt(lead[0]))
        self.assertIn("1. Do the thing.", prompt(lead[0]))
        self.assertEqual({b: c["messages"][0]["status"][b] for b in ("claude-2", "codex")},
                         {"claude-2": "done", "codex": "done"})
        self.assertEqual(c["messages"][side]["status"][side_to], "stopped")  # never answered later
        self.assertFalse([x for x in self.calls() if "quick question" in prompt(x)
                          and "Workflow" not in prompt(x)])

    def test_side_question_to_the_lead(self):
        self.run_case("claude-2")

    def test_side_question_to_a_checker(self):
        self.run_case("codex")

    def test_side_question_to_the_planner(self):
        self.run_case("claude-1")

    def test_a_stopped_skill_doesnt_run_later(self):
        c = self.chat(agents=["claude", "codex"])
        srv.add_msg(c, "user", "/probe do it", to=["claude"], status={"claude": "stopped"},
                    skill={"name": "probe", "args": "do it", "adapters": ["claude"]})
        p = srv.build_prompt(c, "claude", c["messages"][-1:], "\n\n(Workflow · Lead: go)")
        self.assertFalse(p.startswith("/probe"), p)
        self.assertIn("Workflow · Lead", p)


if __name__ == "__main__":
    unittest.main()
