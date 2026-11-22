"""What differs between Linux, macOS, WSL and native Windows, in one place."""
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import webbrowser

SYSTEM = platform.system()          # "Linux", "Darwin" or "Windows"
IS_WINDOWS = os.name == "nt"        # native Windows (WSL reports Linux)
IS_MAC = SYSTEM == "Darwin"
IS_WSL = SYSTEM == "Linux" and "microsoft" in platform.release().lower()
# On native Windows the agents' command sandboxes don't work, so commands and auto mode are off.
COMMANDS_BLOCKED = IS_WINDOWS
WINDOWS_LIMIT = ("isn't available on native Windows (the agents can't sandbox commands there); "
                 "run agent-groupchat inside WSL for it")

_sandbox = {"ok": None, "checked": 0.0}


def sandbox_ok():
    """Can Claude Code and Codex confine commands to the project folder on this computer?
    Linux/WSL: bubblewrap must run (Ubuntu's AppArmor can block it). macOS: Seatbelt
    (sandbox-exec), built in. Native Windows: no. Checked at most once a minute."""
    if IS_WINDOWS:
        return False
    if IS_MAC:
        return shutil.which("sandbox-exec") is not None
    if time.time() - _sandbox["checked"] > 60:
        try:
            r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"],
                               capture_output=True, timeout=10)
            _sandbox["ok"] = r.returncode == 0
        except Exception:
            _sandbox["ok"] = False
        _sandbox["checked"] = time.time()
    return _sandbox["ok"]


def sandbox_help():
    """One line for the page when the sandbox is missing."""
    if IS_WINDOWS:
        return ("Native Windows: agents can read and edit files but not run commands. For the full "
                "feature set, run agent-groupchat inside WSL.")
    if IS_MAC:
        return "The macOS sandbox (sandbox-exec) wasn't found, so agents can't run commands."
    if not shutil.which("bwrap"):
        return ("bubblewrap isn't installed, so agents can't run commands. Install it "
                "(e.g. sudo apt install bubblewrap) and the page notices within a minute.")
    return ("bubblewrap is blocked (on Ubuntu 23.10+ by AppArmor), so agents can't run commands. "
            "See \"Enable the sandbox\" in the README.")


# ---------------------------------------------------------------- processes
def popen_kwargs():
    """Start an agent in its own process group, so Stop can end it with all its children."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(p):
    """End an agent and everything it started: TERM, then KILL after 5 s (Windows: taskkill /T)."""
    if p.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    def hard():
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    threading.Timer(5, hard).start()


# npm's Windows shims end with e.g.  "%_prog%"  "%dp0%\node_modules\@google\gemini-cli\dist\index.js" %*
SHIM_RE = re.compile(r'"%~?dp0%?\\([^"%]+?\.(?:js|mjs|cjs|exe))"', re.I)


def npm_shim_target(path):
    """[node, script] (or [exe]) that an npm .cmd shim runs, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            found = SHIM_RE.findall(f.read())
    except OSError:
        return None
    # the shim also mentions "%dp0%\node.exe" (which node to use): that's not the target
    found = [x for x in found if os.path.basename(x.replace("\\", "/")).lower() != "node.exe"]
    if not found:
        return None
    base = os.path.dirname(path)
    target = os.path.normpath(os.path.join(base, *re.split(r"[\\/]", found[-1])))
    if not os.path.exists(target):
        return None
    if target.lower().endswith(".exe"):
        return [target]
    node = os.path.join(base, "node.exe")
    node = node if os.path.exists(node) else shutil.which("node")
    return [node, target] if node else None


class UnsafeCommand(Exception):
    pass


def resolve_command(cmd):
    """The argv to start an agent with. On Windows, starting a .cmd/.bat file runs cmd.exe,
    which re-parses the arguments (our prompts contain chat text) and can be tricked into
    running commands ("BatBadBut"). So an npm .cmd shim is replaced by the node script it
    would run, and any other batch file is refused."""
    if not IS_WINDOWS or not cmd:
        return cmd
    exe = shutil.which(cmd[0])
    if not exe:
        return cmd  # Popen reports "not found"
    if exe.lower().endswith((".cmd", ".bat")):
        target = npm_shim_target(exe)
        if not target:
            raise UnsafeCommand(f"{cmd[0]} is a .cmd script, and running it with chat text as "
                                "arguments isn't safe on Windows. Install it so it has a .exe, "
                                "or use agent-groupchat inside WSL.")
        return target + list(cmd[1:])
    return [exe, *cmd[1:]]


# ---------------------------------------------------------------- launcher
def probe(port):
    """'free', 'ours' (an agent-groupchat answers there) or 'busy' (something else)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        return "free"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            return "ours" if r.headers.get("X-Agent-Groupchat") else "busy"
    except Exception:
        return "busy"


def open_browser(url, delay=0.6):
    def go():
        try:
            webbrowser.open(url)
        except Exception:
            pass  # no browser (e.g. a server or WSL without one): the URL is printed anyway
    threading.Timer(delay, go).start()
