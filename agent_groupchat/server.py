#!/usr/bin/env python3
"""Local group chat: you + your AI agents (Claude Code, Codex CLI) on one page.

Run:  python3 -m agent_groupchat   then open http://127.0.0.1:8765

- Many chats, each saved in ~/.agent-groupchat/chats/<id>.json (sidebar to reopen any of them).
- Each chat has any number of agents, including several copies of the same AI
  ("Claude 1", "Claude 2"). Each agent is a REAL CLI session (Claude Code, Codex),
  so skills, hooks, CLAUDE.md/AGENTS.md, memory and MCP config all load, and the
  sessions can also be continued from a terminal (claude -r / codex resume).
- Each chat has a project folder and a mode:
    talk : read-only (read files, search, use skills; no edits, no commands)
    work : may edit files and run commands, sandboxed to the chat's folder
- "Discussion rounds": after both answer you, they can reply to each other.
- @name addresses only that agent (@claude-2); @claude reaches every Claude copy.
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

from .adapters import ADAPTERS, RunContext
from .adapters.base import rel
from .skills import frontmatter

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
MAX_AGENTS = 12
LEGACY_AGENTS = [{"adapter": "claude"}, {"adapter": "codex"}]  # default, and chats from before agent lists
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
typing = {}     # id -> {bot: bool}
workers = {}    # id -> running worker thread

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


def ability_text(chat, bot):
    """What this agent may do in this chat's mode, in its adapter's own words."""
    ab = abilities(chat)
    text = adapter_of(chat, bot).mode_text(ab)
    if ab != "talk" and len(chat["agents"]) > 1:
        text += " Coordinate with the other AIs so you don't edit the same files."
    return text


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
                if "agents" not in c:  # saved before chats had agent lists: Claude + Codex
                    c["agents"] = make_agents(LEGACY_AGENTS)
                for a in c["agents"]:
                    c["seen"].setdefault(a["id"], 0)
                    c["sessions"].setdefault(a["id"], None)
                chats[c["id"]] = c
                typing[c["id"]] = {b: False for b in agent_ids(c)}
            except Exception as e:
                print("could not load", name, e)


def new_chat(folder, mode="talk", rounds=0, title="", agents=None):
    cid = uuid.uuid4().hex[:12]
    agents = make_agents(agents or LEGACY_AGENTS)
    ids = [a["id"] for a in agents]
    chat = {
        "id": cid, "title": title or "New chat", "folder": folder, "mode": mode,
        "rounds": rounds, "created": time.time(), "updated": time.time(),
        "agents": agents,
        "messages": [],
        "seen": {b: 0 for b in ids},          # index of first message each agent hasn't seen
        "sessions": {b: None for b in ids},
    }
    with lock:
        chats[cid] = chat
        typing[cid] = {b: False for b in ids}
        save(chat)
    return chat


# ---------------------------------------------------------------- agents
# A chat's agents: [{"id": "claude-2", "adapter": "claude", "name": "Claude 2", "model": None}].
# The id is fixed when the chat is made (messages, sessions and @mentions use it).
NAME_RE = re.compile(r"^[\w .'-]{1,40}$")
MODEL_RE = re.compile(r"^[\w.:/@-]{1,80}$")


def slug(name):
    return re.sub(r"[^\w]+", "-", name.lower()).strip("-_") or "agent"


def parse_agents(specs):
    """Validate agent specs from the page -> [{adapter, name, model}]. Raises ValueError."""
    if not isinstance(specs, list) or not 1 <= len(specs) <= MAX_AGENTS:
        raise ValueError(f"A chat needs 1 to {MAX_AGENTS} agents.")
    out = []
    for sp in specs:
        if not isinstance(sp, dict) or sp.get("adapter") not in ADAPTERS:
            raise ValueError(f"Unknown agent: {sp.get('adapter') if isinstance(sp, dict) else sp}")
        name = str(sp.get("name") or "").strip()
        if name and not (NAME_RE.match(name) and re.search(r"[^\W_]", name)):
            raise ValueError(f"Agent name {name!r}: up to 40 letters, digits, spaces, . ' - _")
        model = str(sp.get("model") or "").strip()
        if model and not MODEL_RE.match(model):
            raise ValueError(f"Model name {model!r} looks wrong (letters, digits, . : / @ - _ only).")
        out.append({"adapter": sp["adapter"], "name": name, "model": model or None})
    return out


def make_agents(specs):
    """Specs -> agents with unique ids and names. Unnamed copies of one AI are numbered
    (Claude 1, Claude 2); a single unnamed one is just "Claude" (id "claude")."""
    count = {}
    for sp in specs:
        count[sp["adapter"]] = count.get(sp["adapter"], 0) + 1
    nth, agents, names, ids = {}, [], set(), set()
    for sp in specs:
        ad = ADAPTERS[sp["adapter"]]
        nth[ad.id] = nth.get(ad.id, 0) + 1
        name = sp.get("name") or (ad.name if count[ad.id] == 1 else f"{ad.name} {nth[ad.id]}")
        base, k = name, 2
        while name.lower() in names:
            name, k = f"{base} {k}", k + 1
        names.add(name.lower())
        aid, k = slug(name), 2
        while aid in ids:
            aid, k = f"{slug(name)}-{k}", k + 1
        ids.add(aid)
        agents.append({"id": aid, "adapter": ad.id, "name": name, "model": sp.get("model")})
    return agents


def agent_ids(chat):
    return [a["id"] for a in chat["agents"]]


def agent(chat, aid):
    return next((a for a in chat["agents"] if a["id"] == aid), None)


def adapter_of(chat, aid):
    return ADAPTERS[agent(chat, aid)["adapter"]]


def display(chat, who):
    """Agent id -> its name ('Claude 2'); 'user' -> 'User'."""
    a = agent(chat, who)
    return a["name"] if a else who.capitalize()


def aliases(chat):
    """@-names -> agent ids. Each agent answers to its id and its name, with or without
    dashes (@claude-2, @claude2, @claude-tester); the AI's id reaches all its copies (@claude)."""
    out = {}
    for a in chat["agents"]:
        for k in (a["id"], slug(a["name"])):
            for v in (k, k.replace("-", "")):
                out.setdefault(v, [a["id"]])
    for a in chat["agents"]:
        if a["adapter"] not in out:
            out[a["adapter"]] = [b["id"] for b in chat["agents"] if b["adapter"] == a["adapter"]]
    return out


def reviewer_of(chat, lead):
    """Lead + Review: the agent after the lead in the chat's list reviews its work."""
    ids = agent_ids(chat)
    if lead not in ids or len(ids) < 2:
        return None
    return ids[(ids.index(lead) + 1) % len(ids)]


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
    me, ad = agent(chat, bot), adapter_of(chat, bot)
    others = [a for a in chat["agents"] if a["id"] != bot]
    desc = [f"{a['name']} ({ADAPTERS[a['adapter']].kind}"
            + (", a separate session of the same AI as you" if a["adapter"] == me["adapter"] else "")
            + ")" for a in others]
    with_whom = ("these AI agents: " + "; ".join(desc)) if others else "no other AI agents"
    labels = ", ".join(f"[{a['name']}]" for a in others)
    return (
        f"You are {me['name']}, a member of a group chat with a human user and {with_whom}. "
        f"Project folder: {chat['folder']}. {ability_text(chat, bot)}\n"
        "Messages arrive labeled [User]"
        + (f" or with the sender's name ({labels}). Every AI gets each user message at the same "
           "time and answers independently; afterwards you may see the others' answers. "
           if others else ". ")
        + "Reply as a group-chat member: concise and conversational, addressing the user"
        + (f" or the other AIs directly (@{others[0]['id']} reaches that one only)" if others else "")
        + ". Never prefix your reply with your own name. "
        f"If you are shown only other AIs' replies and have nothing useful to add, "
        f"reply with exactly {PASS}."
        + ad.intro_note
    )


# ---------------------------------------------------------------- skills
_skill_cache = {}  # folder -> (time, list)


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
        s = skills.setdefault(name, {"name": name, "description": "", "hint": "", "adapters": []})
        s["description"] = s["description"] or meta.get("description", "")
        s["hint"] = s["hint"] or meta.get("argument-hint", "")
        if bot not in s["adapters"]:
            s["adapters"].append(bot)

    for bot, adapter in ADAPTERS.items():
        files, cmds = adapter.skill_files(folder)
        for p in files:
            put(p, bot)
        for p in cmds:
            put(p, bot, is_cmd=True)
    for s in skills.values():
        s["adapters"].sort()
    result = sorted(skills.values(), key=lambda s: s["name"])
    _skill_cache[folder] = (time.time(), result)
    return result


SKILL_RE = re.compile(r"^\s*(?:@[\w-]+[\s,:]*)*/([\w:.-]+)(.*)$", re.I | re.S)


def parse_skill(text, folder):
    """'/grill-me my plan' or '@codex /grill-me my plan' -> {'name','args','adapters'}."""
    m = SKILL_RE.match(text)
    if not m:
        return None
    name = m.group(1)
    for s in list_skills(folder):
        if s["name"].lower() == name.lower():
            return {"name": s["name"], "args": m.group(2).strip(), "adapters": s["adapters"]}
    return None


def skill_adapters(sk):
    return sk.get("adapters", sk.get("bots", [])) if sk else []  # "bots": saved before M2


def mentioned(chat, text):
    """Agents @-mentioned anywhere in text, e.g. '@claude-2', 'hey @Codex,', in chat order."""
    al = aliases(chat)
    alts = "|".join(re.escape(k) for k in sorted(al, key=len, reverse=True))
    found = {b for k in re.findall(rf"(?<![\w@])@({alts})(?![\w-])", text, re.I)
             for b in al[k.lower()]}
    return [b for b in agent_ids(chat) if b in found]


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


def label(chat, m):
    if (m.get("meta") or {}).get("role") == "summary":
        return "Chat summary"
    return display(chat, m["who"])


def format_msgs(chat, msgs):
    return "\n\n".join(f"[{label(chat, m)}]: {m['text']}" for m in msgs)


def route(chat, m):
    """Who a user message goes to: (agents that answer it, lead if Lead + Review)."""
    ids = agent_ids(chat)
    sk, at = m.get("skill"), mentioned(chat, m["text"])
    lead = chat.get("lead") if reviewer_of(chat, chat.get("lead")) else None
    # @name anywhere addresses only those agents; the others stay silent and read it as
    # context later. A /skill goes to every agent whose AI has it. With a lead set, a
    # plain message goes to the lead and the next agent reviews.
    led = lead if lead and not at and not sk else None
    if at:
        return at, led
    if sk:
        return [b for b in ids if agent(chat, b)["adapter"] in skill_adapters(sk)], led
    return ([led] if led else ids), led


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
# Agent CLIs stream JSON events. Adapters read them as they arrive to show live activity
# ("editing server.py", "$ pytest"); stream() keeps the process handle so Stop can kill it.
procs = {}      # (chat id, bot) -> running Popen
activity = {}   # chat id -> {bot: {"since": t, "steps": [recent activity lines]}}
halted = set()  # chat ids where the user pressed Stop; cleared by their next message


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
            p.stdout.close()
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
    """Live preview of the reply being written (token by token or per message, per agent)."""
    with lock:
        a = activity.get(chat["id"], {}).get(bot)
        if a is not None:
            a["text"] = ((a.get("text") or "") + text if append else text)[-20000:]


def run_agent(chat, bot, prompt, ab):
    """Run one turn of `bot` through its adapter. -> (reply text, meta)."""
    def save_session(sid):
        with lock:
            chat["sessions"][bot] = sid
            save(chat)

    ctx = RunContext(
        folder=chat["folder"], session=chat["sessions"].get(bot), model=agent(chat, bot)["model"],
        system_prompt=intro(bot, chat), save_session=save_session,
        step=lambda text: step(chat, bot, text),
        preview=lambda text, append=False: set_text(chat, bot, text, append),
        stream=lambda cmd, on_event: stream(chat, bot, cmd, on_event))
    return adapter_of(chat, bot).run(ctx, prompt, ab)


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
            note = f"[System]: Permissions changed. {ability_text(chat, bot)}\n\n"
        told[bot] = ab
    last_user = next((m for m in reversed(msgs) if m["who"] == "user"), None)
    sk = last_user.get("skill") if last_user else None
    if sk and agent(chat, bot)["adapter"] not in skill_adapters(sk):
        sk = None
    syntax = adapter_of(chat, bot).skill_syntax
    if sk and syntax == "slash":
        # e.g. Claude Code runs a skill when the prompt *starts* with /name (like the terminal)
        ctx = note + format_msgs(chat, [m for m in msgs if m is not last_user])
        prompt = f"/{sk['name']} {sk['args']}".strip()
        if ctx.strip():
            prompt += "\n\n(Group-chat messages since your last turn, for context:\n" + ctx + ")"
    elif sk and syntax == "dollar":
        # e.g. Codex runs a skill when the message mentions $name
        shown = [dict(m, text=re.sub(r"/" + re.escape(sk["name"]) + r"\b", "$" + sk["name"],
                                     m["text"], count=1, flags=re.I))
                 if m is last_user else m for m in msgs]
        prompt = note + format_msgs(chat, shown) + extra
    else:
        prompt = note + format_msgs(chat, msgs) + extra
    before = snapshot(chat["folder"])
    start = time.time()
    with lock:
        mine = runs.setdefault(chat["id"], {})[bot] = [start, None]
        typing.setdefault(chat["id"], {})[bot] = True
        activity.setdefault(chat["id"], {})[bot] = {
            "since": start, "steps": [],
            "role": {"summary": "summarizing"}.get(role, "reviewing" if readonly else "")}
    meta = {}
    try:
        reply, meta = run_agent(chat, bot, prompt, review_ability(ab) if readonly else ab)
    except Exception as e:
        reply = f"(error) {e}"
    finally:
        with lock:
            typing[chat["id"]][bot] = False
            activity.get(chat["id"], {}).pop(bot, None)
    with lock:
        mine[1] = time.time()
        overlap = any(o[0] < mine[1] and (o[1] is None or o[1] > start)
                      for b, o in runs[chat["id"]].items() if b != bot)
    if before is not None and not overlap:  # alone in the folder: every change is this agent's
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


ROUND_NOTE = (f"\n\n(Discussion round: respond to the other AIs if useful, "
              f"otherwise reply exactly {PASS}.)")
HANDOFF_NOTE = ("\n\n(Another AI addressed you directly above. Answer it, or do what it "
                "asks if that fits what the user asked for; otherwise reply exactly " + PASS + ".)")
def addressed(chat, text, bot):
    """Does this text speak to `bot` directly? '@codex …' or 'Codex, please …'."""
    if bot in mentioned(chat, text):
        return True
    name = re.escape(display(chat, bot))
    return bool(re.search(rf"(?:^|[.!?]\s+|\n\s*)(?:hey\s+|ok\s+|so\s+)?{name}\s*[,:]", text, re.I))


def pending(chat):
    """(agents that still owe a reply to SOME user message, skill of the latest one,
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
            targets += [b for b in want if chat["seen"].get(b, 0) <= i and b not in targets]
        return [b for b in agent_ids(chat) if b in targets], sk, led


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


def review(chat, lead, reviewer=None):
    """Lead + Review: the reviewer (by default the agent after the lead) checks what the
    lead just did, read-only."""
    reviewer = reviewer or reviewer_of(chat, lead)
    if not reviewer:
        return
    with lock:
        last = next((m for m in reversed(chat["messages"]) if m["who"] == lead), None)
    if user_waiting(chat) or not last or last["text"].startswith("("):  # (error)/(stopped)…
        set_status(chat, (last.get("meta") or {}).get("for", []) if last else [],
                   reviewer, "skipped", only=("queued",))
        return
    files = (last.get("meta") or {}).get("files") or []
    ask(chat, reviewer, REVIEW_NOTE.format(
        lead=display(chat, lead), files=f" (files changed: {', '.join(files)})" if files else ""),
        readonly=True)


def discuss(chat, bots):
    """Discussion rounds, taking turns: each AI sees what the others just said."""
    for _ in range(int(chat.get("rounds", 0))):
        spoke = False
        for b in bots:
            if user_waiting(chat):
                return
            spoke |= ask(chat, b, ROUND_NOTE) not in (None, PASS)
        if not spoke:
            return


def handoffs(chat, skip):
    """A reply that speaks to another AI reaches it when the user's message was
    @-addressed to only some of them (e.g. read-only Codex asking Claude to write files).
    Agents in `skip` already answered the user; for them only Discussion rounds apply, so
    'Codex, your turn' in a reply to everyone doesn't trigger a second answer."""
    for _ in range(MAX_HANDOFFS):
        if user_waiting(chat):
            return
        with lock:
            to = [b for b in agent_ids(chat) if b not in skip
                  and any(m["who"] not in ("user", b) and addressed(chat, m["text"], b)
                         for m in chat["messages"][chat["seen"].get(b, 0):])]
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
                    for b in agent_ids(chat):
                        chat["seen"][b] = len(chat["messages"])
                    save(chat)
                    return
            targets, sk, led = pending(chat)
            if not targets:
                return
            rev = reviewer_of(chat, led) if led else None
            res = parallel(chat, targets, extras={led: LEAD_NOTE.format(
                reviewer=display(chat, rev))} if led else None)
            if led in targets and res.get(led) not in (None, PASS):
                review(chat, led)
            elif len(targets) >= 2 and not sk:  # no auto back-and-forth during a skill
                discuss(chat, targets)
            handoffs(chat, targets + ([rev] if rev else []))
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
        lead = chat.get("lead")
        return review(chat, lead, reviewer=bot) if lead in agent_ids(chat) and lead != bot else None
    rev = reviewer_of(chat, bot) if chat.get("lead") == bot else None
    if ask(chat, bot, LEAD_NOTE.format(reviewer=display(chat, rev)) if rev else "") \
            not in (None, PASS) and rev:
        review(chat, bot)


SUMMARY_NOTE = ("\n\n(The user pressed Compact. Write a summary of this whole group chat that "
                "you and the other AIs can continue from in fresh sessions: the goal, decisions, "
                "what was done (files changed), open questions and next steps. Markdown bullets, "
                "at most ~300 words. Reply with the summary only.)")


def compact(chat):
    """The first agent summarizes the chat, then every agent starts a fresh session from
    the summary, so long chats stop re-reading their whole history on every reply."""
    with lock:
        chat["messages"].append({"who": "system", "text": "Compact: summarize this chat.",
                                 "ts": time.time(), "hidden": True})
        save(chat)
    reply = ask(chat, agent_ids(chat)[0], SUMMARY_NOTE, readonly=True, role="summary")
    if reply in (None, PASS) or reply.startswith("("):
        return
    with lock:
        old = chat["sessions"]
        chat.setdefault("old_sessions", []).append(old)
        chat["sessions"] = {b: None for b in agent_ids(chat)}
        chat["told"] = {}
        idx = len(chat["messages"]) - 1  # the summary: all new sessions start from it
        for b in agent_ids(chat):
            chat["seen"][b] = idx
        chat["messages"][idx]["meta"]["old_sessions"] = old
        save(chat)


def export_md(chat):
    names = {"user": "You", **{a["id"]: a["name"] for a in chat["agents"]}}
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
PAGE_FILE = os.path.join(HERE, "static", "index.html")


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
                                   "sandbox": sandbox_ok(),
                                   "adapters": [{"id": a.id, "name": a.name, "color": a.color}
                                                for a in ADAPTERS.values()]})
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
                return self.send_json({**c, "typing": typing.get(c["id"], {}),
                                       "aliases": aliases(c),
                                       "colors": {a["id"]: ADAPTERS[a["adapter"]].color
                                                  for a in c["agents"]},
                                       "resume": {b: adapter_of(c, b).resume_hint(sid)
                                                  for b, sid in c["sessions"].items()
                                                  if sid and agent(c, b)},
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
            try:
                agents = parse_agents(d["agents"]) if d.get("agents") is not None else None
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            c = new_chat(folder, mode, int(d.get("rounds") or 0), (d.get("title") or "").strip(),
                         agents)
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
            to = want + ([reviewer_of(c, led)] if led else [])
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
                        or c["messages"][i]["who"] not in agent_ids(c) \
                        or outcome(c["messages"][i]["text"]) == "done":
                    return self.send_json({"error": "nothing to retry"}, 400)
                kick(c, lambda: retry(c, i))
            else:
                kick(c, lambda: compact(c))
        elif action == "stop":
            with lock:  # kill running agents and cancel the rest of this turn
                if c["id"] in workers:
                    halted.add(c["id"])
                running = [procs.get((c["id"], b)) for b in agent_ids(c)]
            for pr in running:
                if pr:
                    kill(pr, "stopped")
        elif action == "settings":
            with lock:
                if d.get("mode") in ("talk", "work", "auto"):
                    c["mode"] = d["mode"]
                if d.get("lead") == "" or reviewer_of(c, d.get("lead")):
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


def main():
    print(f"Group chat on http://127.0.0.1:{PORT}  (data: {DATA_DIR})", flush=True)
    if not sys.stdout.isatty():  # started in the background: keep a log file instead
        sys.stdout = sys.stderr = open(LOG_FILE, "a", buffering=1)
    load_all()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
