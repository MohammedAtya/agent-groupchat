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


class CopyIgnoredUnit(unittest.TestCase):
    def test_symlinks_are_copied_as_links_not_followed(self):
        import shutil
        import tempfile
        src, dest = tempfile.mkdtemp(), tempfile.mkdtemp()
        try:
            git(src, "init", "-q")
            with open(os.path.join(src, ".gitignore"), "w") as f:
                f.write("data\n")
            outside = tempfile.mkdtemp()
            try:
                os.symlink(outside, os.path.join(src, "data"))
            except (OSError, NotImplementedError):
                self.skipTest("no symlinks here")
            out = copies.copy_ignored(src, dest)
            self.assertEqual(out["names"], ["data"])
            self.assertTrue(os.path.islink(os.path.join(dest, "data")))
            shutil.rmtree(outside, ignore_errors=True)
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(dest, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
