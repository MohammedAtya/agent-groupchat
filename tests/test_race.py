"""⚡ Race (SPEC §14): agents do the same task at the same time, each in its own copy."""
import json
import os
import unittest

from tests.helpers import ChatTest, srv
from tests.test_http import HttpBase
from agent_groupchat.copies import IDENT, git

RULES = [
    {"if": "(⚡ Race · Tester", "reply": "3/3 pass\nAll tests are green."},
    {"if": "(⚡ Race · Reviewer", "reply": "Codex's version is cleaner.\nPICK: codex"},
    {"if": "(⚡ Race · Combine", "write": {"app.py": "combined\n"}, "reply": "Kept the best of both."},
    {"style": "claude", "if": "(⚡ Race: you and", "write": {"app.py": "claude\n"}, "reply": "Done (claude)."},
    {"style": "codex", "if": "(⚡ Race: you and", "write": {"app.py": "codex\n"}, "reply": "Done (codex)."},
]
TEAM = [{"adapter": "claude", "roles": ["lead"]}, {"adapter": "codex"},
        {"adapter": "claude", "roles": ["tester"]}, {"adapter": "claude", "roles": ["reviewer"]}]


class RaceTest(ChatTest):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.folder, "app.py"), "w") as f:
            f.write("orig\n")
        git(self.folder, "init", "-q")
        git(self.folder, "add", "-A")
        git(self.folder, *IDENT, "commit", "-q", "-m", "start")
        srv._git_cache.clear()
        self.rules(*RULES)

    def tearDown(self):
        for c in list(srv.chats.values()):
            if c["folder"] == self.folder:
                srv.discard_race(c)
        super().tearDown()

    def race(self, text="build it", agents=TEAM):
        c = self.chat(agents=agents)
        srv.start_race(c, text)
        srv.worker(c)
        return c, c["race"]

    def read(self, *parts):
        with open(os.path.join(*parts)) as f:
            return f.read()


class Check(RaceTest):
    def test_reasons(self):
        c = self.chat(agents=[{"adapter": "claude", "roles": ["lead"]}, {"adapter": "codex", "roles": ["reviewer"]}])
        self.assertIn("2 or more agents", srv.race_check(c))
        self.assertEqual(srv.racers(self.chat(agents=TEAM)), ["claude-1", "codex"])
        plain = self.folder + "-plain"
        os.makedirs(plain)
        c2 = srv.new_chat(plain, 0, "x", srv.parse_agents([{"adapter": "claude"}, {"adapter": "codex"}]))
        self.assertIn("isn't a git repo", srv.race_check(c2))
        with self.assertRaises(ValueError):
            srv.start_race(c2, "go")
        os.rmdir(plain)


class FullRace(RaceTest):
    def test_each_racer_works_in_its_own_copy(self):
        c, r = self.race()
        self.assertEqual(r["stage"], "done")
        self.assertEqual(self.read(self.folder, "app.py"), "orig\n")  # the user's folder is untouched
        cl, cx = r["copies"]["claude-1"], r["copies"]["codex"]
        self.assertEqual(self.read(cl["folder"], "app.py"), "claude\n")
        self.assertEqual(self.read(cx["folder"], "app.py"), "codex\n")
        self.assertTrue(cl["folder"].startswith(os.path.join(srv.DATA_DIR, "worktrees", c["id"])))
        self.assertEqual((cl["status"], cl["files"]), ("done", ["app.py"]))
        runs = {r_["cwd"] for r_ in self.calls() if "(⚡ Race: you and" in r_["argv"][-1]}
        self.assertEqual(runs, {os.path.realpath(cl["folder"]), os.path.realpath(cx["folder"])})

    def test_racers_get_the_task_and_the_chat_in_a_fresh_session(self):
        c = self.chat(agents=TEAM)
        self.send(c, "@claude-1 hello first")
        main_session = c["sessions"]["claude-1"]
        srv.start_race(c, "build it")
        srv.worker(c)
        call = next(x for x in self.calls("claude") if "(⚡ Race: you and" in x["argv"][-1])
        prompt = call["argv"][-1]
        self.assertIn("[User]: @claude-1 hello first", prompt)  # the chat so far
        self.assertIn("[User]: build it", prompt)
        self.assertIn("you and Codex got this same task", prompt)
        intro = call["argv"][call["argv"].index("--append-system-prompt") + 1]
        self.assertIn("your own copy of the user's project", intro)
        self.assertIn("--session-id", call["argv"])  # a new, one-off session
        self.assertEqual(c["sessions"]["claude-1"], main_session)  # the main one is kept

    def test_tester_runs_tests_in_every_copy_and_reviewer_picks(self):
        c, r = self.race()
        tests = [x for x in self.calls() if "(⚡ Race · Tester" in x["argv"][-1]]
        self.assertEqual(sorted(t["cwd"] for t in tests),
                         sorted(os.path.realpath(r["copies"][n]["folder"]) for n in ("claude-1", "codex")))
        self.assertEqual(r["copies"]["codex"]["tests"], "3/3 pass")
        review = next(x for x in self.calls() if "(⚡ Race · Reviewer" in x["argv"][-1])["argv"][-1]
        self.assertIn("+claude", review)
        self.assertIn("+codex", review)
        self.assertIn("tests: 3/3 pass", review)
        self.assertEqual(r["pick"], "codex")
        self.assertEqual(c["messages"][r["msg"]]["status"],
                         {"claude-1": "done", "codex": "done", "claude-2": "done", "claude-3": "done"})

    def test_tester_that_changes_a_copy_is_undone(self):
        self.rules({"if": "(⚡ Race · Tester", "write": {"app.py": "tester was here\n"}, "reply": "1/1 pass"},
                   *RULES[1:])
        c, r = self.race()
        self.assertEqual(self.read(r["copies"]["codex"]["folder"], "app.py"), "codex\n")
        warned = [m for m in c["messages"] if (m.get("meta") or {}).get("warn", "").startswith("the Tester changed")]
        self.assertEqual(len(warned), 2)

    def test_long_chats_get_one_shared_summary(self):
        old = srv.CONTEXT_LIMIT
        srv.CONTEXT_LIMIT = 10
        self.rules({"if": "(Write a summary of this group chat", "reply": "- the summary"}, *RULES)
        try:
            c = self.chat(agents=TEAM)
            self.send(c, "@codex some earlier talk that is long enough")
            srv.start_race(c, "build it")
            srv.worker(c)
        finally:
            srv.CONTEXT_LIMIT = old
        self.assertEqual(sum("(Write a summary" in x["argv"][-1] for x in self.calls()), 1)
        racing = [x["argv"][-1] for x in self.calls() if "(⚡ Race: you and" in x["argv"][-1]]
        self.assertEqual(len(racing), 2)
        for p in racing:
            self.assertIn("(Summary of the group chat so far:)\n- the summary", p)

    def test_agents_remember_their_race_work_afterwards(self):
        c, r = self.race()
        self.send(c, "@claude-1 what did you do?")
        self.assertIn("[Claude 1]: Done (claude).", self.calls("claude")[-1]["argv"][-1])


class Choose(RaceTest):
    def test_use_this_one(self):
        c, r = self.race()
        code, out = srv.race_action(c, {"action": "use", "copy": "codex"})
        self.assertEqual((code, out["files"]), (200, ["app.py"]))
        self.assertEqual(self.read(self.folder, "app.py"), "codex\n")
        self.assertEqual(git(self.folder, "diff", "--cached", "--name-only"), "")  # not staged
        self.assertEqual(r["used"], "codex")
        self.assertIn("You used Codex's copy", c["messages"][-1]["text"])

    def test_clash_with_the_users_own_change(self):
        c, r = self.race()
        with open(os.path.join(self.folder, "app.py"), "w") as f:
            f.write("mine\n")
        code, out = srv.race_action(c, {"action": "use", "copy": "codex"})
        self.assertEqual((code, out["clash"]), (409, ["app.py"]))
        self.assertEqual(self.read(self.folder, "app.py"), "mine\n")

    def test_combine(self):
        c, r = self.race()
        code, _ = srv.race_action(c, {"action": "combine", "copies": ["claude-1", "codex"]})
        self.assertEqual(code, 200)
        import time
        for _ in range(200):  # Combine runs in the chat's own background worker
            if c["id"] not in srv.workers and c["race"]["stage"] == "done":
                break
            time.sleep(0.05)
        comb = r["copies"]["_combined"]
        self.assertEqual((comb["agent"], comb["status"]), ("claude-1", "done"))  # the Lead fixes it
        self.assertEqual(self.read(comb["folder"], "app.py"), "combined\n")
        prompt = next(x for x in self.calls() if "(⚡ Race · Combine" in x["argv"][-1])["argv"][-1]
        self.assertIn("conflict markers: app.py", prompt)
        self.assertEqual(srv.race_action(c, {"action": "use", "copy": "_combined"})[0], 200)
        self.assertEqual(self.read(self.folder, "app.py"), "combined\n")

    def test_discard_and_delete_remove_the_copies(self):
        c, r = self.race()
        paths = [x["path"] for x in r["copies"].values()]
        self.assertEqual(srv.race_action(c, {"action": "discard"})[0], 200)
        self.assertIsNone(c["race"])
        for p in paths:
            self.assertFalse(os.path.exists(p))
        self.assertEqual(git(self.folder, "branch", "--list", "agent/*"), "")
        c2, r2 = self.race()
        srv.discard_race(c2)
        self.assertFalse(os.path.exists(os.path.join(srv.DATA_DIR, "worktrees", c2["id"])))

    def test_a_new_race_replaces_the_old_copies(self):
        c, r = self.race()
        old = r["copies"]["codex"]["path"]
        srv.start_race(c, "again")
        self.assertNotIn("codex", c["race"]["copies"])
        self.assertFalse(os.path.exists(old))
        srv.worker(c)
        self.assertEqual(c["race"]["stage"], "done")

    def test_bad_actions(self):
        c, r = self.race()
        for d in ({"action": "use", "copy": "nobody"}, {"action": "combine", "copies": ["codex"]},
                  {"action": "explode"}):
            with self.subTest(d=d):
                self.assertEqual(srv.race_action(c, d)[0], 400)


class RaceHttp(HttpBase, RaceTest):
    def test_send_race_and_view(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder, "agents": TEAM})
        cid = json.loads(body)["id"]
        self.assertEqual(json.loads(self.req("GET", f"/api/chats/{cid}")[1])["race_block"], "")
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/send", {"text": "build it", "mode": "race"})[0], 200)
        import time
        for _ in range(200):
            chat = json.loads(self.req("GET", f"/api/chats/{cid}")[1])
            if chat["race"] and chat["race"]["stage"] == "done" and not chat["busy"]:
                break
            time.sleep(0.05)
        self.assertEqual(chat["race"]["pick"], "codex")
        self.assertNotIn("path", chat["race"]["copies"]["codex"])  # no internals in the view
        code, diff = self.req("GET", f"/api/chats/{cid}/race/diff/codex")
        self.assertEqual(code, 200)
        self.assertIn(b"+codex", diff)
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/race", {"action": "use", "copy": "codex"})[0], 200)
        self.req("POST", f"/api/chats/{cid}/delete", {})


if __name__ == "__main__":
    unittest.main()
