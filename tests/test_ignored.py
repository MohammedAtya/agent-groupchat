"""Race/Split copies get the user's git-ignored files (.env, node_modules, .venv…), SPEC §14.5b."""
import os
import unittest
from unittest import mock

from tests.helpers import srv
from tests.test_race import RULES, TEAM, RaceTest
from agent_groupchat import copies
from agent_groupchat.copies import IDENT, git


class IgnoredFiles(RaceTest):
    def setUp(self):
        super().setUp()
        self.put(".gitignore", ".env\nnode_modules/\n__pycache__/\nbig/\n*.log\n")
        git(self.folder, "add", ".gitignore")
        git(self.folder, *IDENT, "commit", "-q", "-m", "ignore")
        self.put(".env", "TOKEN=local\n")
        self.put("node_modules/pkg/index.js", "module.exports = 1\n")
        self.put("__pycache__/app.cpython-312.pyc", "cache")

    def put(self, rel, text):
        path = os.path.join(self.folder, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def test_copies_get_ignored_files_but_not_caches(self):
        c, r = self.race()
        for name in ("claude-1", "codex"):
            folder = r["copies"][name]["folder"]
            self.assertEqual(self.read(folder, ".env"), "TOKEN=local\n")
            self.assertEqual(self.read(folder, "node_modules", "pkg", "index.js"), "module.exports = 1\n")
            self.assertFalse(os.path.exists(os.path.join(folder, "__pycache__")))
        self.assertEqual(sorted(r["ignored"]["names"]), [".env", "node_modules"])
        notes = [m["text"] for m in c["messages"] if m["who"] == "system"]
        self.assertTrue(any("git-ignored files were copied" in t and ".env" in t for t in notes), notes)

    def test_they_never_come_back_with_use_this_one(self):
        self.rules({"style": "codex", "if": "(⚡ Race: you and",
                    "write": {"app.py": "codex\n", ".env": "TOKEN=agent\n"}, "reply": "Done."}, *RULES)
        c, r = self.race()
        self.assertEqual(self.read(r["copies"]["codex"]["folder"], ".env"), "TOKEN=agent\n")
        code, out = srv.race_action(c, {"action": "use", "copy": "codex"})
        self.assertEqual((code, out["files"]), (200, ["app.py"]))
        self.assertEqual(self.read(self.folder, ".env"), "TOKEN=local\n")  # the user's stays as it was

    def test_too_big_is_left_out_and_said(self):
        self.put("server.log", "x" * 5000)
        with mock.patch.object(copies, "MAX_IGNORED_BYTES", 1000):
            c, r = self.race()
        folder = r["copies"]["codex"]["folder"]
        self.assertFalse(os.path.exists(os.path.join(folder, "server.log")))
        self.assertTrue(os.path.exists(os.path.join(folder, ".env")))
        self.assertEqual(r["ignored"]["skipped"], [["server.log", 5000]])
        notes = " ".join(m["text"] for m in c["messages"] if m["who"] == "system")
        self.assertIn("Not copied (too big): server.log (4.9 KB)", notes)
        racer_prompts = [x["argv"][-1] for x in self.calls("codex")]
        self.assertTrue(any("Not in this copy because they're too big: server.log" in p
                            for p in racer_prompts))

    def test_a_huge_folder_says_over_the_limit(self):
        for i in range(30):
            self.put(f"big/f{i}.bin", "x" * 100)
        with mock.patch.object(copies, "MAX_IGNORED_BYTES", 1000):
            c, r = self.race()
        self.assertEqual(r["ignored"]["skipped"], [["big", None]])
        notes = " ".join(m["text"] for m in c["messages"] if m["who"] == "system")
        self.assertIn("Not copied (too big): big (over 1000 bytes)", notes)

    def test_no_note_when_nothing_is_ignored(self):
        for rel in (".env", "node_modules/pkg/index.js", "__pycache__/app.cpython-312.pyc"):
            os.remove(os.path.join(self.folder, *rel.split("/")))
        c, r = self.race()
        self.assertEqual(r["ignored"]["names"], [])
        self.assertFalse(any("git-ignored" in m["text"] for m in c["messages"] if m["who"] == "system"))


class Links(unittest.TestCase):
    """No symlink in a copy may lead out of it (to the user's files or anywhere else)."""
    def setUp(self):
        import tempfile
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.src, self.dest = os.path.join(self.root, "proj"), os.path.join(self.root, "copy")
        self.outside = os.path.join(self.root, "outside.txt")
        for folder in (self.src, self.dest):
            os.makedirs(os.path.join(folder, "conf"))
            git(folder, "init", "-q")
            with open(os.path.join(folder, "conf", "real.cfg"), "w") as f:
                f.write("x")  # (the copy has the project's tracked files)
        with open(self.outside, "w") as f:
            f.write("not the project's")
        with open(os.path.join(self.src, ".gitignore"), "w") as f:
            f.write("local.cfg\nlinks/\nout.txt\nup.txt\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def link(self, target, *where):
        try:
            os.symlink(target, os.path.join(self.src, *where))
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")

    def test_links_into_the_project_point_into_the_copy(self):
        os.makedirs(os.path.join(self.src, "links"))
        self.link(os.path.join(self.src, "conf", "real.cfg"), "local.cfg")
        self.link(os.path.join(self.src, "conf"), "links", "conf")
        self.link("../conf/real.cfg", "links", "rel.cfg")
        out = copies.copy_ignored(self.src, self.dest)
        self.assertEqual((sorted(out["names"]), out["links"]), (["links", "local.cfg"], []))
        self.assertEqual(os.readlink(os.path.join(self.dest, "local.cfg")),
                         os.path.join(self.dest, "conf", "real.cfg"))
        self.assertEqual(os.readlink(os.path.join(self.dest, "links", "conf")), os.path.join(self.dest, "conf"))
        self.assertEqual(os.readlink(os.path.join(self.dest, "links", "rel.cfg")), "../conf/real.cfg")

    def test_links_that_lead_out_are_left_out_and_reported(self):
        self.link(self.outside, "out.txt")                         # absolute, outside the project
        self.link(os.path.join("..", "outside.txt"), "up.txt")     # relative, climbs out of the copy
        self.link(os.path.join(self.src, "gone.cfg"), "local.cfg")  # into the project, not in the copy
        out = copies.copy_ignored(self.src, self.dest)
        self.assertEqual(sorted(out["links"]), ["local.cfg", "out.txt", "up.txt"])
        self.assertEqual(out["names"], [])
        for name in ("out.txt", "up.txt", "local.cfg"):
            self.assertFalse(os.path.lexists(os.path.join(self.dest, name)), name)


    def test_read_only_folder_still_loses_its_outward_link(self):
        pkg = os.path.join(self.src, "links", "pkg")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "index.js"), "w") as f:
            f.write("x")
        self.link(self.outside, "links", "pkg", "external.txt")
        os.chmod(pkg, 0o555)
        try:
            out = copies.copy_ignored(self.src, self.dest)
        finally:
            os.chmod(pkg, 0o755)
        copied = os.path.join(self.dest, "links", "pkg")
        self.assertEqual(out["links"], ["links/pkg/external.txt"])
        self.assertFalse(os.path.lexists(os.path.join(copied, "external.txt")))
        self.assertTrue(os.path.exists(os.path.join(copied, "index.js")))
        self.assertEqual(os.stat(copied).st_mode & 0o777, 0o555)  # left as it was

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no named pipes here")
    def test_special_files_are_skipped_and_links_still_checked(self):
        pkg = os.path.join(self.src, "links")
        os.makedirs(pkg)
        os.mkfifo(os.path.join(pkg, "pipe"))
        with open(os.path.join(pkg, "a.txt"), "w") as f:
            f.write("a")
        self.link(self.outside, "links", "external.txt")
        out = copies.copy_ignored(self.src, self.dest)
        self.assertEqual((out["names"], out["links"], out["failed"]), (["links"], ["links/external.txt"], []))
        self.assertFalse(os.path.lexists(os.path.join(self.dest, "links", "pipe")))
        self.assertFalse(os.path.lexists(os.path.join(self.dest, "links", "external.txt")))

    def test_link_chains_in_any_order(self):
        os.makedirs(os.path.join(self.src, "links"))
        # a -> b (absolute, into the project: safe once re-pointed); z -> y -> outside
        self.link(os.path.join(self.src, "conf", "real.cfg"), "links", "b")
        self.link("b", "links", "a")
        self.link(self.outside, "links", "y")
        self.link("y", "links", "z")
        out = copies.copy_ignored(self.src, self.dest)
        self.assertEqual(sorted(out["links"]), ["links/y", "links/z"])
        self.assertEqual(os.path.realpath(os.path.join(self.dest, "links", "a")),
                         os.path.join(self.dest, "conf", "real.cfg"))

    def test_a_partial_copy_is_removed_and_reported(self):
        os.makedirs(os.path.join(self.src, "links"))
        with open(os.path.join(self.src, "links", "a.txt"), "w") as f:
            f.write("a")
        self.link(self.outside, "links", "external.txt")
        def broken(src, dst, **k):  # gets half way (incl. an outward link), then fails
            os.makedirs(dst)
            os.symlink(self.outside, os.path.join(dst, "external.txt"))
            raise OSError("disk full")
        from unittest import mock
        with mock.patch.object(copies.shutil, "copytree", side_effect=broken):
            out = copies.copy_ignored(self.src, self.dest)
        self.assertEqual((out["names"], out["failed"], out["skipped"]), ([], ["links"], []))
        self.assertFalse(os.path.lexists(os.path.join(self.dest, "links")))


    def test_cleanup_that_fails_makes_the_copy_unusable(self):
        os.makedirs(os.path.join(self.src, "links"))
        self.link(self.outside, "links", "external.txt")
        from unittest import mock
        with mock.patch.object(copies, "_remove", return_value=False):
            with self.assertRaises(OSError):
                copies.copy_ignored(self.src, self.dest)


class LinksInARace(RaceTest):
    def test_the_chat_says_which_links_were_left_out(self):
        import tempfile
        outside = os.path.join(os.path.realpath(tempfile.mkdtemp()), "secret.txt")
        with open(outside, "w") as f:
            f.write("s")
        with open(os.path.join(self.folder, ".gitignore"), "w") as f:
            f.write("data.txt\n")
        git(self.folder, "add", ".gitignore")
        git(self.folder, *IDENT, "commit", "-q", "-m", "ignore")
        try:
            os.symlink(outside, os.path.join(self.folder, "data.txt"))
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        c, r = self.race()
        self.assertFalse(os.path.lexists(os.path.join(r["copies"]["codex"]["folder"], "data.txt")))
        self.assertEqual(r["ignored"]["links"], ["data.txt"])
        notes = " ".join(m["text"] for m in c["messages"] if m["who"] == "system")
        self.assertIn("Not copied (links that lead outside the copy): data.txt.", notes)


class UnsafeCopyIsNotUsed(RaceTest):
    def test_the_agent_never_works_in_it(self):
        with mock.patch.object(copies, "copy_ignored", side_effect=OSError("can't delete a link")):
            c, r = self.race()
        self.assertEqual({n: cp["status"] for n, cp in r["copies"].items()},
                         {"claude-1": "error", "codex": "error"})
        self.assertFalse([x for x in self.calls() if "(⚡ Race: you and" in x["argv"][-1]])
        errors = [m["text"] for m in c["messages"] if m["text"].startswith("(error)")]
        self.assertTrue(any("couldn't be made safe" in e and "isn't used" in e for e in errors), errors)


    def test_errors_are_tagged_like_the_turn_they_replace(self):
        c, r = self.race()
        for key, extra, stage in (("_combined", {"from": ["claude-1", "codex"]}, "combine"),
                                  ("codex", {"part": "the API"}, "part")):
            cp = {"agent": "codex", "name": "X", "folder": self.folder, "status": "working", **extra}
            r["copies"][key] = cp
            with mock.patch.object(copies, "copy_ignored", side_effect=OSError("stuck")):
                self.assertFalse(srv.bring_ignored(c, r, cp))
            meta = c["messages"][-1]["meta"]
            self.assertEqual((cp["status"], meta["stage"], meta["copy"]), ("error", stage, key))
        self.assertEqual(c["messages"][r["msg"]]["status"]["codex"], "error")  # the part's turn


if __name__ == "__main__":
    unittest.main()
