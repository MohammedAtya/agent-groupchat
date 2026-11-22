"""Shared setup: fake agent CLIs on PATH, a throwaway data folder, and helpers."""
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.realpath(tempfile.mkdtemp(prefix="gc-test-data-"))  # (macOS: /var -> /private/var)
FAKE = os.path.join(HERE, "bin", "fake_agent.py")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Must be set before the server module is imported (it reads them at import time).
os.environ["AGENT_GROUPCHAT_HOME"] = DATA
LEGACY = os.path.realpath(tempfile.mkdtemp(prefix="gc-test-legacy-"))  # never the real ~/groupchat
os.environ["AGENT_GROUPCHAT_LEGACY"] = LEGACY
os.environ["GROUPCHAT_PORT"] = str(_free_port())
sys.path.insert(0, ROOT)

from agent_groupchat import platform_support as plat  # noqa: E402
from agent_groupchat import server as srv  # noqa: E402
from agent_groupchat.adapters import BUILTIN  # noqa: E402

# Every built-in CLI is replaced by the fake: `python fake_agent.py claude ...` works on every OS
# (tests/bin/claude etc. are the same as shell scripts, for trying the page by hand).
for _a in BUILTIN:
    _a.command = [sys.executable, FAKE, _a.id]


class ChatTest(unittest.TestCase):
    sandbox = True

    def setUp(self):
        self.folder = os.path.realpath(tempfile.mkdtemp(prefix="gc-test-project-"))
        self.log = self.folder + ".calls"  # a folder of one file per fake run
        os.environ["FAKE_LOG"] = self.log
        self._blocked = plat.COMMANDS_BLOCKED
        plat.COMMANDS_BLOCKED = False  # tests act like an OS with a sandbox (Windows CI too)
        os.environ["FAKE_RULES"] = "[]"
        self._orig_sandbox = srv.sandbox_ok
        srv.sandbox_ok = lambda: self.sandbox
        srv.halted.clear()

    def tearDown(self):
        srv.sandbox_ok = self._orig_sandbox
        shutil.rmtree(self.folder, ignore_errors=True)
        shutil.rmtree(self.log, ignore_errors=True)
        plat.COMMANDS_BLOCKED = self._blocked

    # -- helpers
    def reset_calls(self):
        shutil.rmtree(self.log, ignore_errors=True)

    def rules(self, *rules):
        os.environ["FAKE_RULES"] = json.dumps(list(rules))

    def chat(self, level=None, rounds=0, agents=None, lock=False, fix_rounds=1):
        """agents: e.g. ["claude", "claude", "codex"] or [{"adapter": "claude", "roles": [..]}].
        level: permission override for every agent that doesn't set its own."""
        specs = [a if isinstance(a, dict) else {"adapter": a} for a in agents or
                 ["claude", "codex"]]
        if level:
            specs = [{"level": level, **sp} for sp in specs]
        return srv.new_chat(self.folder, rounds, "test", srv.parse_agents(specs), lock=lock,
                            fix_rounds=fix_rounds)

    def send(self, c, text):
        """Post a user message exactly like the page does, then let the AIs answer."""
        srv.post_user_message(c, text, srv.parse_skill(text, c["folder"]))
        srv.worker(c)

    def calls(self, style=None):
        if not os.path.isdir(self.log):
            return []
        runs = []
        for name in sorted(n for n in os.listdir(self.log) if n.endswith(".json")):
            with open(os.path.join(self.log, name), encoding="utf-8") as f:
                runs.append(json.load(f))
        return [r for r in runs if style is None or r["style"] == style]

    def replies(self, c, who=None):
        return [m for m in c["messages"] if m["who"] != "user" and (who is None or m["who"] == who)]
