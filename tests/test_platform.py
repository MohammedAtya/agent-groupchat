"""Linux / macOS / Windows differences, the Stop button, and the launcher."""
import contextlib
import io
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.helpers import ChatTest, srv
from tests.test_http import HttpBase
from agent_groupchat import platform_support as plat
from agent_groupchat.skills import ancestors

SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
IF EXIST "%dp0%\node.exe" (SET "_prog=%dp0%\node.exe") ELSE (SET "_prog=node")
endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@google\gemini-cli\dist\index.js" %*
'''


class WindowsShims(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.shim = os.path.join(self.dir, "gemini.cmd")
        with open(self.shim, "w", encoding="utf-8") as f:
            f.write(SHIM)
        self.js = os.path.join(self.dir, "node_modules", "@google", "gemini-cli", "dist", "index.js")
        os.makedirs(os.path.dirname(self.js))
        open(self.js, "w").close()
        self.node = os.path.join(self.dir, "node.exe")
        open(self.node, "w").close()

    def test_npm_shim_runs_node_directly(self):
        self.assertEqual(plat.npm_shim_target(self.shim), [self.node, os.path.normpath(self.js)])

    def test_resolve_on_windows(self):
        with mock.patch.object(plat, "IS_WINDOWS", True), \
                mock.patch.object(plat.shutil, "which", return_value=self.shim):
            cmd = plat.resolve_command(["gemini", "--prompt", 'a" & del /q * & "'])
        self.assertEqual(cmd, [self.node, os.path.normpath(self.js), "--prompt", 'a" & del /q * & "'])

    def test_other_batch_files_are_refused(self):
        bat = os.path.join(self.dir, "tool.bat")
        with open(bat, "w") as f:
            f.write("@echo off\nsomething.exe %*\n")
        with mock.patch.object(plat, "IS_WINDOWS", True), \
                mock.patch.object(plat.shutil, "which", return_value=bat):
            with self.assertRaises(plat.UnsafeCommand):
                plat.resolve_command(["tool", "x"])

    def test_elsewhere_nothing_changes(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertEqual(plat.resolve_command(["claude", "-p"]), ["claude", "-p"])


class Paths(unittest.TestCase):
    def test_ancestors_stop_at_the_root(self):
        chain = ancestors(os.path.join(os.path.abspath(os.sep), "x", "y"))
        self.assertEqual(chain[-1], os.path.abspath(os.sep))
        self.assertLess(len(chain), 10)


class NativeWindowsLevels(ChatTest):
    def test_no_commands_or_auto_on_native_windows(self):
        with mock.patch.object(plat, "COMMANDS_BLOCKED", True):
            c = self.chat(agents=[{"adapter": "claude", "level": "auto"},
                                  {"adapter": "codex", "roles": ["lead"]}])
            cl, cx = srv.level_info(c, "claude"), srv.level_info(c, "codex")
        self.assertEqual((cl["level"], cx["level"]), ("edit", "read"))
        self.assertIn("WSL", cl["limited"])
        self.assertIn("WSL", cx["limited"])

    def test_sandbox_by_os(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertFalse(plat.sandbox_ok())
            self.assertIn("WSL", plat.sandbox_help())
        with mock.patch.object(plat, "IS_WINDOWS", False), mock.patch.object(plat, "IS_MAC", True), \
                mock.patch.object(plat.shutil, "which", return_value="/usr/bin/sandbox-exec"):
            self.assertTrue(plat.sandbox_ok())


class StopButton(ChatTest):
    def test_stop_kills_a_running_agent(self):
        self.rules({"sleep": 30})
        c = self.chat(agents=["claude"])
        srv.post_user_message(c, "hi")
        t = threading.Thread(target=srv.worker, args=(c,))
        t.start()
        for _ in range(100):  # wait until the fake agent runs
            proc = srv.procs.get((c["id"], "claude"))
            if proc:
                break
            time.sleep(0.05)
        self.assertIsNotNone(proc)
        start = time.time()
        srv.halted.add(c["id"])
        srv.kill(proc, "stopped")
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertLess(time.time() - start, 8)
        self.assertEqual(self.replies(c)[0]["text"], "(stopped)")
        self.assertEqual(c["messages"][0]["status"]["claude"], "stopped")


class Launcher(HttpBase):
    def test_probe(self):
        self.assertEqual(plat.probe(srv.PORT), "ours")
        with socket.socket() as s:  # something else listening
            s.bind(("127.0.0.1", 0))
            s.listen()
            busy = s.getsockname()[1]
            self.assertEqual(plat.probe(busy), "busy")
        self.assertEqual(plat.probe(busy), "free")

    def test_second_launch_just_opens_the_running_one(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            srv.main(["--port", str(srv.PORT), "--no-browser"])
        self.assertIn("already running", out.getvalue())

    def test_version(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()) as out:
            srv.main(["--version"])
        self.assertIn("agent-groupchat", out.getvalue())


if __name__ == "__main__":
    unittest.main()
