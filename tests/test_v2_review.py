"""Regression tests for Codex's review of v2 (Race / Split), one per finding."""
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from tests.helpers import srv
from tests.test_http import HttpBase
from tests.test_copies import Repo, read, write
from tests.test_race import RULES as RACE_RULES, TEAM as RACE_TEAM, RaceTest
from tests.test_split import OWN, RULES as SPLIT_RULES, TEAM as SPLIT_TEAM
from agent_groupchat import copies as cp
from agent_groupchat.copies import IDENT, git

CAN_SYMLINK = os.name != "nt"


# ---------------------------------------------------------------- copies.py
class CopySafety(Repo):
    def test_git_env_variables_cant_redirect_the_copy(self):
        write(self.p("draft.txt"), "unfinished\n")
        before = (git(self.root, "rev-parse", "HEAD"), git(self.root, "status", "--porcelain"))
        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(self.root, ".git"),
                                          "GIT_WORK_TREE": self.root}):
            base, c = self.race("a")
            write(os.path.join(c["a"][0], "x.py"), "x\n")
            cp.save_copy(c["a"][0])
        self.assertEqual((git(self.root, "rev-parse", "HEAD"), git(self.root, "status", "--porcelain")), before)

    @unittest.skipUnless(CAN_SYMLINK, "needs POSIX hooks")
    def test_hooks_dont_run(self):
        marker = os.path.join(self.tmp, "hook-ran")
        hook = self.p(".git", "hooks", "post-checkout")
        write(hook, f"#!/bin/sh\necho ran > '{marker}'\n")
        os.chmod(hook, 0o755)
        self.race("a")
        self.assertFalse(os.path.exists(marker))

    @unittest.skipUnless(CAN_SYMLINK, "needs symlinks")
    def test_symlinks_pointing_outside_are_found(self):
        os.symlink(self.p("app.py"), self.p("abs-link"))
        os.symlink("../../elsewhere", self.p("up-link"))
        os.symlink("app.py", self.p("ok-link"))
        base = cp.snapshot(self.root)
        self.assertEqual(cp.unsafe_links(self.root, base), ["abs-link", "up-link"])

    def test_use_is_byte_exact(self):
        base, c = self.race("a")
        path, branch = c["a"]
        with open(os.path.join(path, "latin1.txt"), "wb") as f:
            f.write("caf\xe9\n".encode("latin-1"))
        with open(os.path.join(path, "crlf.txt"), "wb") as f:
            f.write(b"one\r\ntwo\r\n")
        cp.save_copy(path)
        ok, files, why = cp.use_copy(self.root, base, branch)
        self.assertTrue(ok, why)
        with open(self.p("latin1.txt"), "rb") as f:
            self.assertEqual(f.read(), "caf\xe9\n".encode("latin-1"))
        with open(self.p("crlf.txt"), "rb") as f:
            self.assertEqual(f.read(), b"one\r\ntwo\r\n")

    def test_existing_crlf_file_edited_in_the_copy(self):
        with open(self.p("win.txt"), "wb") as f:
            f.write(b"a\r\nb\r\n")
        git(self.root, "add", "-A")
        git(self.root, *IDENT, "commit", "-q", "-m", "crlf")
        base, c = self.race("a")
        with open(os.path.join(c["a"][0], "win.txt"), "wb") as f:
            f.write(b"a\r\nB\r\n")
        cp.save_copy(c["a"][0])
        ok, files, why = cp.use_copy(self.root, base, c["a"][1])
        self.assertTrue(ok, (files, why))
        with open(self.p("win.txt"), "rb") as f:
            self.assertEqual(f.read(), b"a\r\nB\r\n")

    def test_rename_counts_both_paths(self):
        write(self.p("old.txt"), "keep me\n")
        git(self.root, "add", "-A")
        git(self.root, *IDENT, "commit", "-q", "-m", "old")
        base, c = self.race("a")
        path, branch = c["a"]
        os.rename(os.path.join(path, "old.txt"), os.path.join(path, "new.txt"))
        cp.save_copy(path)
        write(self.p("old.txt"), "the user's edit\n")
        ok, files, why = cp.use_copy(self.root, base, branch)
        self.assertEqual((ok, why), (False, "clash"))
        self.assertIn("old.txt", files)
        self.assertEqual(read(self.p("old.txt")), "the user's edit\n")

    def test_different_lines_of_one_file_merge(self):
        write(self.p("long.txt"), "".join(f"line {i}\n" for i in range(20)))
        git(self.root, "add", "-A")
        git(self.root, *IDENT, "commit", "-q", "-m", "long")
        base, c = self.race("a")
        text = read(os.path.join(c["a"][0], "long.txt")).replace("line 0\n", "LINE 0 (copy)\n")
        write(os.path.join(c["a"][0], "long.txt"), text)
        cp.save_copy(c["a"][0])
        write(self.p("long.txt"), read(self.p("long.txt")).replace("line 19\n", "LINE 19 (user)\n"))
        ok, files, why = cp.use_copy(self.root, base, c["a"][1])
        self.assertTrue(ok, (files, why))
        merged = read(self.p("long.txt"))
        self.assertIn("LINE 0 (copy)", merged)
        self.assertIn("LINE 19 (user)", merged)

    def test_outside_the_chats_folder_is_refused(self):
        sub = self.p("web")
        write(os.path.join(sub, "page.html"), "<p>\n")
        write(self.p("root-untracked.txt"), "not in scope\n")
        base = cp.snapshot(sub, "refs/agent-groupchat/t/base")
        self.assertNotIn("root-untracked.txt", git(self.root, "ls-tree", "-r", "--name-only", base))
        path = os.path.join(self.copies, "a")
        folder = cp.make_copy(self.root, base, path, "agent/t/a", "web")
        write(os.path.join(path, "outside.txt"), "x\n")
        write(os.path.join(folder, "page.html"), "<p>new\n")
        cp.save_copy(path)
        ok, files, why = cp.use_copy(self.root, base, "agent/t/a", "web")
        self.assertEqual((ok, files, why), (False, ["outside.txt"], "outside"))
        self.assertEqual(read(os.path.join(sub, "page.html")), "<p>\n")

    def test_empty_folder_still_exists_in_the_copy(self):
        empty = self.p("empty")
        os.makedirs(empty)
        base = cp.snapshot(empty)
        folder = cp.make_copy(self.root, base, os.path.join(self.copies, "a"), "agent/t/a", "empty")
        self.assertTrue(os.path.isdir(folder))

    def test_force_staged_ignored_file_is_in_the_copy(self):
        write(self.p("secret.txt"), "staged on purpose\n")
        git(self.root, "add", "-f", "secret.txt")
        base, c = self.race("a")
        self.assertEqual(read(os.path.join(c["a"][0], "secret.txt")), "staged on purpose\n")
        self.assertIn("secret.txt", git(self.root, "diff", "--cached", "--name-only"))

    def test_conflict_files_with_spaces(self):
        base, c = self.race("a", "b")
        for n, t in (("a", "A\n"), ("b", "B\n")):
            write(os.path.join(c[n][0], "file with spaces.txt"), t)
            cp.save_copy(c[n][0])
        merged = os.path.join(self.copies, "m")
        conflicts = cp.merge_copies(self.root, base, [c["a"][1], c["b"][1]], merged, "agent/t/m")
        self.assertEqual(conflicts, ["file with spaces.txt"])
        self.assertEqual(cp.conflict_markers(merged, conflicts), ["file with spaces.txt"])

    def test_locked_worktree_is_removed_too(self):
        base, c = self.race("a")
        git(self.root, "worktree", "lock", c["a"][0])
        problems = cp.remove_copies(self.root, [c["a"][0]], [c["a"][1]])
        self.assertEqual(problems, [])
        self.assertEqual(git(self.root, "branch", "--list", "agent/*"), "")

    def test_submodules_are_detected(self):
        sha = git(self.root, "rev-parse", "HEAD").strip()
        git(self.root, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/lib")
        git(self.root, *IDENT, "commit", "-q", "-m", "sub")
        self.assertTrue(cp.has_submodules(self.root, "HEAD"))


# ---------------------------------------------------------------- Race
class RaceFixes(RaceTest):
    def test_retry_runs_in_the_copy_not_the_users_folder(self):
        self.rules({"style": "claude", "if": "(⚡ Race: you and", "exit": 1}, *RACE_RULES)
        c = self.chat(agents=RACE_TEAM)
        srv.start_race(c, "build it")
        srv.worker(c)
        r = c["race"]
        self.assertEqual(r["copies"]["claude-1"]["status"], "error")
        idx = next(i for i, m in enumerate(c["messages"])
                   if m["who"] == "claude-1" and (m.get("meta") or {}).get("copy"))
        self.assertEqual(srv.retry_block(c, idx), "")
        self.rules(*RACE_RULES)
        srv.worker(c, lambda: srv.retry(c, idx))
        last = [x for x in self.calls("claude") if "(⚡ Race: you and" in x["argv"][-1]][-1]
        self.assertEqual(last["cwd"], os.path.realpath(r["copies"]["claude-1"]["folder"]))
        self.assertEqual(self.read(self.folder, "app.py"), "orig\n")
        self.assertEqual((r["copies"]["claude-1"]["status"], r["stage"]), ("done", "done"))

    def test_raised_reviewer_is_read_only_during_a_race(self):
        team = RACE_TEAM[:3] + [{"adapter": "claude", "roles": ["reviewer"], "level": "work"}]
        self.rules({"if": "(⚡ Race · Reviewer", "write": {"app.py": "reviewer!\n"}, "reply": "PICK: codex"},
                   *RACE_RULES)
        c, r = self.race(agents=team)
        argv = next(x for x in self.calls() if "(⚡ Race · Reviewer" in x["argv"][-1])["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("Write", argv[argv.index("--allowedTools"):argv.index("--disallowedTools")])

    def test_tester_commits_are_undone(self):
        self.rules({"if": "(⚡ Race · Tester", "write": {"app.py": "tester\n"}, "commit": True,
                    "reply": "1/1 pass"}, *RACE_RULES[1:])
        c, r = self.race()
        self.assertEqual(self.read(r["copies"]["codex"]["folder"], "app.py"), "codex\n")
        self.assertTrue(r["copies"]["codex"].get("test_warn"))

    def test_conflict_markers_left_make_a_combine_unusable(self):
        self.rules({"if": "(⚡ Race · Combine", "reply": "Fixed (not really)."}, *RACE_RULES)
        c, r = self.race()
        self.assertEqual(srv.race_action(c, {"action": "combine", "copies": ["claude-1", "codex"]})[0], 200)
        self.assertEqual(srv.race_action(c, {"action": "combine", "copies": ["claude-1", "codex"]})[0], 409)
        self.wait_idle(c)
        res = r["copies"]["_combined"]
        self.assertEqual(res["status"], "conflicts")
        self.assertIn("app.py", res["warn"])
        self.assertEqual(srv.race_action(c, {"action": "use", "copy": "_combined"})[0], 400)

    def wait_idle(self, c):
        import time
        for _ in range(200):
            if c["id"] not in srv.workers and c["race"]["stage"] in ("done", "stopped"):
                return
            time.sleep(0.05)

    def test_restart_mid_race_stops_it(self):
        c = self.chat(agents=RACE_TEAM)
        srv.start_race(c, "build it")  # saved with stage "work", no worker ran
        with open(srv.chat_path(c["id"])) as f:
            saved = json.load(f)
        saved["race"]["copies"] = {"codex": {"status": "working", "path": "/x", "branch": "b"}}
        srv.migrate(saved)
        self.assertEqual((saved["race"]["stage"], saved["race"]["interrupted"]), ("stopped", True))
        self.assertEqual(saved["race"]["copies"]["codex"]["status"], "stopped")
        self.assertNotIn(saved["race"]["stage"], srv.RUNNING)  # nothing picks it up half-way

    def test_deleted_chat_removes_a_copy_made_afterwards(self):
        c = self.chat(agents=RACE_TEAM)
        srv.start_race(c, "build it")
        r = c["race"]
        c["deleted"] = True
        self.assertIsNone(srv.open_copy(c, r, "codex", "codex"))
        self.assertFalse(os.path.exists(srv.worktree_dir(c, "codex")))
        self.assertEqual(git(self.folder, "branch", "--list", "agent/*"), "")
        c.pop("deleted")

    def test_failed_merge_is_still_cleaned_up(self):
        c, r = self.race()
        r["copies"]["ghost"] = {"agent": "codex", "name": "Ghost", "path": srv.worktree_dir(c, "ghost"),
                                "branch": "agent/nope/ghost", "status": "done"}
        rec, err = srv.merge_into_result(c, r, ["ghost", "codex"], "claude-1")
        self.assertIsNone(rec)
        self.assertEqual(r["copies"]["_combined"]["status"], "error")
        srv.discard_race(c)
        self.assertEqual(git(self.folder, "branch", "--list", "agent/*"), "")
        self.assertEqual(git(self.folder, "worktree", "list").count("\n"), 1)


# ---------------------------------------------------------------- Split
class SplitFixes(RaceTest):
    def setUp(self):
        super().setUp()
        self.rules(*SPLIT_RULES)

    def test_failed_part_stops_and_retry_finishes_it(self):
        self.rules({"if": "YOUR part: build page.html", "exit": 1}, *SPLIT_RULES)
        c = self.chat(agents=SPLIT_TEAM)
        srv.start_split(c, "build login", OWN)
        srv.worker(c)
        r = c["race"]
        self.assertEqual(r["stage"], "stopped")
        self.assertNotIn("_combined", r["copies"])
        idx = next(i for i, m in enumerate(c["messages"]) if m["who"] == "codex" and (m.get("meta") or {}).get("copy"))
        self.rules(*SPLIT_RULES)
        srv.worker(c, lambda: srv.retry(c, idx))
        self.assertEqual(r["stage"], "done")
        self.assertEqual(sorted(r["copies"]["_combined"]["files"]), ["api.py", "page.html", "tests/test_api.py"])

    def test_agent_named_combined_keeps_its_part(self):
        team = [{"adapter": "claude", "roles": ["lead"], "name": "Combined"}, *SPLIT_TEAM[2:]]
        c = self.chat(agents=team)
        srv.start_split(c, "build login", {"combined": "build api.py", "codex": "build page.html"})
        srv.worker(c)
        self.assertEqual(sorted(c["race"]["copies"]["_combined"]["files"])[:2], ["api.py", "page.html"])

    def test_raised_planner_is_read_only(self):
        team = [{"adapter": "claude", "roles": ["planner"], "level": "work"}, *SPLIT_TEAM[1:]]
        c = self.chat(agents=team)
        srv.start_split(c, "build login")
        srv.worker(c)
        argv = next(x for x in self.calls() if "(🧩 Split · Planner" in x["argv"][-1])["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")

    def test_go_twice_and_cancel_while_waiting(self):
        c = self.chat(agents=SPLIT_TEAM)
        srv.start_split(c, "build login")
        srv.worker(c)
        self.assertTrue(srv.split_go(c))
        self.assertFalse(srv.split_go(c))  # the second Go finds nothing waiting
        c2 = self.chat(agents=SPLIT_TEAM)
        srv.start_split(c2, "build login")
        srv.worker(c2)
        code, _ = srv.race_action(c2, {"action": "discard"})
        self.assertEqual(code, 200)
        self.assertIsNone(c2["race"])
        self.assertEqual(c2["messages"][0]["status"]["codex"], "skipped")

    def test_combine_isnt_for_a_split(self):
        c = self.chat(agents=SPLIT_TEAM)
        srv.start_split(c, "build login", OWN)
        srv.worker(c)
        self.assertEqual(srv.race_action(c, {"action": "combine", "copies": ["claude-2", "codex"]})[0], 400)

    def test_no_split_goes_to_the_lead_only(self):
        self.rules({"if": "(🧩 Split · Planner", "reply": "Can't.\nNO SPLIT"}, *SPLIT_RULES[1:])
        c = self.chat(agents=SPLIT_TEAM)
        srv.start_split(c, "build login")
        srv.worker(c)
        codex = [x for x in self.calls("codex")]
        self.assertEqual(codex, [])  # the editing Participant isn't asked
        self.assertEqual(len([x for x in self.calls() if "(Workflow · Lead" in x["argv"][-1]]), 1)


class SplitHttpFixes(HttpBase, RaceTest):
    def setUp(self):
        super().setUp()
        self.rules(*SPLIT_RULES)

    def waiting_chat(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder, "agents": SPLIT_TEAM})
        cid = json.loads(body)["id"]
        self.req("POST", f"/api/chats/{cid}/send", {"text": "build login", "mode": "split", "split": "planner"})
        import time
        for _ in range(200):
            if srv.split_waiting(srv.chats[cid]) and cid not in srv.workers:
                return cid
            time.sleep(0.05)
        self.fail("the Split never waited for the OK")

    def test_stop_cancels_a_waiting_split(self):
        cid = self.waiting_chat()
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/stop", {})[0], 200)
        self.assertIsNone(srv.chats[cid]["race"])
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/go", {})[0], 409)  # nothing starts
        self.req("POST", f"/api/chats/{cid}/delete", {})

    def test_reply_button_to_the_planner_revises_the_parts(self):
        cid = self.waiting_chat()
        self.req("POST", f"/api/chats/{cid}/send", {"text": "@claude-1 please give the page to Claude 2"})
        import time
        for _ in range(200):
            if srv.split_waiting(srv.chats[cid]) and cid not in srv.workers and \
                    len([x for x in self.calls() if "(🧩 Split · Planner" in x["argv"][-1]]) == 2:
                break
            time.sleep(0.05)
        self.assertEqual(len([x for x in self.calls() if "(🧩 Split · Planner" in x["argv"][-1]]), 2)
        self.req("POST", f"/api/chats/{cid}/delete", {})


class Names(unittest.TestCase):
    def test_windows_reserved_ids(self):
        agents = srv.make_agents([{"adapter": "claude", "name": "CON"}, {"adapter": "codex", "name": "aux"}])
        self.assertEqual([a["id"] for a in agents], ["con-agent", "aux-agent"])


if __name__ == "__main__":
    unittest.main()
