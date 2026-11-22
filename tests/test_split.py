"""🧩 Split (SPEC §14.4): parts built at the same time in copies, merged, then checked."""
import json
import os
import time
import unittest

from tests.helpers import srv
from tests.test_http import HttpBase
from tests.test_race import RaceTest

PLAN = "Two independent parts.\nPART @claude-2: build api.py\n- PART @codex: build page.html"
RULES = [
    {"if": "(🧩 Split · Planner", "reply": PLAN},
    {"if": "(🧩 Split · Tester", "write": {"tests/test_api.py": "ok\n"}, "reply": "2/2 pass\nVERDICT: LGTM"},
    {"if": "(🧩 Split · Reviewer", "reply": "The parts fit together.\nVERDICT: LGTM"},
    {"if": "(🧩 Split · merge", "write": {"app.py": "merged\n"}, "reply": "Fixed the clash."},
    {"if": "(🧩 Split · fix round", "write": {"api.py": "fixed api\n"}, "reply": "Fixed it."},
    {"if": "YOUR part: build api.py", "write": {"api.py": "api\n"}, "reply": "API done."},
    {"if": "YOUR part: build page.html", "write": {"page.html": "<form>\n"}, "reply": "Page done."},
    {"if": "(Workflow · Lead", "reply": "Did it normally."},
]
TEAM = [{"adapter": "claude", "roles": ["planner"]}, {"adapter": "claude", "roles": ["lead"]},
        {"adapter": "codex"}, {"adapter": "claude", "roles": ["tester"]},
        {"adapter": "claude", "roles": ["reviewer"]}]
OWN = {"claude-2": "build api.py", "codex": "build page.html"}


class SplitTest(RaceTest):
    def setUp(self):
        super().setUp()
        self.rules(*RULES)

    def split(self, parts=None, agents=TEAM, text="build login"):
        c = self.chat(agents=agents)
        srv.start_split(c, text, parts)
        srv.worker(c)
        return c, c["race"]

    def prompts(self, needle):
        return [x["argv"][-1] for x in self.calls() if needle in x["argv"][-1]]


class PlannerSplit(SplitTest):
    def test_plan_then_ok_then_parts_merge_and_checks(self):
        c, r = self.split()
        self.assertEqual((r["stage"], r["parts"]), ("await", {"claude-2": "build api.py",
                                                              "codex": "build page.html"}))
        st = c["messages"][r["msg"]]["status"]
        self.assertEqual((st["claude-1"], st["claude-2"], st["codex"]), ("done", "waiting", "waiting"))
        self.assertEqual(self.prompts("YOUR part"), [])  # nothing is built before the OK
        self.assertTrue(srv.split_go(c))
        srv.worker(c)
        self.assertEqual(r["stage"], "done")
        comb = r["copies"]["_combined"]
        self.assertEqual(sorted(comb["files"]), ["api.py", "page.html", "tests/test_api.py"])
        self.assertFalse(os.path.exists(os.path.join(self.folder, "api.py")))  # untouched until used
        self.assertEqual(comb["tests"], "2/2 pass")
        part = self.prompts("YOUR part: build api.py")[0]
        self.assertIn("The other parts (don't do them): Codex: build page.html", part)
        self.assertIn("(🧩 Split · Reviewer", self.prompts("(🧩 Split · Reviewer")[0])
        self.assertEqual(srv.race_action(c, {"action": "use", "copy": "_combined"})[0], 200)
        for f in ("api.py", "page.html", os.path.join("tests", "test_api.py")):
            self.assertTrue(os.path.exists(os.path.join(self.folder, f)), f)

    def test_reply_while_waiting_goes_back_to_the_planner(self):
        c, r = self.split()
        srv.split_feedback(c, "give the page to Claude 2 instead")
        srv.worker(c)
        self.assertEqual(len(self.prompts("(🧩 Split · Planner")), 2)
        self.assertEqual(r["stage"], "await")
        self.assertIn("[User]: give the page to Claude 2 instead", self.prompts("(🧩 Split · Planner")[1])

    def test_no_split_runs_normally(self):
        self.rules({"if": "(🧩 Split · Planner", "reply": "These steps depend on each other.\nNO SPLIT"},
                   *RULES[1:])
        c, r = self.split()
        self.assertIsNone(c["race"])
        self.assertEqual(c["messages"][0]["mode"], "no-split")
        self.assertTrue(any(m["who"] == "system" and "normal message" in m["text"] for m in c["messages"]))
        self.assertEqual(len(self.prompts("(Workflow · Lead")), 1)  # the Lead did it the normal way
        self.assertEqual(self.prompts("YOUR part"), [])


class OwnSplit(SplitTest):
    def test_own_parts_start_at_once(self):
        c, r = self.split(parts=OWN)
        self.assertEqual(self.prompts("(🧩 Split · Planner"), [])
        self.assertEqual(r["stage"], "done")
        self.assertEqual(sorted(r["copies"]), ["_combined", "claude-2", "codex"])
        self.assertIn("2/2 pass", r["copies"]["_combined"]["tests"])

    def test_validation(self):
        c = self.chat(agents=TEAM)
        for parts in ({"claude-2": "only one"}, {"claude-2": "x", "claude-4": "the reviewer can't build"},
                      {"claude-2": "x", "codex": "  "}, "not a dict"):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                srv.start_split(c, "go", parts)
        no_planner = self.chat(agents=TEAM[1:])
        self.assertIn("Planner", srv.split_check(no_planner, by_planner=True))
        self.assertEqual(srv.split_check(no_planner), "")
        with self.assertRaises(ValueError):
            srv.start_split(no_planner, "go")

    def test_clash_is_fixed_by_the_lead(self):
        self.rules({"if": "YOUR part: build api.py", "write": {"app.py": "A\n"}, "reply": "A"},
                   {"if": "YOUR part: build page.html", "write": {"app.py": "B\n"}, "reply": "B"},
                   *RULES)
        c, r = self.split(parts=OWN)
        merge = self.calls()
        fixer = next(x for x in merge if "(🧩 Split · merge" in x["argv"][-1])
        self.assertIn("conflict markers: app.py", fixer["argv"][-1])
        intro = fixer["argv"][fixer["argv"].index("--append-system-prompt") + 1]
        self.assertIn("You are Claude 2", intro)  # the Lead
        self.assertEqual(self.read(r["copies"]["_combined"]["folder"], "app.py"), "merged\n")
        self.assertEqual(r["stage"], "done")

    def test_fix_rounds_on_the_combined_result(self):
        self.rules({"if": "(🧩 Split · Reviewer", "reply": "api.py is wrong.\nVERDICT: FIX"}, *RULES)
        c = self.chat(agents=TEAM, fix_rounds=1)
        srv.start_split(c, "build login", OWN)
        srv.worker(c)
        r = c["race"]
        self.assertEqual(len(self.prompts("(🧩 Split · fix round 1 of 1")), 1)
        self.assertIn("api.py is wrong", self.prompts("(🧩 Split · fix round")[0])
        self.assertEqual(len(self.prompts("(🧩 Split · Reviewer")), 2)  # checked again after the fix
        self.assertEqual((r["stage"], r["fix"]), ("done", 1))
        self.assertEqual(self.read(r["copies"]["_combined"]["folder"], "api.py"), "fixed api\n")

    def test_tester_changing_code_is_flagged(self):
        self.rules({"if": "(🧩 Split · Tester", "write": {"api.py": "hacked\n"}, "reply": "1/1\nVERDICT: LGTM"},
                   *RULES)
        c, r = self.split(parts=OWN)
        warned = [m for m in c["messages"] if (m.get("meta") or {}).get("warn", "").startswith("the Tester changed non-test")]
        self.assertEqual(len(warned), 1)


class SplitHttp(HttpBase, SplitTest):
    def wait(self, cid, stage):
        for _ in range(200):
            chat = json.loads(self.req("GET", f"/api/chats/{cid}")[1])
            if chat["race"] and chat["race"]["stage"] == stage and not chat["busy"]:
                return chat
            time.sleep(0.05)
        self.fail(f"never reached {stage}")

    def test_planner_split_over_http(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder, "agents": TEAM})
        cid = json.loads(body)["id"]
        chat = json.loads(self.req("GET", f"/api/chats/{cid}")[1])
        self.assertEqual((chat["split_block"], chat["split_planner"]), ("", True))
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/send",
                                  {"text": "build login", "mode": "split", "split": "planner"})[0], 200)
        chat = self.wait(cid, "await")
        self.assertEqual(chat["race"]["parts"], {"claude-2": "build api.py", "codex": "build page.html"})
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/go", {})[0], 200)
        chat = self.wait(cid, "done")
        self.assertEqual(chat["race"]["kind"], "split")
        self.assertEqual(chat["race"]["copies"]["claude-2"]["part"], "build api.py")
        self.req("POST", f"/api/chats/{cid}/delete", {})

    def test_own_split_over_http(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder, "agents": TEAM})
        cid = json.loads(body)["id"]
        code, body = self.req("POST", f"/api/chats/{cid}/send", {"text": "build login", "mode": "split",
                                                                 "split": "own", "parts": {"claude-2": "x"}})
        self.assertEqual(code, 400)
        self.assertIn("at least 2", json.loads(body)["error"])
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/send", {"text": "build login", "mode": "split",
                                                                     "split": "own", "parts": OWN})[0], 200)
        self.wait(cid, "done")
        self.req("POST", f"/api/chats/{cid}/delete", {})


if __name__ == "__main__":
    unittest.main()
