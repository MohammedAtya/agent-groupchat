"""Roles, permission levels and the workflow: Planner -> your OK -> Lead -> Tester -> Reviewer."""
import json
import os
import shutil
import unittest

from tests.helpers import ChatTest, srv
from tests.test_multi_agent import caller

LGTM = "Looks good.\nVERDICT: LGTM"
FIX = "The empty-input case crashes.\nVERDICT: FIX"


def a(adapter, *roles, **kw):
    return {"adapter": adapter, "roles": list(roles), **kw}


class Levels(ChatTest):
    def test_role_defaults(self):
        c = self.chat(agents=[a("claude", "planner"), a("claude", "lead"), a("claude", "tester"),
                              a("claude", "reviewer"), a("claude", "critic"), a("claude")])
        self.assertEqual([srv.level_of(c, b) for b in srv.agent_ids(c)],
                         ["read", "work", "work", "read", "read", "work"])

    def test_two_roles_get_the_higher_default(self):
        c = self.chat(agents=[a("claude", "planner", "lead"), "codex"])
        self.assertEqual(srv.level_of(c, "claude"), "work")

    def test_override_up_is_flagged_raised(self):
        c = self.chat(agents=[a("claude", "planner", level="work"), a("codex", "lead", level="read")])
        up, down = srv.level_info(c, "claude"), srv.level_info(c, "codex")
        self.assertEqual((up["level"], up["raised"]), ("work", True))
        self.assertEqual((down["level"], down["raised"]), ("read", False))

    def test_lock_drops_everyone_to_talk(self):
        c = self.chat(agents=[a("claude", "lead", level="auto"), "codex"], lock=True)
        self.assertEqual({b: srv.level_of(c, b) for b in srv.agent_ids(c)},
                         {"claude": "talk", "codex": "talk"})

    def test_without_sandbox_levels_drop_and_say_why(self):
        self.sandbox = False
        c = self.chat(agents=[a("claude", "lead"), a("codex", "lead")])
        cl, cx = srv.level_info(c, "claude"), srv.level_info(c, "codex")
        self.assertEqual((cl["level"], cx["level"]), ("edit", "read"))
        self.assertIn("sandbox", cl["limited"])
        self.assertIn("sandbox", cx["limited"])

    def test_unenforceable_choice_is_greyed_with_reason(self):
        c = self.chat()
        self.assertEqual(srv.level_info(c, "claude")["choices"]["edit"], "")
        self.assertIn("edit files but run no commands", srv.level_info(c, "codex")["choices"]["edit"])

    def test_same_agent_cant_review_its_own_work(self):
        with self.assertRaises(ValueError) as e:
            srv.parse_agents([a("claude", "lead", "reviewer")])
        self.assertIn("review its own work", str(e.exception))

    def test_unknown_role(self):
        with self.assertRaises(ValueError):
            srv.parse_agents([a("claude", "boss")])

    def test_custom_role_from_config(self):
        with open(srv.CONFIG_FILE, "w") as f:
            json.dump({"roles": [{"name": "Translator", "job": "Translate UI text to Turkish.",
                                  "level": "edit"}, {"name": "Lead"}, "junk"]}, f)
        try:
            c = self.chat(agents=[a("claude", "translator"), "codex"])
            self.assertEqual(srv.level_of(c, "claude"), "edit")
            self.assertIn("Translate UI text to Turkish.", srv.intro("claude", c))
            self.assertNotIn("lead-2", srv.roles_now())
        finally:
            os.remove(srv.CONFIG_FILE)


class Flow(ChatTest):
    def order(self):
        return [caller(r) or r["style"] for r in self.calls()]

    def test_lead_then_reviewer(self):
        self.rules({"style": "codex", "reply": LGTM})
        c = self.chat(agents=[a("claude", "lead"), a("codex", "reviewer")])
        self.send(c, "build it")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude", "codex"])
        self.assertIn("(Workflow · Lead: do the work", self.calls("claude")[0]["argv"][-1])
        review = self.calls("codex")[0]["argv"][-1]
        self.assertIn("(Workflow · Reviewer: Claude just did the work above.", review)
        self.assertIn("VERDICT: LGTM", review)
        r = self.replies(c, "codex")[0]["meta"]
        self.assertEqual((r["role"], r["stage"], r["verdict"]), ("Reviewer", "review", "LGTM"))
        self.assertEqual(c["messages"][0]["to"], ["claude", "codex"])
        self.assertIsNone(c["flow"])

    def test_reviewer_runs_read_only(self):
        c = self.chat(agents=[a("codex", "lead"), a("claude", "reviewer")])
        self.send(c, "build it")
        argv = self.calls("claude")[0]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("Bash(git diff:*)", argv)
        self.assertNotIn("Write", argv[argv.index("--allowedTools"):argv.index("--disallowedTools")])

    def test_review_lists_changed_files(self):
        self.rules({"style": "claude", "write": {"app.py": "print(1)\n"}})
        c = self.chat(agents=[a("claude", "lead"), a("codex", "reviewer")])
        self.send(c, "build it")
        self.assertIn("(files changed: app.py)", self.calls("codex")[0]["argv"][-1])

    def test_reviewer_that_edits_is_flagged(self):
        self.rules({"style": "claude", "write": {"sneaky.txt": "x\n"}})
        c = self.chat(agents=[a("codex", "lead"), a("claude", "reviewer")])
        self.send(c, "build it")
        self.assertIn("changed files although its permission is Read",
                      self.replies(c, "claude")[0]["meta"]["warn"])

    def test_fix_round_after_fix_verdict(self):
        self.rules({"style": "codex", "reply": FIX})
        c = self.chat(agents=[a("claude", "lead"), a("codex", "reviewer")], fix_rounds=1)
        self.send(c, "build it")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude", "codex", "claude", "codex"])
        self.assertIn("fix round 1 of 1", self.calls("claude")[1]["argv"][-1])
        self.assertIsNone(c["flow"])  # still FIX after the last allowed round: back to the user

    def test_no_fix_round_when_switched_off_or_lgtm(self):
        for fix_rounds, reply in ((0, FIX), (2, LGTM), (2, "No verdict line at all")):
            with self.subTest(fix_rounds=fix_rounds, reply=reply):
                self.reset_calls()
                self.rules({"style": "codex", "reply": reply})
                c = self.chat(agents=[a("claude", "lead"), a("codex", "reviewer")],
                              fix_rounds=fix_rounds)
                self.send(c, "build it")
                self.assertEqual(len(self.calls()), 2)

    def test_full_order_with_tester_and_reviewer(self):
        self.rules({"if": "Workflow · Tester", "reply": "All green.\nVERDICT: LGTM"},
                   {"if": "Workflow · Reviewer", "reply": LGTM})
        c = self.chat(agents=[a("claude", "reviewer"), a("claude", "tester"), a("codex", "lead")])
        self.send(c, "build it")
        self.assertEqual(self.order(), ["Codex", "Claude 2", "Claude 1"])

    def test_tester_changing_code_is_flagged(self):
        self.rules({"if": "Workflow · Tester", "write": {"app.py": "x\n", "tests/test_app.py": "y\n"},
                    "reply": "VERDICT: LGTM"})
        c = self.chat(agents=[a("codex", "lead"), a("claude", "tester")])
        self.send(c, "build it")
        self.assertEqual(self.replies(c, "claude")[0]["meta"]["warn"],
                         "the Tester changed non-test files: app.py")

    def test_one_agent_lead_and_tester_is_asked_twice(self):
        c = self.chat(agents=[a("claude", "lead", "tester"), "codex"])
        self.send(c, "build it")
        prompts = [r["argv"][-1] for r in self.calls("claude")]
        self.assertEqual(len(prompts), 2)
        self.assertIn("Workflow · Lead", prompts[0])
        self.assertTrue(prompts[1].startswith("(Workflow · Tester"), prompts[1])

    def test_participant_answers_directly_then_the_flow_runs(self):
        c = self.chat(agents=[a("claude", "lead"), "codex"])
        self.send(c, "build it")
        self.assertEqual(self.order(), ["Codex", "Claude"])
        self.assertNotIn("Workflow", self.calls("codex")[0]["argv"][-1])

    def test_mention_bypasses_the_flow(self):
        c = self.chat(agents=[a("claude", "lead"), a("codex", "reviewer")])
        self.send(c, "@codex quick question")
        self.assertEqual([m["who"] for m in self.replies(c)], ["codex"])
        self.assertIsNone(c["flow"])


class PlanPause(ChatTest):
    def make(self):
        return self.chat(agents=[a("claude", "planner"), a("claude", "lead"), a("codex", "reviewer")])

    def test_waits_for_ok_after_the_plan(self):
        c = self.make()
        self.send(c, "build it")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude-1"])
        self.assertIn("(Workflow · Planner: propose", self.calls()[0]["argv"][-1])
        self.assertTrue(c["flow"]["waiting"])
        self.assertEqual(c["messages"][0]["status"],
                         {"claude-1": "done", "claude-2": "waiting", "codex": "waiting"})
        self.assertTrue(srv.go_ahead(c))
        srv.worker(c)
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude-1", "claude-2", "codex"])
        self.assertIn("following the plan above", self.calls()[1]["argv"][-1])
        self.assertEqual(c["messages"][0]["status"],
                         {"claude-1": "done", "claude-2": "done", "codex": "done"})
        self.assertIsNone(c["flow"])
        self.assertFalse(srv.go_ahead(c))

    def test_reply_while_waiting_goes_back_to_the_planner(self):
        c = self.make()
        self.send(c, "build it")
        self.send(c, "use sqlite, not postgres")
        self.assertEqual([caller(r) for r in self.calls()], ["Claude 1", "Claude 1"])  # planner only
        self.assertTrue(c["flow"]["waiting"])
        self.assertEqual(c["messages"][2]["to"], ["claude-1"])

    def test_mention_while_waiting_keeps_waiting(self):
        c = self.make()
        self.send(c, "build it")
        self.send(c, "@codex what do you think?")
        self.assertEqual(self.replies(c)[-1]["who"], "codex")
        self.assertTrue(c["flow"]["waiting"])

    def test_critic_alone_does_not_pause(self):
        c = self.chat(agents=[a("claude", "critic"), a("codex", "lead")])
        self.send(c, "build it")
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude", "codex"])
        self.assertIn("(Workflow · Critic:", self.calls("claude")[0]["argv"][-1])

    def test_new_flow_replaces_one_that_ran(self):
        c = self.chat(agents=[a("claude", "lead"), a("codex", "reviewer")])
        self.send(c, "one")
        self.send(c, "two")
        self.assertEqual(len(self.calls()), 4)


class EditLock(ChatTest):
    def test_editors_take_turns_readers_dont_wait(self):
        times = self.folder + ".times"
        os.environ["FAKE_TIMES"] = times
        self.rules({"sleep": 0.4})
        try:
            c = self.chat(agents=[a("claude", level="work"), a("claude", level="work"),
                                  a("claude", level="read")])
            self.send(c, "hello")
            runs = {}
            for name in os.listdir(times):
                with open(os.path.join(times, name), encoding="utf-8") as f:
                    r = json.load(f)
                runs[caller(r)] = r
        finally:
            os.environ.pop("FAKE_TIMES")
            shutil.rmtree(times, ignore_errors=True)

        def overlap(x, y):
            return runs[x]["start"] < runs[y]["end"] and runs[y]["start"] < runs[x]["end"]
        self.assertFalse(overlap("Claude 1", "Claude 2"))
        self.assertTrue(overlap("Claude 3", "Claude 1") or overlap("Claude 3", "Claude 2"))


class Changes(ChatTest):
    def test_agent_is_told_when_its_role_changes(self):
        c = self.chat()
        self.send(c, "@codex hi")
        srv.set_agents(c, [{"id": "codex", "roles": ["reviewer"]}])
        self.send(c, "@codex again")
        prompt = self.calls("codex")[1]["argv"][-1]
        self.assertTrue(prompt.startswith("[System]: Your role, focus or permissions changed. "
                                          "Role: Reviewer. READ:"), prompt)
        self.send(c, "@codex third")
        self.assertNotIn("[System]", self.calls("codex")[2]["argv"][-1])


class Retry(ChatTest):
    def test_retry_failed_lead_continues_the_flow(self):
        self.rules({"style": "claude", "exit": 1})
        c = self.chat(agents=[a("claude", "lead"), a("codex", "reviewer")])
        self.send(c, "build it")
        self.assertEqual(c["messages"][0]["status"]["codex"], "skipped")
        self.rules()
        idx = next(i for i, m in enumerate(c["messages"]) if m["who"] == "claude")
        srv.worker(c, lambda: srv.retry(c, idx))
        self.assertEqual([m["who"] for m in self.replies(c)], ["claude", "claude", "codex"])


class OldChats(ChatTest):
    def test_mode_and_lead_become_roles_and_levels(self):
        cid = "oldchat00002"
        with open(srv.chat_path(cid), "w") as f:
            json.dump({"id": cid, "title": "old", "folder": self.folder, "mode": "talk",
                       "lead": "codex", "rounds": 0, "created": 0, "updated": 0, "messages": [],
                       "seen": {"claude": 0, "codex": 0}, "sessions": {"claude": None, "codex": None}}, f)
        srv.load_all()
        c = srv.chats.pop(cid)
        os.remove(srv.chat_path(cid))
        self.assertEqual([x["roles"] for x in c["agents"]], [["reviewer"], ["lead"]])
        self.assertEqual({b: srv.level_of(c, b) for b in srv.agent_ids(c)},
                         {"claude": "read", "codex": "read"})
        self.assertNotIn("mode", c)


if __name__ == "__main__":
    unittest.main()
