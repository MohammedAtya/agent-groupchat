"""Several agents per role, each with its own focus (SPEC §4.4), and the overlap warnings."""
import json

from tests.helpers import ChatTest, srv
from tests.test_http import HttpBase


def a(adapter, *roles, focus=None, **kw):
    return {"adapter": adapter, "roles": list(roles), **({"focus": focus} if focus else {}), **kw}


def system_prompt(call):
    argv = call["argv"]
    return argv[argv.index("--append-system-prompt") + 1]


def prompt(call):
    return call["stdin"] or call["argv"][-1]


class FocusField(ChatTest):
    def test_parsed_cleaned_and_kept(self):
        out = srv.parse_agents([a("claude", "tester", focus="  the   login\npage "), a("codex", "tester")])
        self.assertEqual(out[0]["focus"], "the login page")
        self.assertNotIn("focus", out[1])  # empty: not stored in saved teams
        c = self.chat(agents=[a("claude", "tester", focus="login"), a("codex", "tester")])
        self.assertEqual([x["focus"] for x in c["agents"]], ["login", None])

    def test_bad_focus_is_rejected(self):
        for bad in ("x" * (srv.FOCUS_MAX + 1), 42, ["login"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                srv.parse_agents([a("claude", "tester", focus=bad)])

    def test_set_agents_changes_the_focus_and_keeps_it_if_not_sent(self):
        c = self.chat(agents=[a("claude", "tester"), a("codex", "tester")])
        srv.set_agents(c, [{"id": "claude", "roles": ["tester"], "focus": "login"}])
        self.assertEqual(srv.agent(c, "claude")["focus"], "login")
        srv.set_agents(c, [{"id": "claude", "roles": ["tester"]}])  # an older page: no focus key
        self.assertEqual(srv.agent(c, "claude")["focus"], "login")
        srv.set_agents(c, [{"id": "claude", "roles": ["tester"], "focus": ""}])
        self.assertIsNone(srv.agent(c, "claude")["focus"])

    def test_old_chat_gets_an_empty_focus(self):
        c = self.chat()
        for x in c["agents"]:
            x.pop("focus")
        srv.migrate(c)
        self.assertEqual([x["focus"] for x in c["agents"]], [None, None])


class FocusPrompts(ChatTest):
    def team(self):
        return self.chat(agents=[a("codex", "lead"), a("claude", "tester", focus="login"),
                                 a("claude", "tester", focus="database")])

    def test_agent_is_told_its_focus_and_its_colleagues(self):
        c = self.team()
        text = srv.intro("claude-1", c)
        self.assertIn("Your focus: login.", text)
        self.assertIn("You share your role with Tester: Claude 2 (focus: database).", text)
        self.assertIn("Claude 2 (Anthropic's Claude Code; a separate session of the same AI as you; "
                      "role: Tester; focus: database)", text)

    def test_agent_without_focus_is_told_to_build_on_the_others(self):
        c = self.chat(agents=[a("claude", "lead"), a("claude", "lead", focus="frontend")])
        text = srv.permission_text(c, "claude-1")
        self.assertNotIn("Your focus", text)
        self.assertIn("Lead: Claude 2 (focus: frontend)", text)
        self.assertIn("don't redo it", text)

    def test_no_focus_text_for_a_role_nobody_shares(self):
        c = self.chat(agents=[a("claude", "lead"), a("codex", "reviewer")])
        self.assertEqual(srv.focus_text(c, "claude"), "")

    def test_in_a_race_copy_the_task_wins_over_the_focus(self):
        c = self.team()
        text = srv.intro("claude-1", c, folder=self.folder + "-copy")
        self.assertIn("Your usual focus is login, but here you do the task you're given.", text)
        self.assertNotIn("Your focus: login.", text)

    def test_stage_notes_carry_the_focus(self):
        self.rules({"if": "Workflow · Tester", "reply": "ok\nVERDICT: LGTM"})
        c = self.team()
        self.send(c, "build the sign-up")
        testers = {system_prompt(r)[:16]: prompt(r) for r in self.calls("claude")}
        self.assertIn("Your focus: login. Check that area only.", testers["You are Claude 1"])
        self.assertIn("Your focus: database. Check that area only.", testers["You are Claude 2"])

    def test_two_leads_take_turns_and_the_second_sees_the_first(self):
        self.rules({"if": "You are Claude 1", "reply": "LEAD-ONE finished the backend"},
                   {"if": "You are Claude 2", "reply": "LEAD-TWO finished the frontend"})
        c = self.chat(agents=[a("claude", "lead", focus="backend"),
                              a("claude", "lead", focus="frontend"), "codex"])
        self.send(c, "build the app")
        leads = [r for r in self.calls("claude")]
        self.assertEqual(len(leads), 2)
        first, second = prompt(leads[0]), prompt(leads[1])
        self.assertIn("The Leads (Claude 1 and Claude 2) take turns", first)
        mine = "LEAD-ONE" if "You are Claude 1" in system_prompt(leads[0]) else "LEAD-TWO"
        self.assertNotIn("finished the", first)
        self.assertIn(f"{mine} finished", second)  # read after the first Lead's turn
        self.assertIn(", your part only: backend" if mine == "LEAD-TWO" else
                      ", your part only: frontend", second)

    def test_focus_change_is_announced(self):
        c = self.chat(agents=[a("claude", "tester"), a("codex", "tester")])
        self.send(c, "@codex hi")
        srv.set_agents(c, [{"id": "codex", "roles": ["tester"], "focus": "database"}])
        self.send(c, "@codex again")
        text = prompt(self.calls("codex")[1])
        self.assertTrue(text.startswith("[System]: Your role, focus or permissions changed."), text)
        self.assertIn("Your focus: database.", text)

    def test_verdict_asks_whose_area_with_several_focused_leads(self):
        self.rules({"if": "Workflow · Reviewer", "reply": "VERDICT: LGTM"})
        c = self.chat(agents=[a("claude", "lead", focus="api"), a("claude", "lead", focus="ui"),
                              a("codex", "reviewer")])
        self.send(c, "build it")
        self.assertIn("Say whose area each problem is in", prompt(self.calls("codex")[0]))


class Overlaps(ChatTest):
    def find(self, *agents):
        return srv.overlaps(srv.make_agents(list(agents)))

    def test_different_focuses_are_fine(self):
        self.assertEqual(self.find(a("claude", "tester", focus="login"),
                                   a("codex", "tester", focus="database")), [])

    def test_no_shared_role_is_fine(self):
        self.assertEqual(self.find(a("claude", "lead"), a("codex", "tester")), [])

    def test_two_leads_without_focus(self):
        [o] = self.find(a("claude", "lead"), a("codex", "lead"))
        self.assertEqual((o["role"], o["kind"], o["edits"], o["agents"]),
                         ("lead", "none", True, ["claude", "codex"]))
        self.assertIn("neither has a focus", o["text"])
        self.assertIn("⚡ Race", o["text"])

    def test_one_without_focus_covers_the_other(self):
        [o] = self.find(a("claude", "tester", focus="login"), a("codex", "tester"))
        self.assertEqual(o["kind"], "open")
        self.assertIn("Codex has no focus, so it also covers Claude's (login)", o["text"])

    def test_same_focus_in_other_words(self):
        for fx, fy in (("login", "Login"), ("login", "testing the login"),
                       ("logins", "login"), ("login page", "login")):
            with self.subTest(fx=fx, fy=fy):
                [o] = self.find(a("claude", "tester", focus=fx), a("codex", "tester", focus=fy))
                self.assertEqual((o["kind"], o["edits"]), ("same", True))
                self.assertIn("same work twice", o["text"])

    def test_partly_different_focuses_are_fine(self):
        self.assertEqual(self.find(a("claude", "lead", focus="login api"),
                                   a("codex", "lead", focus="login page")), [])

    def test_reviewers_and_critics_get_a_soft_note(self):
        [o] = self.find(a("claude", "reviewer"), a("codex", "reviewer"))
        self.assertFalse(o["edits"])
        self.assertIn("two opinions", o["text"])

    def test_participants_never_overlap(self):
        self.assertEqual(self.find(a("claude"), a("codex"), a("claude")), [])

    def test_three_leads_give_every_pair(self):
        out = self.find(a("claude", "lead"), a("claude", "lead"), a("codex", "lead", focus="ui"))
        self.assertEqual([o["agents"] for o in out],
                         [["claude-1", "claude-2"], ["claude-1", "codex"], ["claude-2", "codex"]])

    def test_specs_from_the_page_tolerate_junk(self):
        out = srv.overlaps_for_specs([a("claude", "lead"), a("claude", "lead"), {"adapter": "nope"},
                                      "x", a("codex", "lead", focus=123)])
        self.assertEqual(len(out), 3)  # every pair of the three valid Leads (bad focus = none)


class OverlapsApi(HttpBase):
    def test_post_overlaps(self):
        code, body = self.req("POST", "/api/overlaps",
                              {"agents": [a("claude", "lead", name="Builder"), a("codex", "lead")]})
        self.assertEqual(code, 200)
        [o] = json.loads(body)["overlaps"]
        self.assertEqual(o["agents"], ["builder", "codex"])
        self.assertTrue(o["text"].startswith("Builder and Codex are both Lead"))

    def test_bad_body(self):
        code, _ = self.req("POST", "/api/overlaps", {"agents": "nope"})
        self.assertEqual(code, 400)

    def test_chat_keeps_the_focus(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder, "agents": [
            a("claude", "tester", focus="login"), a("codex", "tester", focus="database")]})
        self.assertEqual(code, 200)
        cid = json.loads(body)["id"]
        code, body = self.req("GET", f"/api/chats/{cid}")
        self.assertEqual([x["focus"] for x in json.loads(body)["agents"]], ["login", "database"])


class CodexFindings(ChatTest):
    """Regressions for the issues Codex found when it tested this feature."""

    def test_late_read_stops_before_a_newer_user_message(self):
        c = self.chat(agents=[a("claude", "lead"), a("codex", "lead")])
        srv.add_msg(c, "user", "build it")
        known = len(c["messages"])
        srv.add_msg(c, "claude", "first Lead done")      # came while codex waited: include it
        srv.add_msg(c, "user", "@codex quick question")  # a new turn: leave it for later
        users, msgs = srv.unseen_for(c, "codex", known=known)
        self.assertEqual([m["text"] for m in msgs], ["build it", "first Lead done"])
        self.assertEqual(users, [0])
        self.assertEqual(c["seen"]["codex"], 2)
        self.assertEqual([m["text"] for m in srv.unseen_for(c, "codex")[1]], ["@codex quick question"])

    def test_retry_of_one_lead_among_several(self):
        c = self.chat(agents=[a("claude", "lead", focus="api"), a("claude", "lead", focus="ui")])
        self.send(c, "build it")
        self.reset_calls()
        f = {"stages": ["work"], "i": 0, "msg": 0, "fix": 0, "only": ["claude-2"]}
        srv.run_stage(c, f, "work")  # was KeyError('only') with 2+ agents in the stage
        self.assertEqual([system_prompt(r)[:16] for r in self.calls()], ["You are Claude 2"])

    def test_second_lead_is_told_the_files_the_first_changed(self):
        self.rules({"if": "You are Claude 1", "write": {"one.py": "1\n"}, "reply": "did one"},
                   {"if": "You are Claude 2", "write": {"two.py": "2\n"}, "reply": "did two"})
        c = self.chat(agents=[a("claude", "lead", focus="api"), a("claude", "lead", focus="ui")])
        self.send(c, "build it")
        first, second = self.calls()
        theirs = "one.py" if "You are Claude 1" in system_prompt(first) else "two.py"
        self.assertNotIn("files they changed", prompt(first))
        self.assertIn(f"(files they changed: {theirs})", prompt(second))

    def test_lone_lead_fix_round_repeats_its_focus(self):
        f = {"stages": ["work", "review"], "msg": 0, "fix": 1}
        c = self.chat(agents=[a("claude", "lead", focus="api"), a("codex", "reviewer")])
        self.assertIn("Fix them (your focus: api)", srv.stage_note(c, "claude", "work", f))

    def test_clearing_a_focus_is_said_explicitly(self):
        c = self.chat(agents=[a("claude", "tester", focus="login"), a("codex")])
        self.send(c, "@claude hi")
        srv.set_agents(c, [{"id": "claude", "roles": ["tester"], "focus": ""}])
        self.send(c, "@claude again")
        self.assertIn("the whole task is yours again", prompt(self.calls("claude")[1]))

    def test_focus_words_edge_cases(self):
        same = lambda x, y: (lambda wx, wy: wx <= wy or wy <= wx)(srv.focus_words(x), srv.focus_words(y))
        self.assertFalse(same("billing", "💳"))
        self.assertTrue(same("💳", "💳"))
        self.assertTrue(same("dependency", "dependencies"))
        self.assertTrue(same("cookie", "cookies"))
        self.assertTrue(same("api keys", "API key"))
        self.assertFalse(same("cookies", "cooking"))
        self.assertTrue(same("the tests", "tests"))
        self.assertFalse(same("the tests", "login"))

    def test_first_leads_reply_after_a_new_user_message_is_still_shown(self):
        c = self.chat(agents=[a("claude", "lead"), a("codex", "lead")])
        srv.add_msg(c, "user", "build it")
        known = len(c["messages"])
        srv.add_msg(c, "user", "@claude quick question")  # came while codex waited
        srv.add_msg(c, "claude", "first Lead done", meta={"stage": "work"})
        srv.add_msg(c, "claude", "answer to the question")  # not part of the work stage
        users, msgs = srv.unseen_for(c, "codex", known=known, stage="work")
        self.assertEqual([m["text"] for m in msgs], ["build it", "first Lead done"])
        self.assertEqual((users, c["seen"]["codex"]), ([0], 1))  # the question is still unseen

    def test_clearing_one_of_two_focuses_is_said_explicitly(self):
        c = self.chat(agents=[a("claude", "tester", focus="login"), a("codex", "tester", focus="db")])
        self.send(c, "@claude hi")
        srv.set_agents(c, [{"id": "claude", "roles": ["tester"], "focus": ""}])
        self.send(c, "@claude again")
        self.assertIn("You no longer have a focus of your own.", prompt(self.calls("claude")[1]))

    def test_a_colleagues_focus_change_is_not_called_losing_yours(self):
        c = self.chat(agents=[a("claude", "tester"), a("codex", "tester", focus="db")])
        self.send(c, "@claude hi")
        srv.set_agents(c, [{"id": "codex", "roles": ["tester"], "focus": "api"}])
        self.send(c, "@claude again")
        text = prompt(self.calls("claude")[1])
        self.assertIn("Codex (focus: api)", text)
        self.assertNotIn("no longer have a focus", text)
