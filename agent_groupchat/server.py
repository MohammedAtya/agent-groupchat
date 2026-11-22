#!/usr/bin/env python3
"""Local group chat: you + your AI agents (Claude Code, Codex CLI) on one page.

Run:  python3 -m agent_groupchat   then open http://127.0.0.1:8765

- Many chats, each saved in ~/.agent-groupchat/chats/<id>.json (sidebar to reopen any of them).
- Each chat has any number of agents, including several copies of the same AI
  ("Claude 1", "Claude 2"). Each agent is a REAL CLI session (Claude Code, Codex),
  so skills, hooks, CLAUDE.md/AGENTS.md, memory and MCP config all load, and the
  sessions can also be continued from a terminal (claude -r / codex resume).
- Each agent has role(s) (Planner, Lead, Tester, Reviewer, Critic, Participant, custom)
  and a permission level (talk / read / edit / work / auto) that its CLI really enforces.
  Roles act in order: Planner + Critic -> (your OK) -> Lead -> Tester -> Reviewer.
- "Discussion rounds": after both answer you, they can reply to each other.
- @name addresses only that agent (@claude-2); @claude reaches every Claude copy.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import difflib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from . import config as cfg
from . import copies
from . import platform_support as plat
from .platform_support import sandbox_ok
from .adapters import ADAPTERS, RunContext, load_adapters, parse_apis
from .adapters import get as get_adapter
from .adapters.base import rel
from .roles import (BUILTIN, DEFAULT_ROLE, FORBIDDEN_PAIRS, LEVEL_NAMES, LEVELS, STAGES,
                    all_roles, is_test_file, rank)
from .skills import frontmatter

HERE = os.path.dirname(os.path.abspath(__file__))
# User data lives outside the code folder, so updating/reinstalling never touches it and
# it can never end up in git. AGENT_GROUPCHAT_HOME overrides it (used by tests).
DATA_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("AGENT_GROUPCHAT_HOME") or os.path.join("~", ".agent-groupchat")))
CHATS_DIR = os.path.join(DATA_DIR, "chats")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
LOG_FILE = os.path.join(LOGS_DIR, "server.log")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")  # chat APIs, custom roles, teams
# Chats of the old single-folder version (~/groupchat), offered for a one-time import (copied).
LEGACY_CHATS_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("AGENT_GROUPCHAT_LEGACY") or os.path.join("~", "groupchat", "chats")))
PORT = int(os.environ.get("GROUPCHAT_PORT", "8765"))
BOT_TIMEOUT = 900  # seconds per reply (work mode can take a while)
MAX_AGENTS = 12
LEGACY_AGENTS = [{"adapter": "claude"}, {"adapter": "codex"}]  # default, and chats from before agent lists
PASS = "PASS"
MAX_HANDOFFS = 3  # max AI->AI hand-offs after one user message (stops ping-pong)
MAX_FIX_ROUNDS = 3

# Only this page may drive the server. Without these checks any website open in the
# browser could POST to 127.0.0.1 (a text/plain fetch needs no CORS preflight) and start
# an auto-mode chat that runs commands. Host check also blocks DNS rebinding.
TOKEN = secrets.token_urlsafe(24)  # new per launch; injected into the served page
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {"http://" + h for h in ALLOWED_HOSTS}

os.makedirs(CHATS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
lock = threading.RLock()
lock_ = lock  # (alias for functions with a `lock` parameter)
chats = {}      # id -> chat dict
typing = {}     # id -> {bot: bool}
workers = {}    # id -> running worker thread
edit_locks = {}  # id -> Lock: agents that may edit take turns (one at a time per chat)

# ---------------------------------------------------------------- storage
def chat_path(cid):
    return os.path.join(CHATS_DIR, f"{cid}.json")


def save(chat):
    if chat.get("deleted"):  # deleted while an agent was still replying: don't bring it back
        return
    chat["updated"] = time.time()
    tmp = chat_path(chat["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(chat, f, indent=1)
    os.replace(tmp, chat_path(chat["id"]))


def load_all():
    for name in os.listdir(CHATS_DIR):
        if name.endswith(".json"):
            try:
                with open(os.path.join(CHATS_DIR, name), encoding="utf-8") as f:
                    c = json.load(f)
                migrate(c)
                chats[c["id"]] = c
                typing[c["id"]] = {b: False for b in agent_ids(c)}
            except Exception as e:
                print("could not load", name, e)


def migrate(c):
    """Bring a chat saved by an older version up to date (in memory; saved on next change)."""
    if "agents" not in c:  # before agent lists: Claude + Codex
        c["agents"] = make_agents(LEGACY_AGENTS)
    for a in c["agents"]:
        c["seen"].setdefault(a["id"], 0)
        c["sessions"].setdefault(a["id"], None)
        a.setdefault("roles", [])
        a.setdefault("level", None)
    mode = c.pop("mode", None)  # before roles: one mode for the whole chat
    if mode in ("talk", "auto"):
        for a in c["agents"]:
            if a["level"] is None:
                a["level"] = "read" if mode == "talk" else \
                    ("auto" if "auto" in get_adapter(a["adapter"]).levels else None)
    lead = c.pop("lead", None)  # before roles: Lead + Review
    ids = agent_ids(c)
    if lead in ids and len(ids) > 1:
        agent(c, lead)["roles"] = ["lead"]
        agent(c, ids[(ids.index(lead) + 1) % len(ids)])["roles"] = ["reviewer"]
    c.setdefault("lock", False)
    c.setdefault("fix_rounds", 1)
    c.setdefault("flow", None)


def new_chat(folder, rounds=0, title="", agents=None, lock=False, fix_rounds=1):
    cid = uuid.uuid4().hex[:12]
    agents = make_agents(agents or LEGACY_AGENTS)
    ids = [a["id"] for a in agents]
    chat = {
        "id": cid, "title": title or "New chat", "folder": folder,
        "rounds": rounds, "created": time.time(), "updated": time.time(),
        "agents": agents,
        "lock": bool(lock),              # Talk-only lock: everyone drops to "talk"
        "fix_rounds": fix_rounds,        # automatic Lead fixes after Tester/Reviewer findings
        "flow": None,                    # the running Planner -> Lead -> Tester -> Reviewer flow
        "messages": [],
        "seen": {b: 0 for b in ids},     # index of first message each agent hasn't seen
        "sessions": {b: None for b in ids},
    }
    with lock_:
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


def roles_now():
    return all_roles(CONFIG_FILE)


def refresh_adapters():
    """Pick up chat APIs added to config.json (cheap when it didn't change)."""
    err = load_adapters(CONFIG_FILE)
    if err:
        print(err, flush=True)
    return err


def check_roles_and_level(adapter_id, roles, level, name="this agent"):
    """Validate one agent's roles and permission override. Raises ValueError."""
    known = roles_now()
    if not isinstance(roles, list) or not all(isinstance(r, str) and r in known for r in roles):
        raise ValueError(f"Unknown role for {name}: {roles}")
    for a, b in FORBIDDEN_PAIRS:
        if a in roles and b in roles:
            raise ValueError(f"{name} can't be {known[a].name} and {known[b].name}: it would "
                             "review its own work. Give one of the roles to another agent "
                             "(a second copy of the same AI is fine).")
    if level is not None:
        ad = ADAPTERS[adapter_id]
        if level not in LEVELS:
            raise ValueError(f"Unknown permission level: {level}")
        if level not in ad.levels:
            raise ValueError(f"{name}: {LEVEL_NAMES[level]} isn't possible for {ad.name}. "
                             + ad.why_not(level, True))


def parse_agents(specs):
    """Validate agent specs from the page -> [{adapter, name, model, roles, level}].
    Raises ValueError."""
    refresh_adapters()
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
        if not model and getattr(ADAPTERS[sp["adapter"]], "needs_model", False):
            raise ValueError(f"{ADAPTERS[sp['adapter']].name} needs a model name, e.g. sonnet, "
                             "gpt-4.1 or ollama_chat/llama3.1.")
        roles = [r for r in (sp.get("roles") or []) if r != DEFAULT_ROLE] \
            if isinstance(sp.get("roles") or [], list) else sp.get("roles")
        level = sp.get("level") or None
        check_roles_and_level(sp["adapter"], roles, level, name or ADAPTERS[sp["adapter"]].name)
        out.append({"adapter": sp["adapter"], "name": name, "model": model or None,
                    "roles": roles, "level": level})
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
        agents.append({"id": aid, "adapter": ad.id, "name": name, "model": sp.get("model"),
                       "roles": list(sp.get("roles") or []), "level": sp.get("level")})
    return agents


def agent_ids(chat):
    return [a["id"] for a in chat["agents"]]


def agent(chat, aid):
    return next((a for a in chat["agents"] if a["id"] == aid), None)


def adapter_of(chat, aid):
    return get_adapter(agent(chat, aid)["adapter"])


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


def role_list(chat, aid):
    """The agent's Role objects (Participant if it has none)."""
    known = roles_now()
    return [known[r] for r in agent(chat, aid).get("roles") or [] if r in known] \
        or [known[DEFAULT_ROLE]]


def has_role(chat, aid, role_id):
    return any(r.id == role_id for r in role_list(chat, aid))


def stage_agents(chat, stage):
    """Agents that act in a workflow stage (e.g. "review": the Reviewers), in chat order."""
    return [b for b in agent_ids(chat) if any(r.stage == stage for r in role_list(chat, b))]


def participants(chat):
    """Agents with no workflow role: they answer messages directly, like before roles."""
    return [b for b in agent_ids(chat) if not any(r.stage for r in role_list(chat, b))]


def default_level(chat, aid):
    return max((r.level for r in role_list(chat, aid)), key=rank)


def level_info(chat, aid):
    """What `aid` may do right now:
    level     the level it really gets (what its CLI is started with)
    requested its role default or your override, capped by the Talk-only lock
    default   the default from its role(s); raised = your override is above it
    limited   why it gets less than requested ("" if it doesn't)
    choices   {level: "" if possible for this AI, else why not} for the page"""
    a, ad, sb = agent(chat, aid), adapter_of(chat, aid), sandbox_ok()
    default = default_level(chat, aid)
    requested = a.get("level") or default
    if chat.get("lock"):
        requested = "talk"
    level = requested
    while rank(level) > 0 and not can_use(ad, level, sb):
        level = LEVELS[rank(level) - 1]
    return {"level": level, "requested": requested, "default": default,
            "override": a.get("level"), "locked": bool(chat.get("lock")),
            "raised": bool(a.get("level")) and rank(a["level"]) > rank(default),
            "limited": why_not(ad, requested, sb) if level != requested else "",
            "choices": {lv: ("" if lv in ad.levels else ad.why_not(lv, True)) for lv in LEVELS}}


def can_use(ad, level, sandbox):
    if plat.COMMANDS_BLOCKED and level in ("work", "auto"):
        return False
    return ad.available(level, sandbox)


def why_not(ad, level, sandbox):
    if plat.COMMANDS_BLOCKED and level in ("work", "auto") and level in ad.levels:
        return plat.WINDOWS_LIMIT
    return ad.why_not(level, sandbox)


def level_of(chat, aid):
    return level_info(chat, aid)["level"]


def can_edit(level):
    return rank(level) >= rank("edit")


def permission_text(chat, bot):
    """What this agent may do and what its job is, told to it at the start and on changes."""
    info, ad = level_info(chat, bot), adapter_of(chat, bot)
    text = ad.mode_text(info["level"])
    if info["locked"]:
        text += " (The user turned on the Talk-only lock for everyone.)"
    elif info["limited"]:
        text += f" (You'd have {LEVEL_NAMES[info['requested']]}, but it {info['limited']}.)"
    if can_edit(info["level"]) and len(chat["agents"]) > 1:
        text += " Agents that can edit take turns, so you won't edit at the same time as another."
    jobs = [r.job for r in role_list(chat, bot) if r.job]
    return text + ("\n" + " ".join(jobs) if jobs else "")


def role_names(chat, aid):
    return [r.name for r in role_list(chat, aid)]


# ---------------------------------------------------------------- settings (config.json)
def check_team(t):
    """A saved team from the page -> clean dict. Raises ValueError."""
    if not isinstance(t, dict):
        raise ValueError(f"Each team must be an object: {t!r}")
    name = str(t.get("name") or "").strip()
    if not cfg.TEAM_RE.match(name) or not name.strip(" .,'+&()/-"):
        raise ValueError(f"Team name {name!r}: 1-40 letters, digits, spaces and . , ' + & ( ) / -")
    try:
        agents = parse_agents(t.get("agents"))
    except ValueError as e:
        raise ValueError(f"Team {name}: {e}")
    return {"name": name, "agents": agents, "lock": bool(t.get("lock")),
            "fix_rounds": max(0, min(MAX_FIX_ROUNDS, int(t.get("fix_rounds", 1)))),
            "rounds": max(0, min(5, int(t.get("rounds", 1))))}


def save_settings(d):
    """POST /api/config: replace the sections that were sent. Raises ValueError."""
    sections = {}
    if "apis" in d:
        cfg.check_key_env(d["apis"])
        parse_apis(d["apis"])  # raises on bad entries
        sections["apis"] = [{k: str(a.get(k) or "").strip() for k in
                             ("id", "name", "base_url", "model", "key_env")} for a in d["apis"]]
        for a in sections["apis"]:
            a["id"] = a["id"].lower()
    if "roles" in d:
        sections["roles"] = cfg.check_roles(d["roles"])
    if "teams" in d:
        if not isinstance(d["teams"], list):
            raise ValueError("teams must be a list")
        teams = [check_team(t) for t in d["teams"]]
        if len({t["name"].lower() for t in teams}) != len(teams):
            raise ValueError("Two teams have the same name.")
        sections["teams"] = teams
    if sections:
        cfg.update(CONFIG_FILE, **sections)
        load_adapters(CONFIG_FILE, force=True)


_git_cache = {}  # folder -> (time, status): the page asks every second


def git_status(folder, fresh=False):
    """Can ⚡ Race / 🧩 Split run in this folder (it must be in a git repo)? Cached 10 s."""
    hit = _git_cache.get(folder)
    if not fresh and hit and time.time() - hit[0] < 10:
        return hit[1]
    st = copies.status(folder)
    _git_cache[folder] = (time.time(), st)
    return st


def legacy_files():
    """Old chats (~/groupchat/chats/*.json) not imported yet, unless the user dismissed it."""
    if cfg.load(CONFIG_FILE).get("legacy_import") or LEGACY_CHATS_DIR == CHATS_DIR \
            or not os.path.isdir(LEGACY_CHATS_DIR):
        return []
    return sorted(n for n in os.listdir(LEGACY_CHATS_DIR)
                  if n.endswith(".json") and n[:-5] not in chats)


def import_legacy():
    """Copy the old chats (and their diffs) in; the originals stay where they are."""
    n = 0
    for name in legacy_files():
        cid = name[:-5]
        try:
            with open(os.path.join(LEGACY_CHATS_DIR, name), encoding="utf-8") as f:
                c = json.load(f)
            if c.get("id") != cid:
                continue
            migrate(c)
        except Exception as e:
            print("could not import", name, e)
            continue
        diffs = os.path.join(LEGACY_CHATS_DIR, cid + ".diffs")
        if os.path.isdir(diffs):
            shutil.copytree(diffs, os.path.join(CHATS_DIR, cid + ".diffs"), dirs_exist_ok=True)
        with lock:
            chats[cid] = c
            typing[cid] = {b: False for b in agent_ids(c)}
            save(c)
        n += 1
    cfg.update(CONFIG_FILE, legacy_import="done")
    return n


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

    def describe(a):
        bits = [get_adapter(a["adapter"]).kind]
        if a["adapter"] == me["adapter"]:
            bits.append("a separate session of the same AI as you")
        if a.get("roles"):
            bits.append("role: " + ", ".join(role_names(chat, a["id"])))
        return f"{a['name']} ({'; '.join(bits)})"

    with_whom = ("these AI agents: " + "; ".join(map(describe, others))) if others \
        else "no other AI agents"
    labels = ", ".join(f"[{a['name']}]" for a in others)
    mine = role_names(chat, bot)
    return (
        f"You are {me['name']}, a member of a group chat with a human user and {with_whom}. "
        + (f"Your role: {', '.join(mine)}. " if me.get("roles") else "")
        + f"Project folder: {chat['folder']}. {permission_text(chat, bot)}\n"
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


def has_flow(chat):
    """Does any agent have a workflow role (Planner, Critic, Lead, Tester, Reviewer)?"""
    return any(stage_agents(chat, st) for st in STAGES)


def route(chat, m):
    """Who answers a user message directly: (agents, flow). flow=True means a plain message
    in a chat with roles: the participants answer it directly and the workflow does the rest."""
    ids = agent_ids(chat)
    sk, at = m.get("skill"), mentioned(chat, m["text"])
    # @name anywhere addresses only those agents; the others stay silent and read it as
    # context later. A /skill goes to every agent whose AI has it.
    if at:
        return at, False
    if sk:
        return [b for b in ids if agent(chat, b)["adapter"] in skill_adapters(sk)], False
    if has_flow(chat):
        return participants(chat), True
    return ids, False


def set_status(chat, idxs, bot, st, only=None):
    """Per-message delivery status shown under your messages (queued/running/done/waiting…)."""
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
    """Stop an agent run and everything it started (see platform_support.kill_tree)."""
    if p.poll() is not None:
        return
    p.gc_reason = reason
    plat.kill_tree(p)


def stream(chat, bot, cmd, on_event):
    """Run an agent CLI, passing each JSON line of its stdout to on_event.
    Returns (exit code, stderr tail, None | "stopped" | "timed out")."""
    key = (chat["id"], bot)
    try:
        cmd = plat.resolve_command(cmd)
    except plat.UnsafeCommand as e:
        return 1, str(e), None
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        try:
            p = subprocess.Popen(cmd, cwd=chat["folder"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=err, text=True,
                                 encoding="utf-8", errors="replace",
                                 **plat.popen_kwargs())  # own process group, for Stop
        except OSError as e:  # e.g. the CLI isn't installed
            return 1, f"couldn't start {cmd[0]}: {e}", None
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
                except ValueError:  # plain text (e.g. Aider): adapters that want it read it
                    ev = {"type": "_text", "text": line.rstrip("\n")}
                if not isinstance(ev, dict):
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


def run_agent(chat, bot, prompt, level):
    """Run one turn of `bot` through its adapter. -> (reply text, meta)."""
    def save_session(sid):
        with lock:
            chat["sessions"][bot] = sid
            save(chat)

    ctx = RunContext(
        folder=chat["folder"], session=chat["sessions"].get(bot), model=agent(chat, bot)["model"],
        sandbox=sandbox_ok(), state_dir=os.path.join(DATA_DIR, "sessions"),
        system_prompt=intro(bot, chat), save_session=save_session,
        step=lambda text: step(chat, bot, text),
        preview=lambda text, append=False: set_text(chat, bot, text, append),
        stream=lambda cmd, on_event: stream(chat, bot, cmd, on_event))
    return adapter_of(chat, bot).run(ctx, prompt, level)


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


def note_changes(chat, bot):
    """'[System]: ...' when this agent's role or permissions changed since its last turn
    (Claude gets its intro every turn, but Codex only at the start of its session)."""
    info = level_info(chat, bot)
    sig = [info["level"], info["locked"], list(agent(chat, bot).get("roles") or [])]
    with lock:
        told = chat.setdefault("told", {})
        changed = told.get(bot) not in (None, sig)
        told[bot] = sig
    if not changed:
        return ""
    return (f"[System]: Your role or permissions changed. Role: {', '.join(role_names(chat, bot))}. "
            f"{permission_text(chat, bot)}\n\n")


def warning(chat, bot, level, files):
    """⚠ under a reply that changed files it shouldn't have."""
    if not files:
        return ""
    if not can_edit(level):
        return (f"changed files although its permission is {LEVEL_NAMES[level]}: "
                + ", ".join(files[:5]))
    if has_role(chat, bot, "tester") and not has_role(chat, bot, "lead"):
        other = [f for f in files if not is_test_file(f.replace(" (deleted)", ""))]
        if other:
            return "the Tester changed non-test files: " + ", ".join(other[:5])
    return ""


VERDICT_RE = re.compile(r"VERDICT:\s*\**\s*(LGTM|FIX)\b", re.I)


def verdict(text):
    found = VERDICT_RE.findall(text or "")
    return found[-1].upper() if found else None


ROLE_VERB = {"plan": "planning", "work": "working", "test": "testing", "review": "reviewing"}


def ask(chat, bot, extra="", cap=None, role=None, stage="", force=False):
    """Give `bot` everything it hasn't seen yet (plus `extra`) and post its reply.
    cap: highest permission level for this turn (e.g. "read" for a summary).
    role/stage: which role it answers in (shown under the reply; drives warnings/verdicts).
    force: ask even if there's nothing new (an agent with two roles, e.g. Lead + Tester)."""
    users, msgs = unseen_for(chat, bot)
    if not msgs and not force:
        return None
    set_status(chat, users, bot, "running")
    level = level_of(chat, bot)
    if cap and rank(level) > rank(cap):
        level = cap
    note = note_changes(chat, bot)
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
    elif sk and syntax == "dollar":  # (syntax "none": a chat API has no skills)
        # e.g. Codex runs a skill when the message mentions $name
        shown = [dict(m, text=re.sub(r"/" + re.escape(sk["name"]) + r"\b", "$" + sk["name"],
                                     m["text"], count=1, flags=re.I))
                 if m is last_user else m for m in msgs]
        prompt = note + format_msgs(chat, shown) + extra
    elif msgs:
        prompt = note + format_msgs(chat, msgs) + extra
    else:
        prompt = note + extra.strip()
    editor = can_edit(level)
    start = time.time()
    with lock:
        typing.setdefault(chat["id"], {})[bot] = True
        activity.setdefault(chat["id"], {})[bot] = {
            "since": start, "steps": [],
            "role": "summarizing" if role == "summary" else ROLE_VERB.get(stage, "")}
    elock = edit_locks.setdefault(chat["id"], threading.Lock()) if editor else None
    before, meta, reply, got = None, {}, "(stopped)", False
    try:
        if elock and not elock.acquire(blocking=False):  # another agent is editing: wait
            step(chat, bot, "⏳ waiting for its turn to edit")
            elock.acquire()
        got = bool(elock)
        if chat["id"] not in halted:
            before = snapshot(chat["folder"])
            run_start = time.time()
            with lock:
                mine = runs.setdefault(chat["id"], {})[bot] = [run_start, None, editor]
            try:
                reply, meta = run_agent(chat, bot, prompt, level)
            except Exception as e:
                reply = f"(error) {e}"
            with lock:
                mine[1] = time.time()
                # did an agent that can edit work in the folder at the same time? Then the
                # folder snapshot can't tell whose change is whose.
                overlap = any(o[2] and o[0] < mine[1] and (o[1] is None or o[1] > run_start)
                              for b, o in runs[chat["id"]].items() if b != bot)
            if before is not None and not overlap:  # every change in the folder is this agent's
                after = snapshot(chat["folder"])
                if after is not None:
                    meta["files"] = list(dict.fromkeys((meta.get("files") or [])
                                                       + changed_files(before, after, chat["folder"])))
    finally:
        if got:
            elock.release()
        with lock:
            typing.get(chat["id"], {})[bot] = False
            activity.get(chat["id"], {}).pop(bot, None)
    set_status(chat, users, bot, outcome(reply))
    if outcome(reply) == "passed":
        return PASS
    files = meta.get("files") or []
    diff = make_diff(before, files, chat["folder"]) if before is not None and files else ""
    meta = {k: v for k, v in meta.items() if v}
    meta["secs"] = round(time.time() - start)
    meta["for"] = users  # which of your messages this answers (for Retry)
    if role:
        meta["role"] = role
    if stage:
        meta["stage"] = stage
    warn = warning(chat, bot, level, files)
    if warn:
        meta["warn"] = warn
    if stage in ("test", "review") and verdict(reply):
        meta["verdict"] = verdict(reply)
    if diff:
        meta["diff"] = [diff.count("\n+") - diff.count("\n+++"),
                        diff.count("\n-") - diff.count("\n---")]
    idx = add_msg(chat, bot, reply, meta=meta)
    if diff and not chat.get("deleted"):
        os.makedirs(os.path.dirname(diff_path(chat["id"], idx)), exist_ok=True)
        with open(diff_path(chat["id"], idx), "w", encoding="utf-8") as f:
            f.write(diff)
    return reply


def parallel(chat, bots, extra="", extras=None, opts=None):
    """Ask several agents at once (agents that can edit still take turns: edit lock)."""
    results = {}

    def go(b):
        results[b] = ask(chat, b, (extras or {}).get(b, extra), **((opts or {}).get(b, {})))
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
    """(agents that still owe a direct reply to SOME user message, skill of the latest one).
    Checks every user message, not just the latest, so '@codex X' followed quickly by
    '@claude Y' still gets Codex's answer to X. Workflow roles are not included: the
    flow (step_flow) asks them in order."""
    with lock:
        targets, sk = [], None
        for i, m in enumerate(chat["messages"]):
            if m["who"] != "user":
                continue
            sk = m.get("skill")
            want, _ = route(chat, m)
            targets += [b for b in want if chat["seen"].get(b, 0) <= i and b not in targets]
        return [b for b in agent_ids(chat) if b in targets], sk


def user_waiting(chat):
    with lock:  # a new user message (or Stop) takes priority over AI-to-AI talk
        return chat["id"] in halted or (
            bool(chat["messages"]) and chat["messages"][-1]["who"] == "user")


# ---------------------------------------------------------------- workflow
# A plain message in a chat with roles runs a flow, one stage at a time:
#   plan (Planners + Critics) -> await (your OK, only with a Planner) -> work (Leads)
#   -> test (Testers) -> review (Reviewers) -> back to you.
# Stages nobody holds are skipped. If a Tester/Reviewer ends with VERDICT: FIX, the Leads get
# up to chat["fix_rounds"] automatic fix rounds (work -> test -> review again).

def names_of(chat, ids):
    n = [display(chat, b) for b in ids]
    return ", ".join(n[:-1]) + " and " + n[-1] if len(n) > 1 else (n[0] if n else "")


def stage_note(chat, bot, stage, f):
    leads = stage_agents(chat, "work")
    if stage == "plan":
        after = (f"the user reads the plan and says go (or replies) before {names_of(chat, leads)} "
                 "start the work." if "await" in f["stages"] and leads else
                 f"{names_of(chat, leads)} start the work right after you." if leads else
                 "the user decides what happens next.")
        if has_role(chat, bot, "planner"):
            return ("\n\n(Workflow · Planner: propose a short, numbered plan for the user's latest "
                    "request and list any questions that must be answered first. Don't do the "
                    f"work: {after})")
        return ("\n\n(Workflow · Critic: challenge the user's latest request and any plan above: "
                "risks, wrong assumptions, what's missing. Be brief and specific. Don't do the "
                f"work: {after})")
    checkers = stage_agents(chat, "test") + stage_agents(chat, "review")
    if stage == "work":
        then = (f"After you, {names_of(chat, checkers)} check it." if checkers else
                "Then it goes back to the user.")
        if f["fix"]:
            return (f"\n\n(Workflow · Lead, fix round {f['fix']} of {chat.get('fix_rounds', 1)}: "
                    "the Tester/Reviewer found problems above. Fix them, then say briefly what "
                    f"you changed. {then})")
        plan = " following the plan above" if "plan" in f["stages"] else ""
        return f"\n\n(Workflow · Lead: do the work for the user's latest request now{plan}. {then})"
    files = sorted({x for m in chat["messages"][f["msg"]:] if m["who"] in leads
                    for x in (m.get("meta") or {}).get("files") or []})
    did = (f"{names_of(chat, leads)} just did the work above"
           + (f" (files changed: {', '.join(files)})" if files else "") + "."
           if leads else "Look at the user's latest request above.")
    end = ("End your reply with exactly one line: VERDICT: LGTM if nothing needs fixing, or "
           "VERDICT: FIX if the Lead must change something.")
    if stage == "test":
        return (f"\n\n(Workflow · Tester: {did} Write or update tests for it and run them. Only "
                f"change test files, never the code under test. Report what fails. {end})")
    return (f"\n\n(Workflow · Reviewer: {did} Check it against what the user asked: bugs, missed "
            "requirements, risky edits. Read the files and, where your permissions allow, run "
            "read-only checks such as the tests or git diff, but do NOT modify any files. Say "
            f"concretely what to fix. {end})")


def flow_stages(chat):
    st = [x for x in STAGES if stage_agents(chat, x)]
    if "plan" in st and any(has_role(chat, b, "planner") for b in agent_ids(chat)):
        st.insert(st.index("plan") + 1, "await")
    return st


def flow_agents(chat):
    inflow = {b for st in STAGES for b in stage_agents(chat, st)}
    return [b for b in agent_ids(chat) if b in inflow]


def post_user_message(chat, text, sk=None):
    """Add a user message. A plain message in a chat with roles starts the workflow, or,
    while the plan waits for your OK, goes back to the Planners as feedback."""
    with lock:
        halted.discard(chat["id"])
        extra = {"skill": sk} if sk else {}
        want, flow = route(chat, {"text": text, **extra})
        f = chat.get("flow")
        if flow and f and f.get("waiting") and "plan" in f["stages"]:  # feedback on the plan
            extra_to = stage_agents(chat, "plan")
            to = [b for b in agent_ids(chat) if b in want or b in extra_to]
            add_msg(chat, "user", text, to=to, status={b: "queued" for b in to}, **extra)
            f["i"], f["waiting"] = f["stages"].index("plan"), False
        elif flow:
            if f:
                end_flow(chat, "skipped")
            inflow = flow_agents(chat)
            to = [b for b in agent_ids(chat) if b in want or b in inflow]
            idx = add_msg(chat, "user", text, to=to, status={b: "queued" for b in to}, **extra)
            chat["flow"] = {"msg": idx, "stages": flow_stages(chat), "i": 0, "fix": 0,
                            "waiting": False}
        else:
            add_msg(chat, "user", text, to=want, status={b: "queued" for b in want}, **extra)
        save(chat)


def end_flow(chat, status):
    """Stop the workflow; agents that hadn't answered yet get `status` (skipped/stopped)."""
    with lock:
        f = chat.get("flow")
        if not f:
            return
        for i in range(f["msg"], len(chat["messages"])):
            m = chat["messages"][i]
            if m["who"] == "user":
                for b, st in (m.get("status") or {}).items():
                    if st in ("queued", "waiting"):
                        m["status"][b] = status
        chat["flow"] = None
        save(chat)


def go_ahead(chat):
    """You OK'd the plan: continue with the work."""
    with lock:
        f = chat.get("flow")
        if not f or not f.get("waiting"):
            return False
        f["waiting"] = False
        f["i"] += 1
        for i in range(f["msg"], len(chat["messages"])):
            m = chat["messages"][i]
            for b, st in (m.get("status") or {}).items():
                if st == "waiting":
                    m["status"][b] = "queued"
        save(chat)
        return True


def run_stage(chat, f, stage):
    """Ask the agents of one stage. -> False if the flow can't go on (e.g. every Lead failed)."""
    who = stage_agents(chat, stage)
    if f.get("only"):  # Retry: just the agent whose reply failed
        who = [b for b in who if b in f.pop("only")]
    if not who:
        return True

    def role_name(b):
        return next((r.name for r in role_list(chat, b) if r.stage == stage), "")
    opts = {b: {"role": role_name(b), "stage": stage, "force": stage != "plan"} for b in who}
    res = parallel(chat, who, extras={b: stage_note(chat, b, stage, f) for b in who}, opts=opts)
    if stage in ("test", "review"):
        with lock:
            for b in who:
                last = next((m for m in reversed(chat["messages"]) if m["who"] == b), None)
                f.setdefault("verdicts", {})[b] = ((last or {}).get("meta") or {}).get("verdict") or "?"
        return True
    return any(r not in (None, PASS) and outcome(r) == "done" for r in res.values())


def step_flow(chat):
    """Run the next stage of the workflow. -> True if it did something (call again)."""
    with lock:
        f = chat.get("flow")
        if not f or f.get("waiting"):
            return False
        if f["i"] >= len(f["stages"]):
            chat["flow"] = None
            save(chat)
            return False
        stage = f["stages"][f["i"]]
        if stage == "await":  # wait for the user's OK on the plan
            f["waiting"] = True
            later = {b for st in f["stages"][f["i"] + 1:] for b in stage_agents(chat, st)}
            for b in later:
                set_status(chat, [f["msg"]], b, "waiting", only=("queued",))
            save(chat)
            return False
    ok = run_stage(chat, f, stage)
    with lock:
        if chat.get("flow") is not f:  # replaced by a new message, or stopped
            return True
        if not ok:
            end_flow(chat, "skipped")
            return True
        f["i"] += 1
        checks_done = stage in ("test", "review") and \
            not any(s in ("test", "review") for s in f["stages"][f["i"]:])
        if checks_done and "work" in f["stages"]:
            if "FIX" in f.get("verdicts", {}).values() and f["fix"] < chat.get("fix_rounds", 1):
                f["fix"] += 1
                f["i"] = f["stages"].index("work")
            f["verdicts"] = {}
        save(chat)
    return True


def set_agents(chat, specs):
    """Change roles and permission overrides: [{"id", "roles": [...], "level": None|level}]."""
    if not isinstance(specs, list):
        raise ValueError("agents must be a list")
    changes = {}
    for sp in specs:
        a = agent(chat, sp.get("id")) if isinstance(sp, dict) else None
        if not a:
            raise ValueError(f"Unknown agent: {sp}")
        roles = sp.get("roles") or []
        roles = [r for r in roles if r != DEFAULT_ROLE] if isinstance(roles, list) else roles
        level = sp.get("level") or None
        check_roles_and_level(a["adapter"], roles, level, a["name"])
        changes[a["id"]] = (roles, level)
    with lock:
        for aid, (roles, level) in changes.items():
            a = agent(chat, aid)
            a["roles"], a["level"] = roles, level
        save(chat)


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
                            if st in ("queued", "running", "waiting"):
                                m["status"][b] = "stopped"
                    for b in agent_ids(chat):
                        chat["seen"][b] = len(chat["messages"])
                    chat["flow"] = None
                    save(chat)
                    return
            targets, sk = pending(chat)
            if targets:  # direct answers first: @mentions, skills, Participants
                parallel(chat, targets)
                parts = [b for b in targets if b in participants(chat)]
                if len(parts) >= 2 and not sk:  # no auto back-and-forth during a skill
                    discuss(chat, parts)
                handoffs(chat, targets)
                continue
            if step_flow(chat):
                continue
            return
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
    """Ask the same agent again for a reply that failed (error / stopped / timed out).
    A failed workflow step continues the workflow from that step."""
    m = chat["messages"][idx]
    bot, meta = m["who"], m.get("meta") or {}
    users = meta.get("for") or []
    with lock:
        m["retried"] = True
        if users:
            chat["seen"][bot] = min(chat["seen"][bot], min(users))
        save(chat)
    set_status(chat, users, bot, "queued")
    if meta.get("role") == "summary":
        return compact(chat)
    stage = meta.get("stage")
    stages = flow_stages(chat)
    if stage in stages:
        with lock:
            start = users[0] if users else (chat.get("flow") or {}).get("msg", idx)
            chat["flow"] = {"msg": start, "stages": stages, "i": stages.index(stage), "fix": 0,
                            "waiting": False, "only": [bot]}
            save(chat)
        return  # the worker runs the flow from that step
    ask(chat, bot)


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
    reply = ask(chat, agent_ids(chat)[0], SUMMARY_NOTE, cap="read", role="summary")
    if reply in (None, PASS) or reply.startswith("("):
        return
    with lock:
        old = chat["sessions"]
        chat.setdefault("old_sessions", []).append(old)
        chat["sessions"] = {b: None for b in agent_ids(chat)}
        chat["told"] = {}
        chat["flow"] = None
        idx = len(chat["messages"]) - 1  # the summary: all new sessions start from it
        for b in agent_ids(chat):
            chat["seen"][b] = idx
        chat["messages"][idx]["meta"]["old_sessions"] = old
        save(chat)


def export_md(chat):
    names = {"user": "You", **{a["id"]: a["name"] for a in chat["agents"]}}
    out = [f"# {chat['title']}", "",
           f"Folder: `{chat['folder']}` · agents: "
           + ", ".join(f"{a['name']} ({', '.join(role_names(chat, a['id']))})" for a in chat["agents"])
           + f" · exported "
           f"{time.strftime('%Y-%m-%d %H:%M')}", ""]
    for m in chat["messages"]:
        if m.get("hidden"):
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("ts", 0)))
        r = (m.get("meta") or {}).get("role")
        role = " (chat summary)" if r == "summary" else f" ({r})" if r else ""
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
            config_error = refresh_adapters()
            with lock:
                lst = sorted(({**{k: c[k] for k in ("id", "title", "updated", "folder", "lock")},
                               "agents": len(c["agents"])}
                              for c in chats.values()), key=lambda c: -c["updated"])
            return self.send_json({"chats": lst, "home": os.path.expanduser("~"),
                                   "sandbox": sandbox_ok(),
                                   "config_error": config_error,
                                   "platform": {"os": plat.SYSTEM, "wsl": plat.IS_WSL,
                                                "native_windows": plat.IS_WINDOWS,
                                                "sandbox_help": "" if sandbox_ok() else plat.sandbox_help()},
                                   "legacy": {"count": len(legacy_files()), "dir": LEGACY_CHATS_DIR},
                                   "adapters": [{"id": a.id, "name": a.name, "color": a.color,
                                                 "installed": a.installed(),
                                                 "needs_model": getattr(a, "needs_model", False),
                                                 "api": not a.command,
                                                 "levels": list(a.levels),
                                                 "notes": {lv: a.why_not(lv, True) for lv in LEVELS
                                                           if lv not in a.levels}}
                                                for a in ADAPTERS.values()],
                                   "roles": [{"id": r.id, "name": r.name, "level": r.level,
                                              "job": r.job, "builtin": r.builtin}
                                             for r in roles_now().values()],
                                   "levels": [{"id": lv, "name": LEVEL_NAMES[lv]} for lv in LEVELS]})
        if p == "/api/config":
            data = cfg.load(CONFIG_FILE)
            return self.send_json({"apis": data.get("apis") or [], "roles": data.get("roles") or [],
                                   "teams": data.get("teams") or []})
        m = re.fullmatch(r"/api/adapters/([\w-]+)/test", p)
        if m:  # the Test button: is this AI installed / reachable? (no tokens spent)
            refresh_adapters()
            ok, msg = get_adapter(m.group(1)).detect()
            return self.send_json({"ok": ok, "message": msg})
        m = re.fullmatch(r"/api/chats/(\w+)/skills", p)
        if m:
            c = chats.get(m.group(1))
            if not c:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json({"skills": list_skills(c["folder"])})
        m = re.fullmatch(r"/api/chats/(\w+)/diff/(\d+)", p)
        if m:
            try:
                with open(diff_path(m.group(1), int(m.group(2))), encoding="utf-8") as f:
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
                                       "perm": {b: level_info(c, b) for b in agent_ids(c)},
                                       "git": git_status(c["folder"]),
                                       "colors": {a["id"]: get_adapter(a["adapter"]).color
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
        self.send_header("X-Agent-Groupchat", __version__)  # lets a second launch find this one
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.trusted(post=True):
            return self.send_json({"error": "forbidden"}, 403)
        try:
            d = self.body()
        except ValueError:
            return self.send_json({"error": "bad json"}, 400)
        if not isinstance(d, dict):
            return self.send_json({"error": "the request body must be a JSON object"}, 400)
        try:
            return self.post(d)
        except (ValueError, TypeError, KeyError, AttributeError) as e:  # wrong types in the body
            print("bad request", self.path, repr(e), flush=True)
            return self.send_json({"error": f"bad request: {e}"}, 400)

    def post(self, d):
        p = self.path
        if p == "/api/chats":
            folder = os.path.abspath(os.path.expanduser(d.get("folder") or "~"))
            if not os.path.isdir(folder):
                return self.send_json({"error": f"Folder does not exist: {folder}"}, 400)
            try:
                agents = parse_agents(d["agents"]) if d.get("agents") is not None else None
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            c = new_chat(folder, max(0, min(5, int(d.get("rounds") or 0))),
                         (d.get("title") or "").strip(), agents, lock=bool(d.get("lock")),
                         fix_rounds=max(0, min(MAX_FIX_ROUNDS, int(d.get("fix_rounds", 1)))))
            return self.send_json({"id": c["id"]})
        if p == "/api/config":
            try:
                save_settings(d)
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json({"ok": True})
        if p == "/api/legacy":
            if d.get("action") == "import":
                return self.send_json({"ok": True, "imported": import_legacy()})
            cfg.update(CONFIG_FILE, legacy_import="dismissed")
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/chats/(\w+)/(send|settings|agents|go|delete|stop|retry|compact|git-init)", p)
        c = chats.get(m.group(1)) if m else None
        if not c:
            return self.send_json({"error": "not found"}, 404)
        action = m.group(2)
        if action == "send":
            text = str(d.get("text") or "").strip() if isinstance(d.get("text"), str) else ""
            if not text:
                return self.send_json({"error": "empty"}, 400)
            sk = parse_skill(text, c["folder"])
            with lock:
                halted.discard(c["id"])
            post_user_message(c, text, sk)
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
        elif action == "git-init":  # "Initialize git here" (for ⚡ Race / 🧩 Split)
            try:
                copies.init_repo(c["folder"])
            except (copies.GitError, OSError) as e:
                return self.send_json({"error": f"Couldn't initialize git: {e}"}, 400)
            return self.send_json({"ok": True, "git": git_status(c["folder"], fresh=True)})
        elif action == "go":  # the plan is OK: start the work
            if not go_ahead(c):
                return self.send_json({"error": "Nothing is waiting for your OK."}, 409)
            kick(c)
        elif action == "agents":  # change roles / permission overrides
            try:
                set_agents(c, d.get("agents"))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        elif action == "stop":
            with lock:  # kill running agents and cancel the rest of this turn
                end_flow(c, "stopped")
                if c["id"] in workers:
                    halted.add(c["id"])
                running = [procs.get((c["id"], b)) for b in agent_ids(c)]
            for pr in running:
                if pr:
                    kill(pr, "stopped")
        elif action == "settings":
            with lock:
                if "lock" in d:
                    c["lock"] = bool(d["lock"])
                if "fix_rounds" in d:
                    c["fix_rounds"] = max(0, min(MAX_FIX_ROUNDS, int(d["fix_rounds"])))
                if "rounds" in d:
                    c["rounds"] = max(0, min(5, int(d["rounds"])))
                if d.get("title"):
                    c["title"] = d["title"].strip()[:80]
                save(c)
        elif action == "delete":
            with lock:  # stop its agents; a reply still running must not save it again
                c["deleted"] = True
                c["flow"] = None
                if c["id"] in workers:
                    halted.add(c["id"])
                running = [procs.get((c["id"], b)) for b in agent_ids(c)]
            for pr in running:
                if pr:
                    kill(pr, "stopped")
            with lock:
                chats.pop(c["id"], None)
                typing.pop(c["id"], None)
                try:
                    os.remove(chat_path(c["id"]))
                except FileNotFoundError:
                    pass
                shutil.rmtree(os.path.join(CHATS_DIR, f"{c['id']}.diffs"), ignore_errors=True)
        return self.send_json({"ok": True})


def set_port(port):
    global PORT, ALLOWED_HOSTS, ALLOWED_ORIGINS
    PORT = port
    ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    ALLOWED_ORIGINS = {"http://" + h for h in ALLOWED_HOSTS}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="agent-groupchat",
                                 description="A local group chat between you and your AI agents.")
    ap.add_argument("--port", type=int, default=PORT,
                    help=f"port to use (default {PORT}, or the next free one)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ap.add_argument("--version", action="version", version=f"agent-groupchat {__version__}")
    args = ap.parse_args(argv)
    for port in range(args.port, args.port + 10):  # skip ports other programs use
        state = plat.probe(port)
        if state != "busy":
            break
    else:
        sys.exit(f"Ports {args.port}-{args.port + 9} are all in use. Try --port.")
    url = f"http://127.0.0.1:{port}"
    if state == "ours":  # already running: just open it
        print(f"agent-groupchat is already running: {url}")
        if not args.no_browser:
            plat.open_browser(url, delay=0)
        return
    set_port(port)
    refresh_adapters()
    load_all()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"agent-groupchat {__version__} on {url}  (your data: {DATA_DIR})", flush=True)
    if not sandbox_ok():
        print("Note: " + plat.sandbox_help(), flush=True)
    if not args.no_browser:
        plat.open_browser(url)
    if not sys.stdout.isatty():  # started in the background: keep a log file instead
        sys.stdout = sys.stderr = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
