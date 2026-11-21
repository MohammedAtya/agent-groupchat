#!/usr/bin/env python3
"""Local group chat: you + Claude Code + Codex CLI on one page.

Run:  python3 server.py   then open http://127.0.0.1:8765

- Many chats, each saved in ~/.agent-groupchat/chats/<id>.json (sidebar to reopen any of them).
- Each chat is backed by a REAL Claude Code session and a REAL Codex session,
  so skills, hooks, CLAUDE.md/AGENTS.md, memory and MCP config all load, and the
  sessions can also be continued from a terminal (claude -r / codex resume).
- Each chat has a project folder and a mode:
    talk : read-only (read files, search, use skills; no edits, no commands)
    work : may edit files and run commands, sandboxed to the chat's folder
- "Discussion rounds": after both answer you, they can reply to each other.
- Start a message with @claude or @codex to address only one of them.
"""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import difflib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# User data lives outside the code folder, so updating/reinstalling never touches it and
# it can never end up in git. AGENT_GROUPCHAT_HOME overrides it (used by tests).
DATA_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("AGENT_GROUPCHAT_HOME") or os.path.join("~", ".agent-groupchat")))
CHATS_DIR = os.path.join(DATA_DIR, "chats")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
LOG_FILE = os.path.join(LOGS_DIR, "server.log")
PORT = int(os.environ.get("GROUPCHAT_PORT", "8765"))
BOT_TIMEOUT = 900  # seconds per reply (work mode can take a while)
BOTS = ("claude", "codex")
PASS = "PASS"
MAX_HANDOFFS = 3  # max AI->AI hand-offs after one user message (stops ping-pong)

# Only this page may drive the server. Without these checks any website open in the
# browser could POST to 127.0.0.1 (a text/plain fetch needs no CORS preflight) and start
# an auto-mode chat that runs commands. Host check also blocks DNS rebinding.
TOKEN = secrets.token_urlsafe(24)  # new per launch; injected into the served page
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {"http://" + h for h in ALLOWED_HOSTS}

os.makedirs(CHATS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
lock = threading.RLock()
chats = {}      # id -> chat dict
typing = {}     # id -> {"claude": bool, "codex": bool}
workers = {}    # id -> running worker thread

READ_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Skill", "TodoWrite"]
EDIT_TOOLS = READ_TOOLS + ["Edit", "Write", "NotebookEdit"]
WORK_TOOLS = EDIT_TOOLS + ["Bash"]
CLAUDE_SANDBOX = json.dumps({"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True}})

_sandbox = {"ok": None, "checked": 0}


def sandbox_ok():
    """Both CLIs sandbox commands with bubblewrap. If it can't run (e.g. Ubuntu's
    AppArmor user-namespace restriction), commands would run UNconfined, so work
    mode falls back to: Claude edits files only (no shell), Codex read-only."""
    if time.time() - _sandbox["checked"] > 60:
        try:
            r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"],
                               capture_output=True, timeout=10)
            _sandbox["ok"] = r.returncode == 0
        except Exception:
            _sandbox["ok"] = False
        _sandbox["checked"] = time.time()
    return _sandbox["ok"]


def abilities(chat):
    if chat["mode"] == "talk":
        return "talk"
    if chat["mode"] == "auto":
        return "auto" if sandbox_ok() else "auto-limited"
    return "work" if sandbox_ok() else "edit-only"


ABILITY_TEXT = {
    "talk": "TALK mode: you may read files and use skills, but do not modify anything.",
    "work": "WORK mode: you may edit files and run commands inside the project folder "
            "(sandboxed). Coordinate with the other AI so you don't both edit the same files.",
    "edit-only": "LIMITED WORK mode (sandbox unavailable): Claude may edit files in the project "
                 "folder but cannot run shell commands; Codex is read-only. Coordinate edits.",
    "auto": "AUTO mode: Claude runs in Claude Code auto mode and Codex runs with automatic "
            "approval review. Both may run commands and edit files where the user asks. Codex "
            "starts sandboxed to the project folder with no internet; if blocked, request "
            "escalated permissions and the automatic reviewer will approve or deny. Risky "
            "actions are blocked for both. Do what the user asks.",
    "auto-limited": "AUTO mode: Claude runs in Claude Code auto mode (may run commands and edit "
                    "files anywhere the user asks; a safety classifier blocks risky actions). "
                    "Codex is read-only because its sandbox is unavailable, so Claude should do "
                    "the hands-on work.",
}


# ---------------------------------------------------------------- storage
def chat_path(cid):
    return os.path.join(CHATS_DIR, f"{cid}.json")


def save(chat):
    chat["updated"] = time.time()
    tmp = chat_path(chat["id"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(chat, f, indent=1)
    os.replace(tmp, chat_path(chat["id"]))


def load_all():
    for name in os.listdir(CHATS_DIR):
        if name.endswith(".json"):
            try:
                with open(os.path.join(CHATS_DIR, name)) as f:
                    c = json.load(f)
                chats[c["id"]] = c
                typing[c["id"]] = {b: False for b in BOTS}
            except Exception as e:
                print("could not load", name, e)


def new_chat(folder, mode="talk", rounds=0, title=""):
    cid = uuid.uuid4().hex[:12]
    chat = {
        "id": cid, "title": title or "New chat", "folder": folder, "mode": mode,
        "rounds": rounds, "created": time.time(), "updated": time.time(),
        "messages": [],
        "seen": {b: 0 for b in BOTS},          # index of first message each bot hasn't seen
        "sessions": {"claude": None, "codex": None},
    }
    with lock:
        chats[cid] = chat
        typing[cid] = {b: False for b in BOTS}
        save(chat)
    return chat


def add_msg(chat, who, text, **extra):
    """Append a message; returns its index."""
    with lock:
        chat["messages"].append({"who": who, "text": text, "ts": time.time(), **extra})
        if who == "user" and chat["title"] == "New chat":
            chat["title"] = text.strip().splitlines()[0][:50]
        save(chat)
        return len(chat["messages"]) - 1


def diff_path(cid, idx):
    return os.path.join(CHATS_DIR, f"{cid}.diffs", f"{idx}.diff")


# ---------------------------------------------------------------- prompts
def intro(bot, chat):
    name, other = ("Claude", "Codex (OpenAI's coding agent)") if bot == "claude" \
        else ("Codex", "Claude (Anthropic's Claude Code)")
    mode = ABILITY_TEXT[abilities(chat)]
    return (
        f"You are {name}, a member of a group chat with a human user and {other}. "
        f"Project folder: {chat['folder']}. {mode}\n"
        "Messages arrive labeled [User], [Claude] or [Codex]. Both AIs get each user message "
        "at the same time and answer independently; afterwards you may see each other's answers. "
        "Reply as a group-chat member: concise and conversational, addressing the user or the "
        "other AI directly. Never prefix your reply with your own name. "
        f"If you are shown only the other AI's reply and have nothing useful to add, "
        f"reply with exactly {PASS}."
        + (" Note: in your workspace-write sandbox, empty read-only .git/.codex/.agents/.aws "
           "folders can appear in the project; they are sandbox mount points, not real files, "
           "so don't report them as project contents." if bot == "codex" else "")
    )


# ---------------------------------------------------------------- skills
HOME = os.path.expanduser("~")
_skill_cache = {}  # folder -> (time, list)


def frontmatter(path):
    meta = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read(8000).splitlines()
    except OSError:
        return meta
    if not lines or lines[0].strip() != "---":
        return meta
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([\w-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            meta[key] = "" if val in (">", ">-", "|", "|-") else val.strip("\"'")
        elif key and line.startswith((" ", "\t")):  # YAML folded/indented continuation
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta


def ancestors(folder):
    """folder and its parents, stopping at $HOME (inclusive) or /."""
    out, cur = [], os.path.abspath(folder)
    while True:
        out.append(cur)
        if cur in (HOME, "/"):
            break
        cur = os.path.dirname(cur)
    return out


def scan_skill_dirs(roots, recursive=False):
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        if recursive:
            for dp, dns, fns in os.walk(root):
                if "SKILL.md" in fns:
                    found.append(os.path.join(dp, "SKILL.md"))
                    dns[:] = []
        else:
            for name in sorted(os.listdir(root)):
                p = os.path.join(root, name, "SKILL.md")
                if os.path.isfile(p):
                    found.append(p)
    return found


def claude_skill_files(folder):
    roots = [os.path.join(HOME, ".claude", "skills")]
    roots += [os.path.join(d, ".claude", "skills") for d in ancestors(folder) if d != HOME]
    files = scan_skill_dirs(roots)
    try:
        with open(os.path.join(HOME, ".claude", "plugins", "installed_plugins.json")) as f:
            plugins = json.load(f).get("plugins", {})
        for entries in plugins.values():
            for e in entries:
                files += scan_skill_dirs([os.path.join(e["installPath"], "skills")], recursive=True)
    except Exception:
        pass
    cmds = []
    for d in [os.path.join(HOME, ".claude", "commands")] + \
             [os.path.join(a, ".claude", "commands") for a in ancestors(folder) if a != HOME]:
        if os.path.isdir(d):
            cmds += [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".md")]
    return files, cmds


def codex_skill_files(folder):
    roots = [os.path.join(HOME, ".codex", "skills"), os.path.join(HOME, ".codex", "skills", ".system"),
             os.path.join(HOME, ".agents", "skills")]
    roots += [os.path.join(d, ".agents", "skills") for d in ancestors(folder) if d != HOME]
    return scan_skill_dirs(roots)


def list_skills(folder):
    hit = _skill_cache.get(folder)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    skills = {}

    def put(path, bot, is_cmd=False):
        meta = frontmatter(path)
        name = os.path.splitext(os.path.basename(path))[0] if is_cmd else \
            meta.get("name") or os.path.basename(os.path.dirname(path))
        if meta.get("user-invocable", "").lower() == "false":
            return
        s = skills.setdefault(name, {"name": name, "description": "", "hint": "", "bots": []})
        s["description"] = s["description"] or meta.get("description", "")
        s["hint"] = s["hint"] or meta.get("argument-hint", "")
        if bot not in s["bots"]:
            s["bots"].append(bot)

    files, cmds = claude_skill_files(folder)
    for p in files:
        put(p, "claude")
    for p in cmds:
        put(p, "claude", is_cmd=True)
    for p in codex_skill_files(folder):
        put(p, "codex")
    for s in skills.values():
        s["bots"].sort()
    result = sorted(skills.values(), key=lambda s: s["name"])
    _skill_cache[folder] = (time.time(), result)
    return result


SKILL_RE = re.compile(r"^\s*(?:@(?:claude|codex)\b[\s,:]*)*/([\w:.-]+)(.*)$", re.I | re.S)


def parse_skill(text, folder):
    """'/grill-me my plan' or '@codex /grill-me my plan' -> {'name','args','bots'}."""
    m = SKILL_RE.match(text)
    if not m:
        return None
    name = m.group(1)
    for s in list_skills(folder):
        if s["name"].lower() == name.lower():
            return {"name": s["name"], "args": m.group(2).strip(), "bots": s["bots"]}
    return None


def mentioned(text):
    """Bots @-mentioned anywhere in text, e.g. '@claude', 'hey @Codex,'."""
    found = {m.lower() for m in re.findall(r"(?<![\w@])@(claude|codex)\b", text, re.I)}
    return [b for b in BOTS if b in found]


def unseen_for(chat, bot):
    """(indices of the user messages among them, new messages from others since this
    bot's last turn); marks them seen."""
    with lock:
        start = chat["seen"][bot]
        msgs = chat["messages"][start:]
        chat["seen"][bot] = len(chat["messages"])
        save(chat)
    users = [start + k for k, m in enumerate(msgs) if m["who"] == "user"]
    # a bot never re-reads its own messages, except a chat summary (its fresh session needs it)
    return users, [m for m in msgs
                   if m["who"] != bot or (m.get("meta") or {}).get("role") == "summary"]


def label(m):
    if (m.get("meta") or {}).get("role") == "summary":
        return "Chat summary"
    return m["who"].capitalize()


def format_msgs(msgs):
    return "\n\n".join(f"[{label(m)}]: {m['text']}" for m in msgs)


def route(chat, m):
    """Who a user message goes to: (bots that answer it, lead bot if Lead + Review)."""
    sk, at = m.get("skill"), mentioned(m["text"])
    lead = chat.get("lead") if chat.get("lead") in BOTS else None
    # @claude / @codex anywhere addresses only those bots; the other one stays silent and
    # reads it as context later. A /skill goes to whoever has it. With a lead set, a
    # plain message goes to the lead and the other one reviews.
    led = lead if lead and not at and not sk else None
    return (at or (sk["bots"] if sk else [led] if led else list(BOTS))), led


def set_status(chat, idxs, bot, st, only=None):
    """Per-message delivery status shown under your messages (queued/running/done…)."""
    with lock:
        for i in idxs:
            m = chat["messages"][i]
            if bot in m.get("to", []) and (only is None or m.get("status", {}).get(bot) in only):
                m.setdefault("status", {})[bot] = st
        save(chat)


def outcome(reply):
    r = (reply or "").strip()
    if r.strip(".").upper() == PASS:
        return "passed"
    for st in ("error", "stopped", "timed out"):
        if r.startswith(f"({st}"):
            return st
    return "done"


# ---------------------------------------------------------------- bot calls
# Both CLIs stream JSON events. We read them as they arrive to show live activity
# ("editing server.py", "$ pytest") and keep the process handle so Stop can kill it.
procs = {}      # (chat id, bot) -> running Popen
activity = {}   # chat id -> {bot: {"since": t, "steps": [recent activity lines]}}
halted = set()  # chat ids where the user pressed Stop; cleared by their next message
EDIT_NAMES = ("Edit", "MultiEdit", "Write", "NotebookEdit")


def kill(p, reason):
    """Stop an agent run: SIGTERM its whole process group, SIGKILL if it lingers."""
    if p.poll() is not None:
        return
    p.gc_reason = reason
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


def stream(chat, bot, cmd, on_event):
    """Run an agent CLI, passing each JSON line of its stdout to on_event.
    Returns (exit code, stderr tail, None | "stopped" | "timed out")."""
    key = (chat["id"], bot)
    with tempfile.TemporaryFile(mode="w+") as err:
        p = subprocess.Popen(cmd, cwd=chat["folder"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=err, text=True,
                             start_new_session=True)  # own process group, for Stop
        p.gc_reason = None
        procs[key] = p
        if chat["id"] in halted:  # Stop was pressed while this was starting
            kill(p, "stopped")
        timer = threading.Timer(BOT_TIMEOUT, kill, args=(p, "timed out"))
        timer.start()
        try:
            for line in p.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                try:
                    on_event(ev)
                except Exception as e:  # a surprising event must not lose the reply
                    print("event error", bot, e)
            p.wait()
        finally:
            timer.cancel()
            procs.pop(key, None)
        err.seek(0)
        return p.returncode, err.read()[-800:].strip(), p.gc_reason


def step(chat, bot, text):
    text = " ".join(str(text).split())
    if not text:
        return
    with lock:
        a = activity.get(chat["id"], {}).get(bot)
        if a is not None:
            a["steps"] = (a["steps"] + [text[:160]])[-6:]


def set_text(chat, bot, text, append=False):
    """Live preview of the reply being written (Claude: token by token; Codex: per message)."""
    with lock:
        a = activity.get(chat["id"], {}).get(bot)
        if a is not None:
            a["text"] = ((a.get("text") or "") + text if append else text)[-20000:]


def rel(path, folder):
    if not path:
        return ""
    r = os.path.relpath(path, folder) if os.path.isabs(path) else path
    return path if r.startswith("..") else r


def add_file(out, path):
    if path and path not in out["files"]:
        out["files"].append(path)


def describe_tool(name, inp, folder):
    path = rel(inp.get("file_path") or inp.get("notebook_path") or inp.get("path"), folder)
    if name == "Read":
        return f"📖 reading {path}"
    if name in EDIT_NAMES:
        return f"✏️ editing {path}"
    if name == "Bash":
        return "$ " + (inp.get("description") or (inp.get("command") or "").strip().split("\n")[0])
    if name in ("Grep", "Glob"):
        return f"🔎 searching for {inp.get('pattern', '')}" + (f" in {path}" if path else "")
    if name == "WebSearch":
        return f"🌐 searching the web: {inp.get('query', '')}"
    if name == "WebFetch":
        return f"🌐 reading {inp.get('url', '')}"
    if name == "Skill":
        return f"🧩 using skill {inp.get('skill', '')}"
    if name in ("Agent", "Task"):
        return f"🤖 subagent: {inp.get('description', '')}"
    if name == "TodoWrite":
        return "📝 updating its to-do list"
    return f"🔧 {name}"


def first_line(text):
    return "💬 " + next((l for l in text.strip().splitlines() if l.strip()), "")


def run_claude(chat, prompt, ab):
    """-> (reply text, meta). meta: secs, cost, tokens, files changed."""
    sid = chat["sessions"]["claude"]
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--append-system-prompt", intro("claude", chat)]
    if sid:
        cmd += ["--resume", sid]
    else:
        sid = str(uuid.uuid4())
        cmd += ["--session-id", sid]
    if ab in ("auto", "auto-limited"):
        cmd += ["--permission-mode", "auto"]
    elif ab == "work":
        cmd += ["--permission-mode", "acceptEdits", "--settings", CLAUDE_SANDBOX,
                "--allowedTools", *WORK_TOOLS]
    elif ab == "review-auto":  # reviewer: auto mode minus the file-editing tools
        cmd += ["--permission-mode", "auto", "--disallowedTools", *EDIT_NAMES]
    elif ab == "review-work":  # reviewer: sandboxed commands (tests, git diff), no edit tools
        cmd += ["--permission-mode", "dontAsk", "--settings", CLAUDE_SANDBOX,
                "--allowedTools", *READ_TOOLS, "Bash", "--disallowedTools", *EDIT_NAMES]
    elif ab == "edit-only":
        # acceptEdits auto-approves edits only inside the working folder; anything
        # else would need a prompt, which -p can't show, so it is denied.
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", *READ_TOOLS,
                "--disallowedTools", "Bash"]
    else:
        cmd += ["--permission-mode", "dontAsk", "--allowedTools", *READ_TOOLS]
    cmd += ["--", prompt]
    out = {"files": [], "result": None, "started": False}

    def on_event(ev):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            out["started"] = True
        elif t == "stream_event":  # token-by-token text, for the live preview
            e = ev.get("event") or {}
            if e.get("type") == "content_block_start" and \
                    (e.get("content_block") or {}).get("type") == "text":
                set_text(chat, "claude", "")
            elif e.get("type") == "content_block_delta" and \
                    (e.get("delta") or {}).get("type") == "text_delta":
                set_text(chat, "claude", e["delta"].get("text", ""), append=True)
        elif t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    step(chat, "claude", describe_tool(c.get("name", ""), inp, chat["folder"]))
                    if c.get("name") in EDIT_NAMES:
                        add_file(out, rel(inp.get("file_path") or inp.get("notebook_path"),
                                          chat["folder"]))
                elif c.get("type") == "text" and (c.get("text") or "").strip():
                    step(chat, "claude", first_line(c["text"]))
        elif t == "result":
            out["result"] = ev

    code, err, why = stream(chat, "claude", cmd, on_event)
    if out["started"]:  # the session exists now (even if stopped), so keep resuming it
        with lock:
            chat["sessions"]["claude"] = sid
            save(chat)
    res = out["result"] or {}
    u = res.get("usage") or {}
    meta = {"files": out["files"], "cost": res.get("total_cost_usd"),
            "tokens": [u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                       + u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0)]
            if u else None}
    if why:
        return f"({why})", meta
    text = (res.get("result") or "").strip()
    if res and not res.get("is_error") and text:
        return text, meta
    return f"(error) {(text or err or f'exit code {code}')[-800:]}", meta


def run_codex(chat, prompt, ab):
    sid = chat["sessions"]["codex"]
    sandbox = "workspace-write" if ab in ("work", "auto") else "read-only"
    conf = ["-c", f'sandbox_mode="{sandbox}"']
    if ab == "auto":
        # Codex's "approve for me": when the sandbox blocks something (internet, a
        # folder outside the project...), Codex asks for escalation and its
        # automatic reviewer (guardian) approves or denies it, like Claude's auto mode.
        conf += ["-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="auto_review"']
    else:
        conf += ["-c", 'approval_policy="never"']
    out = {"files": [], "usage": None}

    def on_event(ev):
        t, it = ev.get("type"), ev.get("item") or {}
        kind = it.get("type")
        if t == "thread.started" and not sid:  # save right away so Stop keeps it resumable
            with lock:
                chat["sessions"]["codex"] = ev.get("thread_id")
                save(chat)
        elif t == "turn.completed":
            out["usage"] = ev.get("usage")
        elif t == "item.started" and kind == "command_execution":
            cmd = it.get("command") or ""
            m = re.fullmatch(r"/bin/(?:ba|z)?sh -lc (['\"])(.*)\1", cmd, re.S)
            step(chat, "codex", "$ " + (m.group(2) if m else cmd).split("\n")[0])
        elif t == "item.started" and kind == "reasoning":
            step(chat, "codex", "💭 thinking")
        elif t == "item.started" and kind == "web_search":
            step(chat, "codex", f"🌐 searching the web: {it.get('query', '')}")
        elif t == "item.started" and kind == "mcp_tool_call":
            step(chat, "codex", f"🔧 {it.get('server', '')}.{it.get('tool', '')}")
        elif t == "item.completed" and kind == "file_change":
            for c in it.get("changes") or []:
                p = rel(c.get("path"), chat["folder"])
                add_file(out, p)
                step(chat, "codex", f"✏️ editing {p}")
        elif t == "item.completed" and kind == "agent_message" and (it.get("text") or "").strip():
            step(chat, "codex", first_line(it["text"]))
            set_text(chat, "codex", it["text"])

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        outfile = tf.name
    try:
        if sid:
            cmd = ["codex", "exec", "--skip-git-repo-check", "--json", *conf,
                   "-o", outfile, "resume", sid, prompt]
        else:
            cmd = ["codex", "exec", "--skip-git-repo-check", "--json", *conf,
                   "-C", chat["folder"], "-o", outfile, intro("codex", chat) + "\n\n" + prompt]
        code, err, why = stream(chat, "codex", cmd, on_event)
        with open(outfile) as f:
            text = f.read().strip()
    finally:
        os.unlink(outfile)
    u = out["usage"] or {}
    meta = {"files": out["files"], "cost": None,
            "tokens": [u.get("input_tokens", 0), u.get("output_tokens", 0)] if u else None}
    if why:
        return f"({why})", meta
    return (text or f"(error) {(err or f'exit code {code}')[-800:]}"), meta


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", ".next", "dist", "build", ".cache"}
runs = {}  # chat id -> {bot: [start, end or None]} of the latest run, to spot overlaps


MAX_DIFF_FILE = 256 * 1024     # files above this get no diff (just "changed")
CONTENT_BUDGET = 16 * 1024 ** 2  # max text kept in memory per snapshot
MAX_DIFF = 60 * 1024            # max diff stored per reply


def read_text(path):
    try:
        if os.path.getsize(path) > MAX_DIFF_FILE:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return None if b"\0" in data[:4096] else data.decode("utf-8", "replace")


def snapshot(folder, limit=20000):
    """({path: mtime}, {path: text}) of the project's files, or None if it's too big to
    scan quickly. Catches edits the tool events miss (e.g. a file written by a shell
    command) and keeps the text so each reply can show a diff."""
    mt, text, budget = {}, {}, CONTENT_BUDGET
    for dp, dns, fns in os.walk(folder):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and os.path.join(dp, d) != DATA_DIR]
        for f in fns:
            if len(mt) >= limit:
                return None
            path = os.path.join(dp, f)
            try:
                st = os.stat(path)
            except OSError:
                continue
            mt[path] = st.st_mtime_ns
            if st.st_size <= min(MAX_DIFF_FILE, budget):
                t = read_text(path)
                if t is not None:
                    text[path] = t
                    budget -= st.st_size
    mt.pop(LOG_FILE, None)
    return mt, text


def changed_files(before, after, folder):
    b, a = before[0], after[0]
    out = [rel(p, folder) for p in a if b.get(p) != a[p]]
    return sorted(out) + sorted(rel(p, folder) + " (deleted)" for p in b if p not in a)


def make_diff(before, files, folder):
    """Unified diff of `files` (relative names) against the snapshot taken before the run."""
    parts, total = [], 0
    for f in files:
        name = f[:-len(" (deleted)")] if f.endswith(" (deleted)") else f
        path = name if os.path.isabs(name) else os.path.join(folder, name)
        existed = path in before[0]
        old = before[1].get(path, "" if not existed else None)
        new = "" if f.endswith(" (deleted)") else read_text(path)
        if old is None or new is None:
            part = f"--- {name}\n(binary or large file: no diff)\n"
        else:
            part = "".join(difflib.unified_diff(
                old.splitlines(True), new.splitlines(True),
                "/dev/null" if not existed else f"a/{name}",
                "/dev/null" if f.endswith(" (deleted)") else f"b/{name}"))
            if part and not part.endswith("\n"):
                part += "\n"
        if total + len(part) > MAX_DIFF:
            parts.append(f"… diff truncated ({len(files)} files changed)\n")
            break
        parts.append(part)
        total += len(part)
    return "".join(parts)


def review_ability(ab):
    """Reviewer turns may run commands (tests, git diff) but get no file-editing tools."""
    return {"auto": "review-auto", "auto-limited": "review-auto",
            "work": "review-work"}.get(ab, "talk")


def ask(chat, bot, extra="", readonly=False, role=None):
    """Give `bot` everything it hasn't seen yet and post its reply.
    readonly: reviewer turn, no file-editing tools whatever the chat's mode."""
    users, msgs = unseen_for(chat, bot)
    if not msgs:
        return None
    set_status(chat, users, bot, "running")
    ab = abilities(chat)
    note = ""
    with lock:
        told = chat.setdefault("told", {})
        if told.get(bot) not in (None, ab):  # mode changed since this bot's last turn
            note = f"[System]: Permissions changed. {ABILITY_TEXT[ab]}\n\n"
        told[bot] = ab
    last_user = next((m for m in reversed(msgs) if m["who"] == "user"), None)
    sk = last_user.get("skill") if last_user else None
    if sk and bot not in sk["bots"]:
        sk = None
    if sk and bot == "claude":
        # Claude Code runs a skill when the prompt *starts* with /name (like the terminal)
        ctx = note + format_msgs([m for m in msgs if m is not last_user])
        prompt = f"/{sk['name']} {sk['args']}".strip()
        if ctx.strip():
            prompt += "\n\n(Group-chat messages since your last turn, for context:\n" + ctx + ")"
    elif sk and bot == "codex":
        # Codex runs a skill when the message mentions $name
        shown = [dict(m, text=re.sub(r"/" + re.escape(sk["name"]) + r"\b", "$" + sk["name"],
                                     m["text"], count=1, flags=re.I))
                 if m is last_user else m for m in msgs]
        prompt = note + format_msgs(shown) + extra
    else:
        prompt = note + format_msgs(msgs) + extra
    before = snapshot(chat["folder"])
    start = time.time()
    with lock:
        mine = runs.setdefault(chat["id"], {})[bot] = [start, None]
        typing[chat["id"]][bot] = True
        activity.setdefault(chat["id"], {})[bot] = {
            "since": start, "steps": [],
            "role": {"summary": "summarizing"}.get(role, "reviewing" if readonly else "")}
    meta = {}
    try:
        reply, meta = (run_claude if bot == "claude" else run_codex)(
            chat, prompt, review_ability(ab) if readonly else ab)
    except Exception as e:
        reply = f"(error) {e}"
    finally:
        with lock:
            typing[chat["id"]][bot] = False
            activity.get(chat["id"], {}).pop(bot, None)
    with lock:
        mine[1] = time.time()
        o = runs[chat["id"]].get(other(bot))
        overlap = bool(o) and o[0] < mine[1] and (o[1] is None or o[1] > start)
    if before is not None and not overlap:  # alone in the folder: every change is this bot's
        after = snapshot(chat["folder"])
        if after is not None:
            meta["files"] = list(dict.fromkeys((meta.get("files") or [])
                                               + changed_files(before, after, chat["folder"])))
    set_status(chat, users, bot, outcome(reply))
    if outcome(reply) == "passed":
        return PASS
    diff = make_diff(before, meta["files"], chat["folder"]) \
        if before is not None and meta.get("files") else ""
    meta = {k: v for k, v in meta.items() if v}
    meta["secs"] = round(time.time() - start)
    meta["for"] = users  # which of your messages this answers (for Retry)
    if role or readonly:
        meta["role"] = role or "review"
    if readonly and meta.get("files"):
        meta["warn"] = "the reviewer changed files"
    if diff:
        meta["diff"] = [diff.count("\n+") - diff.count("\n+++"),
                        diff.count("\n-") - diff.count("\n---")]
    idx = add_msg(chat, bot, reply, meta=meta)
    if diff:
        os.makedirs(os.path.dirname(diff_path(chat["id"], idx)), exist_ok=True)
        with open(diff_path(chat["id"], idx), "w") as f:
            f.write(diff)
    return reply


def parallel(chat, bots, extra="", extras=None):
    results = {}
    def go(b):
        results[b] = ask(chat, b, (extras or {}).get(b, extra))
    ts = [threading.Thread(target=go, args=(b,)) for b in bots]
    for t in ts: t.start()
    for t in ts: t.join()
    return results


ROUND_NOTE = (f"\n\n(Discussion round: respond to the other AI if useful, "
              f"otherwise reply exactly {PASS}.)")
HANDOFF_NOTE = ("\n\n(The other AI addressed you directly above. Answer it, or do what it "
                "asks if that fits what the user asked for; otherwise reply exactly " + PASS + ".)")
_ADDRESS = {b: re.compile(rf"(?:^|[.!?]\s+|\n\s*)(?:hey\s+|ok\s+|so\s+)?{b}\s*[,:]", re.I)
            for b in BOTS}


def addressed(text, bot):
    """Does this text speak to `bot` directly? '@codex …' or 'Codex, please …'."""
    return bot in mentioned(text) or bool(_ADDRESS[bot].search(text))


def other(bot):
    return "codex" if bot == "claude" else "claude"


def pending(chat):
    """(bots that still owe a reply to SOME user message, skill of the latest one,
    lead bot if the latest one goes to the lead for Lead + Review).
    Checks every user message, not just the latest, so '@codex X' followed quickly by
    '@claude Y' still gets Codex's answer to X."""
    with lock:
        targets, sk, led = [], None, None
        for i, m in enumerate(chat["messages"]):
            if m["who"] != "user":
                continue
            sk = m.get("skill")
            want, led = route(chat, m)
            targets += [b for b in want if chat["seen"][b] <= i and b not in targets]
        return [b for b in BOTS if b in targets], sk, led


def user_waiting(chat):
    with lock:  # a new user message (or Stop) takes priority over AI-to-AI talk
        return chat["id"] in halted or (
            bool(chat["messages"]) and chat["messages"][-1]["who"] == "user")


REVIEW_NOTE = ("\n\n(Review turn: {lead} is the lead and just did the work above; you are the "
               "reviewer. Check its changes{files} against what the user asked: bugs, missed "
               "requirements, risky edits. Read the files, and where your permissions allow, run "
               "read-only checks such as the tests or git diff, but do NOT modify any files this "
               "turn. Say concretely what to fix, or reply \"LGTM\" plus one line on what you "
               "checked. Don't redo the work.)")


LEAD_NOTE = ("\n\n(Lead + Review: you are the lead for this message. Only you received it; "
             "you do the work, then {reviewer} reviews it read-only after you. "
             "No need to coordinate edits with {reviewer}.)")


def review(chat, lead):
    """Lead + Review: the other AI reviews what the lead just did, read-only."""
    with lock:
        last = next((m for m in reversed(chat["messages"]) if m["who"] == lead), None)
    if user_waiting(chat) or not last or last["text"].startswith("("):  # (error)/(stopped)…
        set_status(chat, (last.get("meta") or {}).get("for", []) if last else [],
                   other(lead), "skipped", only=("queued",))
        return
    files = (last.get("meta") or {}).get("files") or []
    ask(chat, other(lead), REVIEW_NOTE.format(
        lead=lead.capitalize(), files=f" (files changed: {', '.join(files)})" if files else ""),
        readonly=True)


def discuss(chat, bots):
    """Discussion rounds, taking turns: each AI sees what the other just said."""
    for _ in range(int(chat.get("rounds", 0))):
        spoke = False
        for b in bots:
            if user_waiting(chat):
                return
            spoke |= ask(chat, b, ROUND_NOTE) not in (None, PASS)
        if not spoke:
            return


def handoffs(chat, skip):
    """A reply that speaks to the other AI reaches it when the user's message was
    @-addressed to only one of them (e.g. read-only Codex asking Claude to write files).
    Bots in `skip` already answered the user; for them only Discussion rounds apply, so
    'Codex, your turn' in a reply to both doesn't trigger a second answer."""
    for _ in range(MAX_HANDOFFS):
        if user_waiting(chat):
            return
        with lock:
            to = [b for b in BOTS if b not in skip
                  and any(m["who"] not in ("user", b) and addressed(m["text"], b)
                         for m in chat["messages"][chat["seen"][b]:])]
        if not to:
            return
        for b in to:
            ask(chat, b, HANDOFF_NOTE)


def worker(chat, first=None):
    """Processes a chat until there is nothing new to answer. `first`: a task to run
    before that (Retry, Compact)."""
    try:
        if first:
            first()
        while True:
            with lock:
                if chat["id"] in halted:  # Stop: drop the rest of this turn
                    for i, m in enumerate(chat["messages"]):
                        for b, st in (m.get("status") or {}).items():
                            if st in ("queued", "running"):
                                m["status"][b] = "stopped"
                    for b in BOTS:
                        chat["seen"][b] = len(chat["messages"])
                    save(chat)
                    return
            targets, sk, led = pending(chat)
            if not targets:
                return
            res = parallel(chat, targets, extras={led: LEAD_NOTE.format(
                reviewer=other(led).capitalize())} if led else None)
            if led in targets and res.get(led) not in (None, PASS):
                review(chat, led)
            elif len(targets) == 2 and not sk:  # no auto back-and-forth during a skill
                discuss(chat, targets)
            handoffs(chat, targets + ([other(led)] if led else []))
    finally:
        with lock:
            workers.pop(chat["id"], None)


def kick(chat, first=None):
    with lock:
        if chat["id"] in workers:
            return False
        t = threading.Thread(target=worker, args=(chat, first), daemon=True)
        workers[chat["id"]] = t
        t.start()
        return True


def retry(chat, idx):
    """Ask the same bot again for a reply that failed (error / stopped / timed out)."""
    m = chat["messages"][idx]
    bot, meta = m["who"], m.get("meta") or {}
    users = meta.get("for") or []
    with lock:
        m["retried"] = True
        if users:
            chat["seen"][bot] = min(chat["seen"][bot], min(users))
        save(chat)
    set_status(chat, users, bot, "queued")
    if meta.get("role") == "review":
        return review(chat, other(bot))
    is_lead = chat.get("lead") == bot
    if ask(chat, bot, LEAD_NOTE.format(reviewer=other(bot).capitalize()) if is_lead else "") \
            not in (None, PASS) and is_lead:
        review(chat, bot)


SUMMARY_NOTE = ("\n\n(The user pressed Compact. Write a summary of this whole group chat that "
                "you and the other AI can continue from in fresh sessions: the goal, decisions, "
                "what was done (files changed), open questions and next steps. Markdown bullets, "
                "at most ~300 words. Reply with the summary only.)")


def compact(chat):
    """Summarize the chat, then start fresh Claude and Codex sessions from the summary,
    so long chats stop re-reading their whole history on every reply."""
    with lock:
        chat["messages"].append({"who": "system", "text": "Compact: summarize this chat.",
                                 "ts": time.time(), "hidden": True})
        save(chat)
    reply = ask(chat, "claude", SUMMARY_NOTE, readonly=True, role="summary")
    if reply in (None, PASS) or reply.startswith("("):
        return
    with lock:
        old = chat["sessions"]
        chat.setdefault("old_sessions", []).append(old)
        chat["sessions"] = {"claude": None, "codex": None}
        chat["told"] = {}
        idx = len(chat["messages"]) - 1  # the summary: both new sessions start from it
        for b in BOTS:
            chat["seen"][b] = idx
        chat["messages"][idx]["meta"]["old_sessions"] = old
        save(chat)


def export_md(chat):
    names = {"user": "You", "claude": "Claude", "codex": "Codex"}
    out = [f"# {chat['title']}", "",
           f"Folder: `{chat['folder']}` · mode: {chat['mode']} · exported "
           f"{time.strftime('%Y-%m-%d %H:%M')}", ""]
    for m in chat["messages"]:
        if m.get("hidden"):
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("ts", 0)))
        role = {"review": " (review)", "summary": " (chat summary)"}.get(
            (m.get("meta") or {}).get("role"), "")
        out += [f"## {names.get(m['who'], m['who'])}{role} · {when}", "", m["text"], ""]
    return "\n".join(out)


# ---------------------------------------------------------------- http
PAGE_FILE = os.path.join(HERE, "index.html")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def trusted(self, post=False):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return False
        if post:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in ALLOWED_ORIGINS:
                return False
            if not secrets.compare_digest(self.headers.get("X-Groupchat-Token", ""), TOKEN):
                return False
        return True

    def do_GET(self):
        if not self.trusted():
            return self.send_json({"error": "forbidden"}, 403)
        p = self.path.split("?")[0]
        if p == "/api/chats":
            with lock:
                lst = sorted(({k: c[k] for k in ("id", "title", "updated", "folder", "mode")}
                              for c in chats.values()), key=lambda c: -c["updated"])
            return self.send_json({"chats": lst, "home": os.path.expanduser("~"),
                                   "sandbox": sandbox_ok()})
        m = re.fullmatch(r"/api/chats/(\w+)/skills", p)
        if m:
            c = chats.get(m.group(1))
            if not c:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json({"skills": list_skills(c["folder"])})
        m = re.fullmatch(r"/api/chats/(\w+)/diff/(\d+)", p)
        if m:
            try:
                with open(diff_path(m.group(1), int(m.group(2)))) as f:
                    body = f.read().encode()
            except OSError:
                return self.send_json({"error": "no diff"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        m = re.fullmatch(r"/api/chats/(\w+)/export", p)
        if m and m.group(1) in chats:
            body = export_md(chats[m.group(1)]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="chat-{m.group(1)}.md"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        m = re.fullmatch(r"/api/chats/(\w+)", p)
        if m:
            c = chats.get(m.group(1))
            if not c:
                return self.send_json({"error": "not found"}, 404)
            with lock:
                return self.send_json({**c, "typing": typing[c["id"]],
                                       "activity": activity.get(c["id"], {}),
                                       "busy": c["id"] in workers})
        with open(PAGE_FILE, "rb") as f:
            body = f.read().replace(b"__GC_TOKEN__", TOKEN.encode())
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")  # the token changes every launch
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.trusted(post=True):
            return self.send_json({"error": "forbidden"}, 403)
        p = self.path
        try:
            d = self.body()
        except ValueError:
            return self.send_json({"error": "bad json"}, 400)
        if p == "/api/chats":
            folder = os.path.abspath(os.path.expanduser(d.get("folder") or "~"))
            if not os.path.isdir(folder):
                return self.send_json({"error": f"Folder does not exist: {folder}"}, 400)
            mode = d.get("mode") if d.get("mode") in ("talk", "work", "auto") else "talk"
            c = new_chat(folder, mode, int(d.get("rounds") or 0), (d.get("title") or "").strip())
            return self.send_json({"id": c["id"]})
        m = re.fullmatch(r"/api/chats/(\w+)/(send|settings|delete|stop|retry|compact)", p)
        c = chats.get(m.group(1)) if m else None
        if not c:
            return self.send_json({"error": "not found"}, 404)
        action = m.group(2)
        if action == "send":
            text = (d.get("text") or "").strip()
            if not text:
                return self.send_json({"error": "empty"}, 400)
            sk = parse_skill(text, c["folder"])
            with lock:
                halted.discard(c["id"])
            msg = {"text": text, **({"skill": sk} if sk else {})}
            want, led = route(c, msg)
            to = want + ([other(led)] if led else [])
            add_msg(c, "user", text, to=to, status={b: "queued" for b in to},
                    **({"skill": sk} if sk else {}))
            kick(c)
        elif action in ("retry", "compact"):
            if c["id"] in workers:
                return self.send_json({"error": "They're still working. Try again when they finish."}, 409)
            with lock:
                halted.discard(c["id"])
            if action == "retry":
                i = d.get("index")
                if not isinstance(i, int) or not 0 <= i < len(c["messages"]) \
                        or c["messages"][i]["who"] not in BOTS \
                        or outcome(c["messages"][i]["text"]) == "done":
                    return self.send_json({"error": "nothing to retry"}, 400)
                kick(c, lambda: retry(c, i))
            else:
                kick(c, lambda: compact(c))
        elif action == "stop":
            with lock:  # kill running agents and cancel the rest of this turn
                if c["id"] in workers:
                    halted.add(c["id"])
                running = [procs.get((c["id"], b)) for b in BOTS]
            for pr in running:
                if pr:
                    kill(pr, "stopped")
        elif action == "settings":
            with lock:
                if d.get("mode") in ("talk", "work", "auto"):
                    c["mode"] = d["mode"]
                if d.get("lead") in ("", "claude", "codex"):
                    c["lead"] = d["lead"] or None
                if "rounds" in d:
                    c["rounds"] = max(0, min(5, int(d["rounds"])))
                if d.get("title"):
                    c["title"] = d["title"].strip()[:80]
                save(c)
        elif action == "delete":
            with lock:
                chats.pop(c["id"], None)
                typing.pop(c["id"], None)
                try:
                    os.remove(chat_path(c["id"]))
                except FileNotFoundError:
                    pass
                shutil.rmtree(os.path.join(CHATS_DIR, f"{c['id']}.diffs"), ignore_errors=True)
        return self.send_json({"ok": True})


if __name__ == "__main__":
    print(f"Group chat on http://127.0.0.1:{PORT}  (data: {DATA_DIR})", flush=True)
    if not sys.stdout.isatty():  # started in the background: keep a log file instead
        sys.stdout = sys.stderr = open(LOG_FILE, "a", buffering=1)
    load_all()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
