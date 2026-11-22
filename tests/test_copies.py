"""Copies of the project for Race/Split (agent_groupchat/copies.py), on throwaway git repos."""
import os
import shutil
import tempfile
import unittest

from agent_groupchat import copies as cp
from agent_groupchat.copies import IDENT, git


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="gc-copies-"))
        self.root = os.path.join(self.tmp, "project")
        self.copies = os.path.join(self.tmp, "copies")
        os.makedirs(self.root)
        write(os.path.join(self.root, "app.py"), "def greet():\n    return 'hi'\n")
        write(os.path.join(self.root, ".gitignore"), "secret.txt\n")
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, *IDENT, "commit", "-q", "-m", "start")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def staged(self):
        return git(self.root, "diff", "--cached", "--name-only")

    def race(self, *names):
        """A snapshot and one copy per name. -> (base, {name: (path, branch)})."""
        base = cp.snapshot(self.root, "refs/agent-groupchat/test/base")
        out = {}
        for n in names:
            path, branch = os.path.join(self.copies, n), f"agent/test/{n}"
            cp.make_copy(self.root, base, path, branch)
            out[n] = (path, branch)
        return base, out


class GitStatus(Repo):
    def test_status_and_init(self):
        self.assertTrue(cp.status(self.root)["ok"])
        plain = os.path.join(self.tmp, "plain")
        write(os.path.join(plain, "notes.md"), "hello\n")
        st = cp.status(plain)
        self.assertEqual((st["ok"], st["can_init"]), (False, True))
        self.assertEqual(cp.init_repo(plain), os.path.realpath(plain))
        self.assertEqual(read(os.path.join(plain, "notes.md")), "hello\n")  # files unchanged
        self.assertIn("notes.md", git(plain, "ls-files"))
        with self.assertRaises(cp.GitError):
            cp.init_repo(plain)  # already a repo


class Snapshot(Repo):
    def test_includes_uncommitted_and_new_files_but_not_ignored(self):
        write(self.p("app.py"), "def greet():\n    return 'hello'\n")  # uncommitted edit
        write(self.p("new.py"), "x = 1\n")                                # new, untracked
        write(self.p("secret.txt"), "password\n")                         # ignored
        git(self.root, "add", "app.py")                                   # something staged
        before = (self.staged(), git(self.root, "status", "--porcelain"))
        base, c = self.race("a")
        path = c["a"][0]
        self.assertIn("'hello'", read(os.path.join(path, "app.py")))
        self.assertEqual(read(os.path.join(path, "new.py")), "x = 1\n")
        self.assertFalse(os.path.exists(os.path.join(path, "secret.txt")))
        # the user's staging area and files are exactly as before
        self.assertEqual((self.staged(), git(self.root, "status", "--porcelain")), before)

    def test_repo_without_commits(self):
        fresh = os.path.join(self.tmp, "fresh")
        write(os.path.join(fresh, "a.txt"), "a\n")
        git(fresh, "init", "-q")
        sha = cp.snapshot(fresh)
        self.assertIn("a.txt", git(fresh, "ls-tree", "-r", "--name-only", sha))

    def test_project_in_a_subfolder_of_the_repo(self):
        sub = self.p("web")
        write(os.path.join(sub, "page.html"), "<p>hi</p>\n")
        base = cp.snapshot(sub)
        self.assertIn("web/page.html", git(self.root, "ls-tree", "-r", "--name-only", base))


class UseCopy(Repo):
    def test_changes_land_uncommitted_in_the_user_folder(self):
        base, c = self.race("claude")
        path, branch = c["claude"]
        write(os.path.join(path, "app.py"), "def greet():\n    return 'HI'\n")
        write(os.path.join(path, "tests", "test_app.py"), "import app\n")
        self.assertTrue(cp.save_copy(path))
        self.assertEqual(cp.changed_files(self.root, base, branch), ["app.py", "tests/test_app.py"])
        self.assertIn("+    return 'HI'", cp.diff(self.root, base, branch))
        ok, files = cp.use_copy(self.root, base, branch)
        self.assertTrue(ok)
        self.assertIn("'HI'", read(self.p("app.py")))
        self.assertTrue(os.path.exists(self.p("tests", "test_app.py")))
        self.assertEqual(self.staged(), "")  # nothing staged or committed for the user
        self.assertEqual(git(self.root, "log", "--oneline").count("\n"), 1)

    def test_nothing_changed(self):
        base, c = self.race("a")
        self.assertFalse(cp.save_copy(c["a"][0]))
        self.assertEqual(cp.use_copy(self.root, base, c["a"][1]), (True, []))

    def test_clash_with_the_users_own_later_change_is_refused(self):
        base, c = self.race("codex")
        path, branch = c["codex"]
        write(os.path.join(path, "app.py"), "def greet():\n    return 'from codex'\n")
        cp.save_copy(path)
        write(self.p("app.py"), "def greet():\n    return 'mine'\n")  # the user edits meanwhile
        ok, clash = cp.use_copy(self.root, base, branch)
        self.assertEqual((ok, clash), (False, ["app.py"]))
        self.assertIn("'mine'", read(self.p("app.py")))  # nothing overwritten

    def test_a_new_file_in_the_snapshot_is_no_false_clash(self):
        write(self.p("draft.py"), "a = 1\n")  # untracked when the race starts
        base, c = self.race("a")
        path, branch = c["a"]
        write(os.path.join(path, "draft.py"), "a = 2\n")
        cp.save_copy(path)
        ok, files = cp.use_copy(self.root, base, branch)
        self.assertTrue(ok, files)
        self.assertEqual(read(self.p("draft.py")), "a = 2\n")


class Merge(Repo):
    def test_parts_that_dont_overlap_merge_cleanly(self):
        base, c = self.race("api", "page")
        write(os.path.join(c["api"][0], "api.py"), "def login(): ...\n")
        write(os.path.join(c["page"][0], "page.html"), "<form></form>\n")
        for n in c:
            cp.save_copy(c[n][0])
        merged = os.path.join(self.copies, "merged")
        conflicts = cp.merge_copies(self.root, base, [c["api"][1], c["page"][1]], merged,
                                    "agent/test/merged")
        self.assertEqual(conflicts, [])
        self.assertTrue(os.path.exists(os.path.join(merged, "api.py")))
        self.assertTrue(os.path.exists(os.path.join(merged, "page.html")))
        self.assertEqual(cp.changed_files(self.root, base, "agent/test/merged"), ["api.py", "page.html"])

    def test_overlapping_parts_report_conflicts_with_markers(self):
        base, c = self.race("a", "b")
        write(os.path.join(c["a"][0], "app.py"), "def greet():\n    return 'A'\n")
        write(os.path.join(c["b"][0], "app.py"), "def greet():\n    return 'B'\n")
        for n in c:
            cp.save_copy(c[n][0])
        merged = os.path.join(self.copies, "merged")
        conflicts = cp.merge_copies(self.root, base, [c["a"][1], c["b"][1]], merged, "agent/test/merged")
        self.assertEqual(conflicts, ["app.py"])
        text = read(os.path.join(merged, "app.py"))
        self.assertIn("<<<<<<<", text)
        self.assertIn("'A'", text)
        self.assertIn("'B'", text)


class Cleanup(Repo):
    def test_remove_copies(self):
        base, c = self.race("a", "b")
        cp.remove_copies(self.root, [p for p, _ in c.values()], [b for _, b in c.values()],
                         ["refs/agent-groupchat/test/base"])
        for path, branch in c.values():
            self.assertFalse(os.path.exists(path))
            self.assertNotIn(branch, git(self.root, "branch", "--list", "agent/*"))
        self.assertEqual(git(self.root, "worktree", "list").count("\n"), 1)
        self.assertEqual(git(self.root, "for-each-ref", "refs/agent-groupchat"), "")
        cp.remove_copies(self.root, [os.path.join(self.copies, "gone")], ["agent/none"])  # no errors


if __name__ == "__main__":
    unittest.main()
