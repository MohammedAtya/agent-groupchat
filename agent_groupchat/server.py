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
from .adapters.base import first_line, rel
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
        a.setdefault("focus", None)
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
    r = c.get("race")
    if r:
        r.setdefault("scope", (r.get("sub") or ".").replace(os.sep, "/"))
        if r["stage"] in RUNNING:  # interrupted by a restart: never pick it up half-way later
            r["stage"], r["interrupted"] = "stopped", True
            r.pop("combine", None)
            r.pop("retry", None)
            for cp in r["copies"].values():
                if cp.get("status") == "working":
                    cp["status"] = "stopped"
            for m in c["messages"][r.get("msg", 0):]:
                for b, st in (m.get("status") or {}).items():
                    if st in ("running", "queued", "waiting"):
                        m["status"][b] = "stopped"


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


FOCUS_MAX = 120


def clean_focus(value, name="this agent"):
    """An agent's focus (SPEC §4.4): one short line of text, or None. Raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name}: the focus must be text, e.g. login or database.")
    focus = " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())
    if len(focus) > FOCUS_MAX:
        raise ValueError(f"{name}: keep the focus under {FOCUS_MAX} characters "
                         "(a short area like 'login' or 'database').")
    return focus or None


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
        focus = clean_focus(sp.get("focus"), name or ADAPTERS[sp["adapter"]].name)
        out.append({"adapter": sp["adapter"], "name": name, "model": model or None,
                    "roles": roles, "level": level, **({"focus": focus} if focus else {})})
    return out


WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}


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
        if aid.upper() in WINDOWS_RESERVED:  # can't be a folder name on Windows (copies use it)
            aid = f"{aid}-agent"
        first_id = aid
        while aid in ids:
            aid, k = f"{first_id}-{k}", k + 1
        ids.add(aid)
        agents.append({"id": aid, "adapter": ad.id, "name": name, "model": sp.get("model"),
                       "roles": list(sp.get("roles") or []), "level": sp.get("level"),
                       "focus": sp.get("focus") or None})
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


def permission_text(chat, bot, copy=False):
    """What this agent may do and what its job is, told to it at the start and on changes.
    copy: it works in a Race/Split copy, where it does the task it's given (not its focus)."""
    info, ad = level_info(chat, bot), adapter_of(chat, bot)
    text = ad.mode_text(info["level"])
    if info["locked"]:
        text += " (The user turned on the Talk-only lock for everyone.)"
    elif info["limited"]:
        text += f" (You'd have {LEVEL_NAMES[info['requested']]}, but it {info['limited']}.)"
    if can_edit(info["level"]) and len(chat["agents"]) > 1:
        text += " Agents that can edit take turns, so you won't edit at the same time as another."
    jobs = [r.job for r in role_list(chat, bot) if r.job]
    return text + ("\n" + " ".join(jobs) if jobs else "") + focus_text(chat, bot, copy)


def role_names(chat, aid):
    return [r.name for r in role_list(chat, aid)]


# ---------------------------------------------------------------- focus (SPEC §4.4)
# Several agents can share a role; each can get a focus (an area: "login", "database") so they
# split the work instead of doing it twice. Overlapping focuses get a warning, never a block.

def shared_roles(agents, aid):
    """[(Role, [the other agents holding it])] for this agent's roles that others hold too."""
    known = roles_now()
    me = next(a for a in agents if a["id"] == aid)
    out = []
    for rid in me.get("roles") or []:
        if rid in known and rid != DEFAULT_ROLE:
            others = [a for a in agents if a["id"] != aid and rid in (a.get("roles") or [])]
            if others:
                out.append((known[rid], others))
    return out


def focus_text(chat, bot, copy=False):
    """Told to the agent: its focus, and who else holds its roles with which focus."""
    mine = agent(chat, bot).get("focus")
    if copy:
        return (f" Your usual focus is {mine}, but here you do the task you're given."
                if mine else "")
    shared = shared_roles(chat["agents"], bot)
    text = (f" Your focus: {mine}. Work on that area only and leave the others' areas to them."
            if mine else "")
    if shared:
        text += " You share your role with " + "; ".join(
            f"{r.name}: " + ", ".join(f"{a['name']} (focus: {a['focus']})" if a.get("focus")
                                      else f"{a['name']} (no focus)" for a in others)
            for r, others in shared) + "."
        text += (" Look at what they already did above and don't redo it." if not mine else
                 " Don't redo their part.")
    return text


FOCUS_STOP = {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "all",
              "only", "part", "parts", "area", "test", "tests", "testing", "check", "checks",
              "checking", "review", "reviewing", "code"}


def focus_key(w):
    """One key for a word's singular and plural: logins/login, cookies/cookie,
    dependencies/dependency -> login, cooki, dependenci."""
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    if len(w) > 3 and w.endswith("ie"):
        w = w[:-1]
    elif len(w) > 3 and w.endswith("y"):
        w = w[:-1] + "i"
    return w


def focus_words(focus):
    """'Testing the Login page' -> {'login', 'page'} (for spotting the same focus twice)."""
    raw = re.findall(r"[^\W_]+", (focus or "").lower())
    words = {focus_key(w) for w in raw if w not in FOCUS_STOP}
    # only filler words, or no letters at all (an emoji): compare the text as a whole
    return words or set(raw) or {" ".join((focus or "").lower().split())}


def overlaps(agents):
    """Agents that share a role and would do the same work (SPEC §4.4) ->
    [{"role", "agents": [ids], "kind": same|none|open, "edits", "text"}].
    edits: the role changes files (Lead, Tester), so a duplicate collides, not just repeats."""
    known, out = roles_now(), []
    for rid, role in known.items():
        if rid == DEFAULT_ROLE:
            continue
        holders = [a for a in agents if rid in (a.get("roles") or [])]
        edits = role.stage in ("work", "test")
        for i, x in enumerate(holders):
            for y in holders[i + 1:]:
                fx, fy = x.get("focus"), y.get("focus")
                both = f"{x['name']} and {y['name']} are both {role.name}"
                if not fx and not fy:
                    kind = "none"
                    text = (f"{both} and neither has a focus, so both do the whole task"
                            + (" one after the other: the same work twice. Give each a focus "
                               "(e.g. backend / frontend), or use ⚡ Race to let them compete."
                               if edits else ". Fine if you want two opinions; otherwise give "
                               "each a focus."))
                elif not fx or not fy:
                    kind = "open"
                    free, busy = (x, y) if not fx else (y, x)
                    text = (f"{both}, but {free['name']} has no focus, so it also covers "
                            f"{busy['name']}'s ({busy['focus']}). Give {free['name']} a focus "
                            "of its own.")
                else:
                    wx, wy = focus_words(fx), focus_words(fy)
                    if not (wx <= wy or wy <= wx):
                        continue
                    kind = "same"
                    same = fx if fx.lower() == fy.lower() else f"{fx} / {fy}"
                    text = (f"{both} with the same focus ({same})"
                            + (": they'll do the same work twice and can undo each other's "
                               "changes. Give each a different focus." if edits else
                               ": two opinions on the same thing. Fine if that's what you want."))
                out.append({"role": rid, "agents": [x["id"], y["id"]], "kind": kind,
                            "edits": edits, "text": text})
    return out


def overlaps_for_specs(specs):
    """POST /api/overlaps: the page's agent rows (not saved yet) -> overlaps."""
    if not isinstance(specs, list):
        raise ValueError("agents must be a list")
    rows = []
    for sp in specs[:MAX_AGENTS]:
        if not isinstance(sp, dict) or sp.get("adapter") not in ADAPTERS:
            continue
        roles = sp.get("roles") if isinstance(sp.get("roles"), list) else []
        try:
            focus = clean_focus(sp.get("focus"))
        except ValueError:
            focus = None
        name = str(sp.get("name") or "").strip()
        rows.append({"adapter": sp["adapter"], "name": name if NAME_RE.match(name) else "",
                     "roles": [r for r in roles if isinstance(r, str)], "focus": focus})
    return overlaps(make_agents(rows))


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
def intro(bot, chat, folder=None):
    me, ad = agent(chat, bot), adapter_of(chat, bot)
    others = [a for a in chat["agents"] if a["id"] != bot]

    def describe(a):
        bits = [get_adapter(a["adapter"]).kind]
        if a["adapter"] == me["adapter"]:
            bits.append("a separate session of the same AI as you")
        if a.get("roles"):
            bits.append("role: " + ", ".join(role_names(chat, a["id"])))
        if a.get("focus"):
            bits.append("focus: " + a["focus"])
        return f"{a['name']} ({'; '.join(bits)})"

    with_whom = ("these AI agents: " + "; ".join(map(describe, others))) if others \
        else "no other AI agents"
    labels = ", ".join(f"[{a['name']}]" for a in others)
    mine = role_names(chat, bot)
    return (
        f"You are {me['name']}, a member of a group chat with a human user and {with_whom}. "
        + (f"Your role: {', '.join(mine)}. " if me.get("roles") else "")
        + (f"Project folder: {folder} (your own copy of the user's project {chat['folder']}; "
           "work only in your copy. It's a git worktree that shares the user's stash and branches: "
           "don't use git stash, and don't create, switch, delete or reset branches). "
           if folder else f"Project folder: {chat['folder']}. ")
        + f"{permission_text(chat, bot, copy=bool(folder))}\n"
        "Messages arrive labeled [User]"
        + (f" or with the sender's name ({labels}). Every AI gets each user message at the same "
           "time and answers independently; afterwards you may see the others' answers. "
           if others else ". ")
        + "Reply as a group-chat member: concise and conversational, addressing the user"
        + (f" or the other AIs directly (@{others[0]['id']} reaches that one only)" if others else "")
        + ". Never prefix your reply with your own name. "
        f"If you are shown only other AIs' replies and have nothing useful to add, "
        f"reply with exactly {PASS}. "
        "If the user has to choose between a few options, end your reply with one line: "
        "CHOICES: first option | second option (2 to 5 short options); they appear as buttons."
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


def unseen_for(chat, bot, peek=False, known=None, stage=""):
    """(indices of the user messages among them, new messages from others since this
    bot's last turn); marks them seen (unless peek).
    known: the chat's length when this turn began. Messages added since are included (e.g. the
    Lead before this one), but not from the next user message on: that's a new turn. Replies
    in the same workflow `stage` posted after it are still shown (and shown again later)."""
    with lock:
        start = chat["seen"][bot]
        end = len(chat["messages"])
        if known is not None:
            end = next((i for i in range(max(known, start), end)
                        if chat["messages"][i]["who"] == "user"), end)
        msgs = chat["messages"][start:end]
        if known is not None and stage:
            msgs = msgs + [m for m in chat["messages"][end:] if m["who"] not in ("user", bot)
                           and (m.get("meta") or {}).get("stage") == stage]
        if not peek:
            chat["seen"][bot] = end
            save(chat)
    users = [start + k for k, m in enumerate(msgs) if m["who"] == "user"]
    # a bot never re-reads its own messages, except a chat summary (its fresh session needs it)
    # and what it did in a Race copy (that ran in a separate session)
    return users, [m for m in msgs
                   if m["who"] != bot or (m.get("meta") or {}).get("role") == "summary"
                   or (m.get("meta") or {}).get("copy")]


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
    if m.get("mode") in ("race", "split", "split-feedback"):  # handled by step_race
        return [], False
    if m.get("mode") == "no-split":  # the Lead's flow, or just one agent
        return ([] if m.get("flow") else [m["only"]]), False
    if m.get("reply_to") in agent_ids(chat):  # a decision button: that agent only, whatever it says
        return [m["reply_to"]], False
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


GIT_REDIRECT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE")


def agent_env():
    """The agents' environment, minus git variables that would send their git commands (in a
    copy, or the project) to another repository or index."""
    return {k: v for k, v in os.environ.items() if k.upper() not in GIT_REDIRECT}


def stream(chat, bot, cmd, on_event, cwd=None, stdin=None):
    """Run an agent CLI in the chat's folder (or `cwd`: a Race copy), passing each JSON line
    of its stdout to on_event.
    Returns (exit code, stderr tail, None | "stopped" | "timed out")."""
    key = (chat["id"], bot)
    try:
        cmd = plat.resolve_command(cmd)
    except plat.UnsafeCommand as e:
        return 1, str(e), None
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        try:
            p = subprocess.Popen(cmd, cwd=cwd or chat["folder"], env=agent_env(),
                                 stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=err, text=True,
                                 encoding="utf-8", errors="replace",
                                 **plat.popen_kwargs())  # own process group, for Stop
        except OSError as e:  # e.g. the CLI isn't installed
            return 1, f"couldn't start {cmd[0]}: {e}", None
        p.gc_reason = None
        procs[key] = p
        if stdin is not None:  # (a thread, so a full pipe can't block reading the output)
            def feed():
                try:
                    p.stdin.buffer.write(stdin.encode("utf-8"))  # bytes: no \r\n on Windows
                    p.stdin.close()
                except (OSError, ValueError):
                    pass
            threading.Thread(target=feed, daemon=True).start()
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


def run_agent(chat, bot, prompt, level, folder=None, fresh=False):
    """Run one turn of `bot` through its adapter. -> (reply text, meta).
    folder: run in this folder instead of the chat's (a Race copy). fresh: a one-off session
    that isn't kept (Race turns: CLIs keep sessions per folder, SPEC §14.6)."""
    def save_session(sid):
        if fresh:
            return
        with lock:
            chat["sessions"][bot] = sid
            save(chat)

    ctx = RunContext(
        folder=folder or chat["folder"], session=None if fresh else chat["sessions"].get(bot),
        model=agent(chat, bot)["model"],
        sandbox=sandbox_ok(), state_dir=os.path.join(DATA_DIR, "sessions"),
        system_prompt=intro(bot, chat, folder), save_session=save_session,
        step=lambda text: step(chat, bot, text),
        preview=lambda text, append=False: set_text(chat, bot, text, append),
        stream=lambda cmd, on_event, stdin=None: stream(chat, bot, cmd, on_event, cwd=folder, stdin=stdin))
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
    team = focus_text(chat, bot)
    if team:  # (only then: chats saved before focuses keep their old signature)
        sig.append(team)
    with lock:
        told = chat.setdefault("told", {})
        before = told.get(bot)
        changed = before not in (None, sig)
        told[bot] = sig
    if not changed:
        return ""
    had = len(before) > 3 and "Your focus:" in before[3]
    lifted = ("" if agent(chat, bot).get("focus") else
              " You no longer have a focus or share your role: the whole task is yours again."
              if not team and len(before) > 3 else
              " You no longer have a focus of your own." if team and had else "")
    return (f"[System]: Your role, focus or permissions changed. "
            f"Role: {', '.join(role_names(chat, bot))}. "
            f"{permission_text(chat, bot)}{lifted}\n\n")


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


MAX_CHOICES, MAX_CHOICE_LEN = 5, 80


def split_choices(text):
    """'…\nCHOICES: Yes | No | Show me first' at the end of a reply -> (text without that
    line, ["Yes", "No", "Show me first"]); (text, None) if there's no valid choices line."""
    lines = (text or "").rstrip().split("\n")
    m = re.match(r"(?i)^choices\s*:\s*(.+)$", lines[-1].strip().strip("*`_ ").strip())
    if not m:
        return text, None
    options = []
    for o in m.group(1).split("|"):
        o = o.strip().strip("*`_ \"'").strip()
        if o and o.lower() not in (x.lower() for x in options):
            options.append(o)
    if not 2 <= len(options) <= MAX_CHOICES or any(len(o) > MAX_CHOICE_LEN for o in options):
        return text, None
    return "\n".join(lines[:-1]).rstrip(), options


ROLE_VERB = {"plan": "planning", "work": "working", "test": "testing", "review": "reviewing"}


def build_prompt(chat, bot, msgs, extra=""):
    """The turn's prompt: changes to its role/permissions, the new messages, then `extra`."""
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
        return prompt
    if sk and syntax == "dollar":  # (syntax "none": a chat API has no skills)
        # e.g. Codex runs a skill when the message mentions $name
        shown = [dict(m, text=re.sub(r"/" + re.escape(sk["name"]) + r"\b", "$" + sk["name"],
                                     m["text"], count=1, flags=re.I))
                 if m is last_user else m for m in msgs]
        return note + format_msgs(chat, shown) + extra
    if msgs:
        return note + format_msgs(chat, msgs) + extra
    return note + extra.strip()


def ask(chat, bot, extra="", cap=None, role=None, stage="", force=False, race=False):
    """Give `bot` everything it hasn't seen yet (plus `extra`) and post its reply.
    cap: highest permission level for this turn (e.g. "read" for a summary).
    role/stage: which role it answers in (shown under the reply; drives warnings/verdicts).
    force: ask even if there's nothing new (an agent with two roles, e.g. Lead + Tester).
    extra: text added after the messages, or a function that writes it when the turn starts."""
    # Leads and Testers that share the role take turns on the same task: one that waited for
    # its turn reads the chat only then, so it sees what the one before it said and changed
    # (SPEC §4.4). Everyone else answers independently, as the intro says.
    late = stage in ("work", "test")
    if callable(extra) and not late:  # extra may be written when the turn starts (stage notes)
        extra = extra()
    known = len(chat["messages"])
    users, msgs = unseen_for(chat, bot, peek=late)
    if not msgs and not force:
        return None
    set_status(chat, users, bot, "running")
    prompt = None if late else build_prompt(chat, bot, msgs, extra)
    level = level_of(chat, bot)
    if cap and rank(level) > rank(cap):
        level = cap
    editor = can_edit(level)
    start = time.time()
    with lock:
        typing.setdefault(chat["id"], {})[bot] = True
        activity.setdefault(chat["id"], {})[bot] = {
            "since": start, "steps": [],
            "role": "summarizing" if role == "summary" else ROLE_VERB.get(stage, "")}
    elock = edit_locks.setdefault(chat["id"], threading.Lock()) if editor else None
    before, meta, reply, got, hold = None, {}, "(stopped)", False, False
    try:
        if elock and not elock.acquire(blocking=False):  # another agent is editing: wait
            step(chat, bot, "⏳ waiting for its turn to edit")
            elock.acquire()
        got = bool(elock)
        if late:
            extra = extra() if callable(extra) else extra
            users, msgs = unseen_for(chat, bot, known=known, stage=stage)
            set_status(chat, users, bot, "running")
            prompt = build_prompt(chat, bot, msgs, extra)
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
        # a Lead/Tester keeps its turn until its reply is posted, so the next one sees it
        hold = got and late
    finally:
        if got and not hold:
            elock.release()
        with lock:
            typing.get(chat["id"], {})[bot] = False
            activity.get(chat["id"], {}).pop(bot, None)
    try:
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
        if race:
            meta["race"] = True  # part of a Race/Split: never retried in the user's folder
        warn = warning(chat, bot, level, files)
        if warn:
            meta["warn"] = warn
        if stage in ("test", "review") and verdict(reply):
            meta["verdict"] = verdict(reply)
        if diff:
            meta["diff"] = [diff.count("\n+") - diff.count("\n+++"),
                            diff.count("\n-") - diff.count("\n---")]
        shown, choices = split_choices(reply) if outcome(reply) == "done" else (reply, None)
        if choices and shown:  # (a reply that is only a choices line stays as it is)
            reply, meta["choices"] = shown, choices
        idx = add_msg(chat, bot, reply, meta=meta)
        if diff and not chat.get("deleted"):
            os.makedirs(os.path.dirname(diff_path(chat["id"], idx)), exist_ok=True)
            with open(diff_path(chat["id"], idx), "w", encoding="utf-8") as f:
                f.write(diff)
        return reply
    finally:
        if hold:
            elock.release()


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
    focus = agent(chat, bot).get("focus")
    if stage == "work":
        then = (f"After you, {names_of(chat, checkers)} check it." if checkers else
                "Then it goes back to the user.")
        theirs = sorted({x for m in chat["messages"][f["msg"]:] if m["who"] in leads
                         and m["who"] != bot for x in (m.get("meta") or {}).get("files") or []})
        turns = (f" The Leads ({names_of(chat, leads)}) take turns: build on what the others "
                 "already did above instead of redoing it"
                 + (f" (files they changed: {', '.join(theirs[:20])})" if theirs else "") + "."
                 if len(leads) > 1 else "")
        if f["fix"]:
            mine = ("" if not focus else
                    f" (the ones in your area, {focus}; leave the others to the other Leads)"
                    if len(leads) > 1 else f" (your focus: {focus})")
            return (f"\n\n(Workflow · Lead, fix round {f['fix']} of {chat.get('fix_rounds', 1)}: "
                    f"the Tester/Reviewer found problems above. Fix them{mine}, then say briefly "
                    f"what you changed.{turns} {then})")
        plan = " following the plan above" if "plan" in f["stages"] else ""
        part = f", your part only: {focus}" if focus else ""
        return (f"\n\n(Workflow · Lead: do the work for the user's latest request now{plan}"
                f"{part}.{turns} {then})")
    area = f" Your focus: {focus}. Check that area only." if focus else ""
    files = sorted({x for m in chat["messages"][f["msg"]:] if m["who"] in leads
                    for x in (m.get("meta") or {}).get("files") or []})
    did = (f"{names_of(chat, leads)} just did the work above"
           + (f" (files changed: {', '.join(files)})" if files else "") + "."
           if leads else "Look at the user's latest request above.")
    end = ("End your reply with exactly one line: VERDICT: LGTM if nothing needs fixing, or "
           "VERDICT: FIX if the Lead must change something."
           + (" Say whose area each problem is in, so the right Lead fixes it."
              if len(leads) > 1 and any(agent(chat, b).get("focus") for b in leads) else ""))
    if stage == "test":
        return (f"\n\n(Workflow · Tester: {did}{area} Write or update tests for it and run them. "
                f"Only change test files, never the code under test. Report what fails. {end})")
    return (f"\n\n(Workflow · Reviewer: {did}{area} Check it against what the user asked: bugs, missed "
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


def post_user_message(chat, text, sk=None, reply_to=None):
    """Add a user message. A plain message in a chat with roles starts the workflow, or,
    while the plan waits for your OK, goes back to the Planners as feedback.
    reply_to: an agent id (a decision button): only that agent gets it."""
    with lock:
        halted.discard(chat["id"])
        extra = {"skill": sk} if sk else {}
        if reply_to in agent_ids(chat):
            extra["reply_to"] = reply_to
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
    only = f.pop("only", None)
    if only:  # Retry: just the agent whose reply failed
        who = [b for b in who if b in only]
    if not who:
        return True

    def role_name(b):
        return next((r.name for r in role_list(chat, b) if r.stage == stage), "")
    opts = {b: {"role": role_name(b), "stage": stage, "force": stage != "plan"} for b in who}
    # each note is written when that agent's turn starts (a Lead's lists the earlier Leads' files)
    notes = {b: (lambda b=b: stage_note(chat, b, stage, f)) for b in who}
    res = parallel(chat, who, extras=notes, opts=opts)
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


# ---------------------------------------------------------------- ⚡ Race (SPEC §14)
# Every agent that can edit (Leads and agents without a workflow role) does the same task at
# the same time, each in its own copy of the project (a git worktree). Then the Tester runs
# the tests in every copy, the Reviewer compares them (PICK: <agent>), and the user picks one
# ("Use this one"), combines two, or discards them. Race turns run in fresh sessions inside
# the copies (CLIs keep sessions per folder), given the chat so far or one shared summary.
# Nothing here may change the user's folder before "Use this one": agents in the user's folder
# during a Race/Split (Reviewer, Planner) are capped at Read.
CONTEXT_LIMIT = 30_000  # characters of chat history sent as-is; above it, one shared summary
# Diffs in a review prompt, in total. Lower on native Windows: prompts travel on the command
# line there, which is limited to 32,767 characters.
REVIEW_BUDGET = 16_000 if plat.IS_WINDOWS else 45_000
RESULT = "_combined"  # copy name of a Combine / a Split's result (agent ids never start with _)
RUNNING = ("work", "retry", "test", "review", "combine", "plan", "parts", "merge", "check", "fix")
RACE_NOTE = ("\n\n(⚡ Race: you and {others} got this same task at the same time, each in your own copy "
             "of the project. Your copy is {folder}; work only there (the user's real folder stays "
             "untouched until they pick a copy). Do the task completely, run the tests if there are "
             "any, and end with a short summary of what you did.)")
RACE_TEST_NOTE = ("\n\n(⚡ Race · Tester: this folder ({folder}) is {name}'s copy, where {name} did the task "
                  "above. Run the project's tests here. Don't write or change any files. Start your "
                  "reply with one short line with the result, e.g. \"12/12 pass\" or \"11/12 pass: "
                  "test_login fails because …\", then any details.)")
RACE_REVIEW_NOTE = ("\n\n(⚡ Race · Reviewer: {n} agents each did the user's latest request above in their own "
                    "copy of the project. Their changes and test results are below. Compare them: "
                    "correctness, completeness, code quality, tests. Say briefly which is best and why, "
                    "and what the others did better if anything. Don't change any files. End with "
                    "exactly one line: PICK: <agent id> (one of: {ids}).)\n\n{copies}")
COMBINE_NOTE = ("\n\n(⚡ Race · Combine: the user asked to combine {names}'s versions of this task in this "
                "folder ({folder}). Git merged them{conflicts}. Make one working version that keeps the "
                "best of both, make sure no conflict markers (<<<<<<< ======= >>>>>>>) are left (for a binary "
                "file, or one that a side deleted, decide which version to keep and write or delete it "
                "yourself), run the tests, and say briefly what you kept from each.)")
SUMMARY_FOR_RACE = ("\n\n(Write a summary of this group chat for agents who will continue the work in fresh "
                    "sessions: the goal, decisions, what was done (files), open questions. Markdown "
                    "bullets, at most ~300 words. Reply with the summary only.)")
PICK_RE = re.compile(r"PICK:\s*@?([\w-]+)", re.I)
RACE_VERB = {"race": "racing", "part": "building", "test": "testing", "combine": "combining"}


def worktree_dir(chat, name):
    return os.path.join(DATA_DIR, "worktrees", chat["id"], name)


def racers(chat):
    """Agents that race: they can edit and are Leads or have no workflow role (SPEC §14.1)."""
    return [b for b in agent_ids(chat) if can_edit(level_of(chat, b))
            and (has_role(chat, b, "lead") or not any(r.stage for r in role_list(chat, b)))]


def race_check(chat):
    """'' if a Race can start now, else why not (shown on the greyed-out ⚡ Race button)."""
    st = git_status(chat["folder"])
    if not st["ok"]:
        return st["reason"]
    if len(racers(chat)) < 2:
        return "needs 2 or more agents that can edit files (Leads or agents without a role)"
    if chat["id"] in workers or (chat.get("race") or {}).get("stage") in RUNNING:
        return "wait until the agents have finished"
    return ""


def race_view(chat):
    """What the page needs to show the Race (no diffs: those are fetched on demand)."""
    r = chat.get("race")
    if not r:
        return None
    keep = ("agent", "name", "status", "files", "diff", "tests", "test_warn", "msg", "from", "part", "warn")
    return {"kind": r.get("kind", "race"), "parts": r.get("parts"), "fix": r.get("fix", 0),
            "interrupted": r.get("interrupted", False),
            "stage": r["stage"], "msg": r["msg"], "pick": r.get("pick"), "used": r.get("used"),
            "copies": {n: {k: c[k] for k in keep if k in c} for n, c in r["copies"].items()}}


def discard_race(chat):
    """Delete the Race's copies, branches and snapshot (SPEC §14.5). -> problems."""
    r = chat.get("race")
    if not r:
        return []
    try:
        problems = copies.remove_copies(r["root"], [c["path"] for c in r["copies"].values()],
                                        [c["branch"] for c in r["copies"].values()], [r["ref"]])
    except Exception as e:
        problems = [str(e)]
    shutil.rmtree(os.path.join(DATA_DIR, "worktrees", chat["id"]), ignore_errors=True)
    chat["race"] = None
    return problems


def new_base(chat):
    """Snapshot the chat's folder for a Race/Split. Refuses projects with symlinks that point
    outside them: an agent writing through one in its copy would change the user's files."""
    root = copies.repo_root(chat["folder"])
    ref = f"refs/agent-groupchat/{chat['id']}/base"
    base = copies.snapshot(chat["folder"], ref)
    bad = copies.unsafe_links(root, base)
    if bad:
        copies.git(root, "update-ref", "-d", ref, check=False)
        raise ValueError("this project has symlinks that point outside it (" + ", ".join(bad[:5])
                         + "); an agent writing through one would change your real files, so "
                           "Race and Split are off here")
    return {"root": root, "scope": copies.scope_of(root, chat["folder"]), "ref": ref, "base": base,
            "submodules": copies.has_submodules(root, base)}


def note_submodules(chat, r):
    if r.get("submodules"):
        add_msg(chat, "system", "This project has git submodules. They aren't copied, so the agents' "
                                "copies have them empty, and changes to them can't be raced or split.")


def start_race(chat, text):
    """⚡ Race: post the message and snapshot the folder; the worker runs the rest."""
    with lock:
        why = race_check(chat)
        if why:
            raise ValueError(f"Can't start a Race: {why}.")
        discard_race(chat)  # the previous Race's copies go now
        try:
            info = new_base(chat)
        except ValueError as e:
            raise ValueError(f"Can't start a Race: {e}.")
        who = racers(chat)
        checkers = [b for b in stage_agents(chat, "test")[:1] + stage_agents(chat, "review")
                    if b not in who]
        to = [b for b in agent_ids(chat) if b in who or b in checkers]
        halted.discard(chat["id"])
        idx = add_msg(chat, "user", text, mode="race", to=to, status={b: "queued" for b in to})
        chat["race"] = {"msg": idx, "stage": "work", **info, "racers": who, "copies": {},
                        "pick": None, "used": None}
        note_submodules(chat, chat["race"])
        save(chat)


def race_context(chat, r):
    """The chat before the Race message: as it is if short, else one summary for everyone."""
    msgs = [m for m in chat["messages"][:r["msg"]] if not m.get("hidden")]
    text = format_msgs(chat, msgs)
    if len(text) <= CONTEXT_LIMIT:
        return text
    if not r.get("summary"):
        first = agent_ids(chat)[0]
        reply, _ = run_agent(chat, first, text[-150_000:] + SUMMARY_FOR_RACE, "read" if
                             can_use(adapter_of(chat, first), "read", sandbox_ok()) else "talk",
                             fresh=True)
        r["summary"] = reply if outcome(reply) == "done" else text[-CONTEXT_LIMIT:]
    return "(Summary of the group chat so far:)\n" + r["summary"]


def ask_copy(chat, bot, folder, prompt, role, stage, users, cap=None):
    """One turn of `bot` in a fresh session in `folder` (a copy). -> (reply, meta).
    Copies share the user's stash and branches: see copies.check_refs."""
    root = (chat.get("race") or {}).get("root")
    try:
        refs_before = copies.refs_state(root) if root else None
    except copies.GitError:
        refs_before = None
    level = level_of(chat, bot)
    if cap and rank(level) > rank(cap):
        level = cap
    set_status(chat, users, bot, "running")
    start = time.time()
    with lock:
        typing.setdefault(chat["id"], {})[bot] = True
        activity.setdefault(chat["id"], {})[bot] = {"since": start, "steps": [],
                                                    "role": RACE_VERB.get(stage, stage)}
    reply, meta = "(stopped)", {}
    try:
        if chat["id"] not in halted and not chat.get("deleted"):
            reply, meta = run_agent(chat, bot, prompt, level, folder=folder, fresh=True)
    except Exception as e:
        reply = f"(error) {e}"
    finally:
        with lock:
            typing.get(chat["id"], {})[bot] = False
            activity.get(chat["id"], {}).pop(bot, None)
    set_status(chat, users, bot, outcome(reply))
    meta = {k: v for k, v in meta.items() if v and k != "files"}
    meta.update(secs=round(time.time() - start), role=role, stage=stage)
    if users:
        meta["for"] = users
    if refs_before:
        try:
            notes = copies.check_refs(root, refs_before, chat["race"]["base"])
        except (copies.GitError, KeyError, TypeError):
            notes = []
        if notes:  # copies share the user's stash and branches
            meta["warn"] = "git stash/branches: " + "; ".join(notes[:3])
            add_msg(chat, "system", "During this turn: " + "; ".join(notes) + ".")
    return reply, meta


def diff_counts(text):
    return [text.count("\n+") - text.count("\n+++"), text.count("\n-") - text.count("\n---")]


def post_copy_reply(chat, r, bot, name, reply, meta, markers=False):
    """Commit what was done in copy `name`, post the reply with that copy's files and diff.
    markers: check that no conflict markers are left (merges); if some are, the copy can't
    be used."""
    c = r["copies"][name]
    try:
        copies.save_copy(c["path"], f"{display(chat, bot)}: {meta.get('role', '')}")
        c["files"] = copies.changed_files(r["root"], r["base"], c["branch"])
        text = copies.diff(r["root"], r["base"], c["branch"], binary=False)
    except copies.GitError as e:
        c["files"], text = [], ""
        reply += f"\n\n(error saving the copy: {e})"
    left = []
    if markers:
        left = copies.unresolved(c["path"], c["merged_at"], c.get("conflicts", [])) if c.get("merged_at") \
            else copies.conflict_markers(c["path"], c["files"])
    c["diff"] = diff_counts(text)
    if outcome(reply) != "done":
        c["status"] = outcome(reply)
    elif left:
        c["status"] = "conflicts"
        c["warn"] = meta["warn"] = "unresolved merge conflicts in: " + ", ".join(left[:5])
    else:
        c["status"] = "done" if c["files"] else "empty"
    meta.update(copy=name, files=c["files"], **({"diff": c["diff"]} if text else {}))
    idx = add_msg(chat, bot, reply, meta=meta)
    c["msg"] = idx
    if text and not chat.get("deleted"):
        os.makedirs(os.path.dirname(diff_path(chat["id"], idx)), exist_ok=True)
        with open(diff_path(chat["id"], idx), "w", encoding="utf-8") as f:
            f.write(text[:MAX_DIFF])


def human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def bring_ignored(chat, r, c):
    """Copy the user's git-ignored files (.env, node_modules, .venv…) into copy `c`, so tests
    can run there (SPEC §14.5b). The first copy tells the chat what came along.
    -> False if the copy couldn't be made safe: then it's an error and nobody works in it."""
    try:
        info = copies.copy_ignored(chat["folder"], c["folder"])
    except OSError as e:
        c["status"] = "error"
        # tagged like the turn it replaces: a racer / a part can be retried, a combined copy can't
        kind = ("combine", "⚡ Combine") if "from" in c else ("part", "🧩 Split") if "part" in c \
            else ("race", "⚡ Race")
        name = next((n for n, x in r["copies"].items() if x is c), c["agent"])
        add_msg(chat, c["agent"], f"(error) {c['name']}'s copy couldn't be made safe to use "
                                  f"(copying your git-ignored files: {e}), so it isn't used.",
                meta={"role": kind[1], "stage": kind[0], "copy": name})
        if kind[0] != "combine":
            set_status(chat, [r["msg"]], c["agent"], "error")
        return False
    with lock:
        first = "ignored" not in r
        if first:
            r["ignored"] = {k: info[k] for k in ("names", "skipped", "files", "bytes", "links", "failed")}
    if not first or not (info["names"] or info["skipped"] or info["links"] or info["failed"]):
        return True
    names = ", ".join(info["names"][:6]) + (f" and {len(info['names']) - 6} more"
                                             if len(info["names"]) > 6 else "")
    text = (f"Your git-ignored files were copied into each copy so tests can run there: {names} "
            f"({info['files']} files, {human_size(info['bytes'])}). They stay out of what you Use."
            if info["names"] else "")
    if info["skipped"]:
        text += (" " if text else "") + "Not copied (too big): " + skipped_text(info["skipped"]) + "."
    if info["links"]:
        text += ((" " if text else "") + "Not copied (links that lead outside the copy): "
                 + ", ".join(info["links"][:6]) + (f" and {len(info['links']) - 6} more"
                                                   if len(info["links"]) > 6 else "") + ".")
    if info["failed"]:
        text += (" " if text else "") + "Couldn't be copied: " + ", ".join(info["failed"][:6]) + "."
    add_msg(chat, "system", text)
    return True


def skipped_text(skipped):
    return ", ".join(f"{n} ({human_size(b) if b is not None else 'over ' + human_size(copies.MAX_IGNORED_BYTES)})"
                     for n, b in skipped[:6]) + (f" and {len(skipped) - 6} more" if len(skipped) > 6 else "")


def ignored_note(r):
    """Told to agents in copies: ignored folders that were too big to copy."""
    skipped = (r.get("ignored") or {}).get("skipped") or []
    if not skipped:
        return ""
    return ("\n\n(Not in this copy because they're too big: " + skipped_text(skipped)
            + ". The user's project has them but git ignores them. If you need them (e.g. "
            "installed dependencies) install or build them here if your permissions allow; "
            "otherwise say so.)")


def open_copy(chat, r, name, agent_id, extra=None):
    """Make (or remake) copy `name` from the snapshot. -> its record, or None if the chat was
    deleted / the Race replaced meanwhile (then the copy is removed again)."""
    path, branch = worktree_dir(chat, name), f"agent/{chat['id']}/{name}"
    c = {"agent": agent_id, "name": display(chat, agent_id), "path": path, "branch": branch,
         "status": "working", **(extra or {})}
    with lock:
        r["copies"][name] = c  # registered first: cleanup knows about it whatever happens
    try:
        c["folder"] = copies.make_copy(r["root"], r["base"], path, branch, r["scope"])
    except copies.GitError as e:
        c["status"] = "error"
        add_msg(chat, agent_id, f"(error) couldn't make a copy of the project: {e}",
                meta={"role": "⚡ Race", "stage": "race", "copy": name})
        return None
    if chat.get("deleted") or chat.get("race") is not r:
        copies.remove_copies(r["root"], [path], [branch])
        return None
    if not bring_ignored(chat, r, c):
        return None
    return c


def run_racer(chat, r, b, context, task):
    c = open_copy(chat, r, b, b)
    if not c:
        return
    others = names_of(chat, [x for x in r["racers"] if x != b]) or "the others"
    prompt = ((context + "\n\n") if context else "") + f"[User]: {task}" + \
        RACE_NOTE.format(others=others, folder=c["folder"]) + ignored_note(r)
    reply, meta = ask_copy(chat, b, c["folder"], prompt, "⚡ Race", "race", [r["msg"]])
    post_copy_reply(chat, r, b, b, reply, meta)


def race_work(chat, r):
    """Every racer does the task in its own copy, all at the same time."""
    context, task = race_context(chat, r), chat["messages"][r["msg"]]["text"]
    ts = [threading.Thread(target=run_racer, args=(chat, r, b, context, task)) for b in r["racers"]]
    for t in ts: t.start()
    for t in ts: t.join()


def race_test(chat, r):
    """The (first) Tester runs the tests in every copy that has changes, one after another.
    Whatever it changes or commits in a copy is undone: the copy stays as its agent left it."""
    testers = [b for b in stage_agents(chat, "test") if b not in r["racers"]][:1]
    task = chat["messages"][r["msg"]]["text"]
    only = r.pop("retest", None)
    for tester in testers:
        for name, c in list(r["copies"].items()):
            if c["status"] != "done" or chat["id"] in halted or name.startswith(RESULT) \
                    or (only and name not in only):
                continue
            before = copies.head(c["path"])
            prompt = f"[User]: {task}" + RACE_TEST_NOTE.format(folder=c["folder"], name=c["name"]) \
                + ignored_note(r)
            reply, meta = ask_copy(chat, tester, c["folder"], prompt,
                                   f"Tester · {c['name']}'s copy", "test", [r["msg"]])
            meta["copy"] = name
            changed = copies.head(c["path"]) != before or \
                copies.git(c["path"], "status", "--porcelain", check=False).strip()
            if changed:
                copies.reset_copy(c["path"], before)
                meta["warn"] = "the Tester changed files in this copy; they were undone"
                c["test_warn"] = True
            c["tests"] = first_line(reply)[2:][:160] if outcome(reply) == "done" else reply[:160]
            add_msg(chat, tester, reply, meta=meta)


def review_diffs(chat, r, names):
    budget = REVIEW_BUDGET // max(1, len(names))
    parts = []
    for n in names:
        c = r["copies"][n]
        text = copies.diff(r["root"], r["base"], c["branch"], binary=False)
        if len(text) > budget:
            text = text[:budget] + "\n… (cut: the diff is longer)"
        parts.append(f"=== {c['name']} (id {n}) · tests: {c.get('tests', 'not run')} ===\n{text}")
    return "\n\n".join(parts)


def race_review(chat, r):
    """Each Reviewer compares all copies (diffs + test results) and picks one (read-only)."""
    done = [n for n, c in r["copies"].items() if c["status"] == "done" and not n.startswith(RESULT)]
    if not done:
        return
    note = RACE_REVIEW_NOTE.format(n=len(done), ids=", ".join(done), copies=review_diffs(chat, r, done))
    for rev in [b for b in stage_agents(chat, "review") if b not in r["racers"]]:
        if chat["id"] in halted:
            return
        reply = ask(chat, rev, note, cap="read", role="Reviewer · ⚡ Race", stage="review-race", force=True,
                    race=True)
        picks = PICK_RE.findall(reply or "")
        pick = picks[-1].lower() if picks else None
        if pick in done and not r.get("pick"):
            r["pick"] = pick


def merge_into_result(chat, r, names, fixer):
    """A new result copy with `names` merged in. -> (record, conflicts) or (None, error)."""
    k = 1 + sum(1 for n in r["copies"] if n.startswith(RESULT))
    name = RESULT if k == 1 else f"{RESULT}-{k}"
    label = " + ".join(r["copies"][n]["name"] for n in names)
    path, branch = worktree_dir(chat, name), f"agent/{chat['id']}/{name}"
    c = {"agent": fixer, "name": f"Combined ({label})", "path": path, "branch": branch,
         "status": "working", "from": names, "label": label}
    with lock:
        r["copies"][name] = c  # registered before merging: cleanup knows about it
    try:
        conflicts = copies.merge_copies(r["root"], r["base"], [r["copies"][n]["branch"] for n in names],
                                        path, branch, r["scope"])
    except copies.GitError as e:
        c["status"] = "error"
        add_msg(chat, "system", f"Couldn't merge {label}: {e}")
        return None, str(e)
    c["folder"] = path if r["scope"] == "." else os.path.join(path, *r["scope"].split("/"))
    c["key"], c["conflicts"], c["merged_at"] = name, conflicts, copies.head(path)
    if not bring_ignored(chat, r, c):
        return None, "the combined copy couldn't be made safe"
    return c, conflicts


def race_combine(chat, r):
    """Merge the chosen copies into a new one; an agent fixes clashes and checks it."""
    names = r.pop("combine", None)
    if not names:  # (already being combined)
        return
    fixer = next((b for b in r["racers"] if has_role(chat, b, "lead")), r["racers"][0])
    c, conflicts = merge_into_result(chat, r, names, fixer)
    if not c:
        return
    task = chat["messages"][r["msg"]]["text"]
    prompt = f"[User]: {task}" + COMBINE_NOTE.format(
        names=c["label"], folder=c["folder"],
        conflicts=f"; these files still have conflict markers: {', '.join(conflicts)}"
        if conflicts else " without conflicts") + ignored_note(r)
    reply, meta = ask_copy(chat, fixer, c["folder"], prompt, "⚡ Combine", "combine", [])
    post_copy_reply(chat, r, fixer, c["key"], reply, meta, markers=True)


def race_retry(chat, r):
    """Run one failed racer / Split part again, in its own copy (never in the user's folder)."""
    name = r.pop("retry", None)
    c = r["copies"].get(name) if name else None
    if not c:
        return
    context, task = race_context(chat, r), chat["messages"][r["msg"]]["text"]
    b = c["agent"]
    if r.get("kind") == "split":
        run_part(chat, r, b, context, task)
        for n in [n for n in r["copies"] if n.startswith(RESULT)]:  # merge again from scratch
            old = r["copies"].pop(n)
            copies.remove_copies(r["root"], [old["path"]], [old["branch"]])
    else:
        run_racer(chat, r, b, context, task)
        r["pick"], r["retest"] = None, [name]


def step_race(chat):
    """Run the next stage of the Race or Split. -> True if it did something (call again)."""
    r = chat.get("race")
    if not r or r["stage"] not in RUNNING:
        return False
    stage = r["stage"]
    try:
        if stage == "retry":
            race_retry(chat, r)
            r["stage"] = ("merge" if all_parts_ok(chat, r) else "stopped") if r.get("kind") == "split" \
                else "test"
        elif r.get("kind") == "split":
            step_split(chat, r)
        elif stage == "work":
            race_work(chat, r)
            r["stage"] = "test"
        elif stage == "test":
            race_test(chat, r)
            r["stage"] = "review"
        elif stage == "review":
            race_review(chat, r)
            r["stage"] = "done"
        elif stage == "combine":
            race_combine(chat, r)
            r["stage"] = "done"
    except (copies.GitError, OSError) as e:
        add_msg(chat, "system", f"The {'Split' if r.get('kind') == 'split' else 'Race'} stopped: {e}")
        r["stage"] = "stopped"
    r = chat.get("race")  # (a Split that ran normally after all has no race any more)
    if r and chat["id"] in halted and r["stage"] != "done":
        r["stage"] = "stopped"
    with lock:
        save(chat)
    return True


def race_action(chat, d):
    """The compare view's buttons: use / combine / discard. -> (code, response).
    Checks and state changes happen under the lock, so a double click can't do things twice."""
    with lock:
        r, action = chat.get("race"), d.get("action")
        if not r:
            return 409, {"error": "There's no Race or Split in this chat."}
        busy = chat["id"] in workers or r["stage"] in RUNNING
        if action == "discard" and split_waiting(chat):
            cancel_split(chat)
            return 200, {"ok": True, "problems": []}
        if action == "discard":
            if busy:
                return 409, {"error": "Wait until the agents have finished (or press Stop)."}
            problems = discard_race(chat)
            add_msg(chat, "system", "The copies were discarded." + (
                " Some couldn't be removed: " + "; ".join(problems) if problems else ""))
            return 200, {"ok": True, "problems": problems}
        if busy:
            return 409, {"error": "Wait until the agents have finished."}
        if action == "use":
            c = r["copies"].get(d.get("copy"))
            if not c or c["status"] != "done":
                return 400, {"error": "That copy has no finished changes."}
            ok, files, why = copies.use_copy(r["root"], r["base"], c["branch"], r["scope"])
            if not ok:
                if why.startswith("error"):
                    return 500, {"error": "Couldn't copy the changes, so your folder was put back as "
                                          f"it was: {why[7:]}"}
                if why == "outside":
                    return 409, {"error": "This copy changed files outside the chat's folder, so "
                                          "nothing was copied: " + ", ".join(files[:10]), "clash": files}
                return 409, {"error": "You changed the same lines in these files since it started, "
                                      "so nothing was copied: " + ", ".join(files)
                                      + ". Commit or undo your changes there first.", "clash": files}
            r["used"] = d["copy"]
            whose = f"the {c['name'][0].lower()}{c['name'][1:]} copy" if c.get("from") else f"{c['name']}'s copy"
            add_msg(chat, "system", f"You used {whose}: its changes are now in your folder "
                                    f"(not committed): {', '.join(files) or 'no files'}.")
            return 200, {"ok": True, "files": files}
        if action == "combine":
            if r.get("kind") == "split":
                return 400, {"error": "Combine is for a Race; a Split combines its parts by itself."}
            names = d.get("copies")
            if not isinstance(names, list) or len(set(names)) < 2 or any(
                    r["copies"].get(n, {}).get("status") != "done" for n in names):
                return 400, {"error": "Pick 2 or more finished copies to combine."}
            r["combine"], r["stage"] = list(dict.fromkeys(names)), "combine"
            halted.discard(chat["id"])
            save(chat)
    if action == "combine":
        kick(chat)
        return 200, {"ok": True}
    return 400, {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------- 🧩 Split (SPEC §14.4)
# The task is divided into independent parts (by the Planner, or by the user), each agent builds
# its part in its own copy at the same time, the parts are merged into one combined copy (the
# Lead fixes clashes), then the Tester and Reviewer check the combined result with the chat's
# normal fix rounds. Uses the Race's copies, cards, "Use this" and Discard.
SPLIT_PLAN_NOTE = ("\n\n(🧩 Split · Planner: the user wants the latest request split into parts that agents "
                   "build at the same time, each in its own copy of the project. Agents who can take a "
                   "part: {who}. Split it into independent parts: no part may need another part to be "
                   "finished first. At most one part per agent; not every agent needs one. Don't change "
                   "any files. Explain the split briefly, then list the parts at the end, one per line, "
                   "exactly like:\nPART @{ex}: <what this agent builds, with file names>\n"
                   "If the task can't be split into independent parts, say why and end with the line: "
                   "NO SPLIT)")
SPLIT_PART_NOTE = ("\n\n(🧩 Split: the task above was divided into parts that are built at the same time, each "
                   "in its own copy of the project. YOUR part: {part}\nThe other parts (don't do them): "
                   "{others}\nYour copy is {folder}; work only there. Build only your part, completely, "
                   "run the tests if there are any, and end with a short summary of what you did.)")
SPLIT_MERGE_NOTE = ("\n\n(🧩 Split · merge: the parts ({parts}) were built separately and git merged them in "
                    "this folder ({folder}); these files have conflict markers: {conflicts}. Fix the "
                    "clashes so all parts work together, make sure no conflict markers (<<<<<<< ======= "
                    ">>>>>>>) are left (for a binary file, or one that a part deleted, decide which "
                    "version to keep and write or delete it yourself), run the tests, and say briefly "
                    "what you did.)")
SPLIT_TEST_NOTE = ("\n\n(🧩 Split · Tester: this folder ({folder}) has the combined result of all parts of the "
                   "task above. Write or update tests for it and run them. Only change test files, never "
                   "the code under test. Report what fails. End your reply with exactly one line: "
                   "VERDICT: LGTM if everything passes, or VERDICT: FIX if something must change.)")
SPLIT_REVIEW_NOTE = ("\n\n(🧩 Split · Reviewer: the task above was split into parts ({parts}) that were built "
                     "separately and merged. Below is the combined change and the Tester's result. Check it "
                     "against what the user asked: do the parts work together, bugs, missing pieces, risky "
                     "edits. Don't modify files. Say concretely what to fix. End your reply with exactly one "
                     "line: VERDICT: LGTM if nothing needs fixing, or VERDICT: FIX if something must change."
                     ")\n\nTests: {tests}\n\n{diff}")
SPLIT_FIX_NOTE = ("\n\n(🧩 Split · fix round {k} of {n}: this folder ({folder}) has the combined result of "
                  "the task above. The checks found problems:\n\n{findings}\n\nFix them here, run the "
                  "tests, and say briefly what you changed.)")
PART_RE = re.compile(r"^\s*(?:[-*•]\s*)?\**PART\**\s+@?([\w-]+)\s*\**\s*:\s*(.+?)\s*$", re.I | re.M)
NO_SPLIT_RE = re.compile(r"^\s*\**NO SPLIT\**\s*$", re.I | re.M)


def split_check(chat, by_planner=False):
    """'' if a Split can start now, else why not."""
    why = race_check(chat)
    if why:
        return why
    if by_planner and not any(has_role(chat, b, "planner") for b in agent_ids(chat)):
        return "needs an agent with the Planner role (or split it yourself)"
    return ""


def checkers_of(chat, builders):
    return [b for b in stage_agents(chat, "test")[:1] + stage_agents(chat, "review") if b not in builders]


def start_split(chat, text, parts=None):
    """🧩 Split. parts: {agent id: its part} (the user's own split), or None (the Planner splits)."""
    who = racers(chat)
    if parts is not None:
        if not isinstance(parts, dict):
            raise ValueError("parts must be an object: {agent id: part}")
        parts = {b: str(t).strip()[:4000] for b, t in parts.items() if str(t or "").strip()}
        bad = [b for b in parts if b not in who]
        if bad:
            raise ValueError(f"These agents can't take a part (they can't edit files): {', '.join(bad)}")
        if len(parts) < 2:
            raise ValueError("Give at least 2 agents a part (or use a normal message).")
    with lock:
        why = split_check(chat, by_planner=parts is None)
        if why:
            raise ValueError(f"Can't start a Split: {why}.")
        planner = None if parts else next(b for b in agent_ids(chat) if has_role(chat, b, "planner"))
        discard_race(chat)
        try:
            info = new_base(chat)
        except ValueError as e:
            raise ValueError(f"Can't start a Split: {e}.")
        builders = list(parts) if parts else who
        to = [b for b in agent_ids(chat) if b in builders or b in checkers_of(chat, builders) or b == planner]
        halted.discard(chat["id"])
        idx = add_msg(chat, "user", text, mode="split", to=to,
                      status={b: "waiting" if planner and b != planner else "queued" for b in to})
        chat["race"] = {"kind": "split", "msg": idx, "stage": "plan" if planner else "parts",
                        "planner": planner, "parts": parts or {}, **info, "racers": builders,
                        "copies": {}, "pick": None, "used": None, "fix": 0}
        note_submodules(chat, chat["race"])
        save(chat)


def split_feedback(chat, text):
    """A reply while the Planner's split waits for the OK: back to the Planner."""
    r = chat["race"]
    with lock:
        add_msg(chat, "user", text, mode="split-feedback", to=[r["planner"]],
                status={r["planner"]: "queued"})
        r["stage"] = "plan"
        save(chat)


def split_waiting(chat):
    r = chat.get("race")
    return bool(r and r.get("kind") == "split" and r["stage"] == "await")


def split_go(chat):
    """The user OK'd the Planner's parts (atomic: a second Go finds nothing waiting)."""
    with lock:
        if not split_waiting(chat):
            return False
        r = chat["race"]
        r["stage"] = "parts"
        for b in r["racers"] + checkers_of(chat, r["racers"]):
            set_status(chat, [r["msg"]], b, "queued", only=("waiting",))
        save(chat)
    return True


def cancel_split(chat):
    """Stop / Discard while the parts wait for the OK."""
    with lock:
        r = chat["race"]
        m = chat["messages"][r["msg"]]
        for b, st in (m.get("status") or {}).items():
            if st in ("waiting", "queued"):
                m["status"][b] = "skipped"
        discard_race(chat)
        add_msg(chat, "system", "The Split was cancelled.")


def split_plan(chat, r):
    """The Planner proposes the parts (read-only); then wait for the OK, or run normally."""
    who = racers(chat)
    note = SPLIT_PLAN_NOTE.format(who=", ".join(f"{display(chat, b)} (@{b})" for b in who), ex=who[0])
    reply = ask(chat, r["planner"], note, cap="read", role="Planner · 🧩 Split", stage="plan-split",
                force=True, race=True) or ""
    if outcome(reply) != "done":
        r["stage"] = "stopped"
        return
    parts = {}
    for b, text in PART_RE.findall(reply):
        b = b.lower()
        if b in who and b not in parts:
            parts[b] = text
    if NO_SPLIT_RE.search(reply) or len(parts) < 2:
        run_normally(chat, r, "The Planner didn't split this task into 2 or more independent parts, "
                              "so the Lead does it as a normal message.")
        return
    r["parts"], r["racers"], r["stage"] = parts, list(parts), "await"
    m = chat["messages"][r["msg"]]
    keep = set(parts) | {r["planner"]} | set(checkers_of(chat, list(parts)))
    for b in list(m.get("status") or {}):  # agents without a part have nothing to do
        if b not in keep and m["status"][b] == "waiting":
            m["status"][b] = "skipped"


def run_normally(chat, r, why):
    """NO SPLIT: one agent does the task the normal way: the Lead (with the usual Tester and
    Reviewer after it), or the first agent that can edit if there's no Lead."""
    m = chat["messages"][r["msg"]]
    lead = next((b for b in agent_ids(chat) if has_role(chat, b, "lead")), None)
    with lock:
        status = {b: st for b, st in (m.get("status") or {}).items() if b == r.get("planner")}
        m["mode"] = "no-split"
        if lead and has_flow(chat):
            m["flow"] = True
            stages = [s for s in flow_stages(chat) if s not in ("plan", "await")]
            chat["flow"] = {"msg": r["msg"], "stages": stages, "i": 0, "fix": 0, "waiting": False}
            to = [b for b in agent_ids(chat) if b == lead or (b in checkers_of(chat, [lead]))]
        else:
            m["only"] = lead or racers(chat)[0]
            to = [m["only"]]
        for b in to:
            status.setdefault(b, "queued")
        m["status"], m["to"] = status, list(status)
        add_msg(chat, "system", why)
        discard_race(chat)


def run_part(chat, r, b, context, task):
    c = open_copy(chat, r, b, b, {"part": r["parts"][b]})
    if not c:
        return
    others = "; ".join(f"{display(chat, x)}: {t}" for x, t in r["parts"].items() if x != b) or "none"
    prompt = ((context + "\n\n") if context else "") + f"[User]: {task}" + \
        SPLIT_PART_NOTE.format(part=r["parts"][b], others=others, folder=c["folder"]) + ignored_note(r)
    reply, meta = ask_copy(chat, b, c["folder"], prompt, "🧩 Part", "part", [r["msg"]])
    post_copy_reply(chat, r, b, b, reply, meta)


def all_parts_ok(chat, r):
    """Every part finished (or had nothing to change)? Otherwise say which failed and stop:
    a result with a part missing must not look ready."""
    failed = [c["name"] for n, c in r["copies"].items()
              if not n.startswith(RESULT) and c["status"] not in ("done", "empty")]
    if failed:
        add_msg(chat, "system", f"🧩 {', '.join(failed)}'s part didn't finish, so the parts weren't merged. "
                                "Press Retry under that reply, or Discard.")
    return not failed


def split_parts(chat, r):
    """Every agent with a part builds it in its own copy, all at the same time."""
    context, task = race_context(chat, r), chat["messages"][r["msg"]]["text"]
    ts = [threading.Thread(target=run_part, args=(chat, r, b, context, task)) for b in r["parts"]]
    for t in ts: t.start()
    for t in ts: t.join()


def split_fixer(chat, r):
    return next((b for b in r["parts"] if has_role(chat, b, "lead")), next(iter(r["parts"])))


def split_merge(chat, r):
    """Merge the finished parts into one result copy; the Lead fixes clashes.
    -> the next stage."""
    done = [n for n, c in r["copies"].items() if c["status"] == "done" and not n.startswith(RESULT)]
    if not done:
        add_msg(chat, "system", "🧩 No part changed any files, so there's nothing to merge.")
        return "stopped"
    c, conflicts = merge_into_result(chat, r, done, split_fixer(chat, r))
    if not c:
        return "stopped"
    if conflicts:
        prompt = f"[User]: {chat['messages'][r['msg']]['text']}" + SPLIT_MERGE_NOTE.format(
            parts=c["label"], folder=c["folder"], conflicts=", ".join(conflicts)) + ignored_note(r)
        reply, meta = ask_copy(chat, c["agent"], c["folder"], prompt, "🧩 Merge", "combine", [])
        post_copy_reply(chat, r, c["agent"], c["key"], reply, meta, markers=True)
        if c["status"] != "done":
            add_msg(chat, "system", "🧩 The merged result isn't usable: " +
                    (c.get("warn") or f"the merge fix {c['status']}") + ".")
            return "stopped"
    else:
        c["files"] = copies.changed_files(r["root"], r["base"], c["branch"])
        c["diff"] = diff_counts(copies.diff(r["root"], r["base"], c["branch"], binary=False))
        c["status"] = "done"
        add_msg(chat, "system", f"🧩 The parts ({c['label']}) merged without clashes.")
    r["result"] = c["key"]
    return "check"


def split_check_result(chat, r):
    """Tester + Reviewer check the result copy; FIX verdicts -> fix rounds (chat setting)."""
    c = r["copies"].get(r.get("result"))
    if not c or c["status"] != "done":
        return "stopped"
    task = chat["messages"][r["msg"]]["text"]
    findings, tests, unfinished = [], "not run", []
    for tester in [b for b in stage_agents(chat, "test") if b not in r["parts"]][:1]:
        before = copies.head(c["path"])
        reply, meta = ask_copy(chat, tester, c["folder"], f"[User]: {task}" +
                               SPLIT_TEST_NOTE.format(folder=c["folder"]) + ignored_note(r),
                               "Tester · 🧩 Split", "test", [r["msg"]])
        copies.save_copy(c["path"], "tests")
        changed = [p for _, p in copies.name_status(c["path"], before, "HEAD")]
        other = [f for f in changed if not is_test_file(f)]
        if other:
            meta["warn"] = "the Tester changed non-test files: " + ", ".join(other[:5])
        v = verdict(reply)
        if v:
            meta["verdict"] = v
        meta["copy"] = r["result"]
        add_msg(chat, tester, reply, meta=meta)
        tests = first_line(reply)[2:][:160] if outcome(reply) == "done" else reply[:160]
        c["tests"] = tests
        if outcome(reply) != "done":
            unfinished.append(f"{display(chat, tester)} (Tester) {outcome(reply)}")
        if v == "FIX":
            findings.append(f"[{display(chat, tester)}, tester]: {reply}")
    c["files"] = copies.changed_files(r["root"], r["base"], c["branch"])
    c["diff"] = diff_counts(copies.diff(r["root"], r["base"], c["branch"], binary=False))
    text = copies.diff(r["root"], r["base"], c["branch"], binary=False)
    if len(text) > REVIEW_BUDGET:
        text = text[:REVIEW_BUDGET] + "\n… (cut: the diff is longer)"
    note = SPLIT_REVIEW_NOTE.format(parts=", ".join(display(chat, b) for b in r["parts"]),
                                    tests=tests, diff=text)
    for rev in [b for b in stage_agents(chat, "review") if b not in r["parts"]]:
        reply = ask(chat, rev, note, cap="read", role="Reviewer · 🧩 Split", stage="review", force=True,
                    race=True) or ""
        if outcome(reply) != "done":
            unfinished.append(f"{display(chat, rev)} (Reviewer) {outcome(reply)}")
        if verdict(reply) == "FIX":
            findings.append(f"[{display(chat, rev)}, reviewer]: {reply}")
    if unfinished or chat["id"] in halted:
        c["warn"] = "not fully checked: " + (", ".join(unfinished) or "stopped")
        add_msg(chat, "system", f"🧩 The checks didn't finish ({', '.join(unfinished) or 'stopped'}), "
                                "so the result isn't marked ready. You can still look at it and use it.")
        return "stopped"
    if findings and r["fix"] < chat.get("fix_rounds", 1):
        r["findings"] = "\n\n".join(findings)
        return "fix"
    return "done"


def split_fix(chat, r):
    """One fix round in the result copy. -> the next stage."""
    r["fix"] += 1
    c = r["copies"][r["result"]]
    prompt = f"[User]: {chat['messages'][r['msg']]['text']}" + SPLIT_FIX_NOTE.format(
        k=r["fix"], n=chat.get("fix_rounds", 1), folder=c["folder"], findings=r.pop("findings", ""))
    reply, meta = ask_copy(chat, c["agent"], c["folder"], prompt, f"🧩 Fix round {r['fix']}", "combine", [])
    post_copy_reply(chat, r, c["agent"], r["result"], reply, meta, markers=True)
    if c["status"] != "done":
        add_msg(chat, "system", "🧩 The fix round left the result unusable: " +
                (c.get("warn") or f"it {c['status']}") + ".")
        return "stopped"
    return "check"


def step_split(chat, r):
    stage = r["stage"]
    if stage == "plan":
        split_plan(chat, r)
    elif stage == "parts":
        split_parts(chat, r)
        r["stage"] = "merge" if all_parts_ok(chat, r) else "stopped"
    elif stage == "merge":
        r["stage"] = split_merge(chat, r)
    elif stage == "check":
        r["stage"] = split_check_result(chat, r)
    elif stage == "fix":
        r["stage"] = split_fix(chat, r)


def set_agents(chat, specs):
    """Change roles, permission overrides and focuses:
    [{"id", "roles": [...], "level": None|level, "focus": None|text}]."""
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
        focus = clean_focus(sp.get("focus", a.get("focus")), a["name"])
        changes[a["id"]] = (roles, level, focus)
    with lock:
        for aid, (roles, level, focus) in changes.items():
            a = agent(chat, aid)
            a["roles"], a["level"], a["focus"] = roles, level, focus
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
    me = threading.current_thread()
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
                    if chat.get("race") and chat["race"]["stage"] not in ("done", "stopped"):
                        chat["race"]["stage"] = "stopped"
                    save(chat)
                    if workers.get(chat["id"]) is me:
                        workers.pop(chat["id"])
                    return
            targets, sk = pending(chat)
            if targets:  # direct answers first: @mentions, skills, Participants
                parallel(chat, targets)
                parts = [b for b in targets if b in participants(chat)]
                if len(parts) >= 2 and not sk:  # no auto back-and-forth during a skill
                    discuss(chat, parts)
                handoffs(chat, targets)
                continue
            if step_race(chat):
                continue
            if step_flow(chat):
                continue
            with lock:  # check and leave together: work that arrives now is seen, or kick() starts a new worker
                if has_work(chat):
                    continue
                if workers.get(chat["id"]) is me:
                    workers.pop(chat["id"])
                return
    finally:
        with lock:
            if workers.get(chat["id"]) is me:
                workers.pop(chat["id"], None)


def has_work(chat):
    """Is there anything for the worker to do (a reply owed, a Race/Split or flow stage)?"""
    if chat["id"] in halted:
        return False
    if pending(chat)[0]:
        return True
    r = chat.get("race")
    if r and r["stage"] in RUNNING:
        return True
    f = chat.get("flow")
    return bool(f and not f.get("waiting"))


def kick(chat, first=None):
    with lock:
        if chat["id"] in workers:
            return False
        t = threading.Thread(target=worker, args=(chat, first), daemon=True)
        workers[chat["id"]] = t
        t.start()
        return True


def retry_block(chat, idx):
    """'' if the failed reply `idx` can be retried, else why not."""
    meta = chat["messages"][idx].get("meta") or {}
    if meta.get("race"):
        return ("this reply was part of a Race/Split; start a new one (or Discard), because a retry "
                "would run in your real folder")
    if not meta.get("copy"):
        return ""
    r = chat.get("race")
    if not r or meta["copy"] not in r["copies"]:
        return "this Race/Split's copies are gone; start a new one"
    if meta.get("stage") not in ("race", "part"):
        return "start a new Race/Split for this (only a racer's or a part's reply can be retried)"
    if r["stage"] in RUNNING or chat["id"] in workers:
        return "wait until the agents have finished"
    return ""


def retry(chat, idx):
    """Ask the same agent again for a reply that failed (error / stopped / timed out).
    A failed workflow step continues the workflow from that step; a failed racer or Split part
    runs again in its own copy."""
    m = chat["messages"][idx]
    bot, meta = m["who"], m.get("meta") or {}
    if meta.get("copy"):
        with lock:
            m["retried"] = True
            r = chat["race"]
            r["retry"], r["stage"] = meta["copy"], "retry"
            set_status(chat, [r["msg"]], bot, "queued")
            save(chat)
        return
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
        if (m.get("meta") or {}).get("choices"):
            out += ["Choices: " + " · ".join(f"[{o}]" for o in m["meta"]["choices"]), ""]
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
                               "agents": len(c["agents"]),
                               "count": sum(1 for m in c["messages"] if not m.get("hidden"))}
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
        m = re.fullmatch(r"/api/chats/(\w+)/race/diff/([\w-]+)", p)
        if m:  # one Race copy's whole diff (the compare view's ▸ Diff)
            c = chats.get(m.group(1))
            r = c and c.get("race")
            cp = r and r["copies"].get(m.group(2))
            if not cp:
                return self.send_json({"error": "no such copy"}, 404)
            try:
                body = copies.diff(r["root"], r["base"], cp["branch"], binary=False).encode()
            except copies.GitError as e:
                return self.send_json({"error": str(e)}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
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
            git_now = git_status(c["folder"])  # (outside the lock: git may be slow)
            with lock:
                return self.send_json({**c, "typing": typing.get(c["id"], {}),
                                       "aliases": aliases(c),
                                       "perm": {b: level_info(c, b) for b in agent_ids(c)},
                                       "git": git_now,
                                       "race": race_view(c), "race_block": race_check(c),
                                       "racers": racers(c),
                                       "split_block": split_check(c),
                                       "split_planner": not split_check(c, by_planner=True),
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
        except (copies.GitError, OSError) as e:  # git or the file system said no
            print("failed", self.path, repr(e), flush=True)
            return self.send_json({"error": f"That didn't work: {e}"}, 500)

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
        if p == "/api/overlaps":  # the page's agent rows -> who would do the same work
            refresh_adapters()
            return self.send_json({"overlaps": overlaps_for_specs(d.get("agents"))})
        if p == "/api/legacy":
            if d.get("action") == "import":
                return self.send_json({"ok": True, "imported": import_legacy()})
            cfg.update(CONFIG_FILE, legacy_import="dismissed")
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/chats/(\w+)/(send|settings|agents|go|delete|stop|retry|compact|git-init|race)", p)
        c = chats.get(m.group(1)) if m else None
        if not c:
            return self.send_json({"error": "not found"}, 404)
        action = m.group(2)
        if action == "send":
            text = str(d.get("text") or "").strip() if isinstance(d.get("text"), str) else ""
            if not text:
                return self.send_json({"error": "empty"}, 400)
            sk = parse_skill(text, c["folder"])
            reply_to = d.get("reply_to") if d.get("reply_to") in agent_ids(c) else None
            with lock:
                halted.discard(c["id"])
            r = c.get("race")
            if d.get("mode") == "race":
                try:
                    start_race(c, text)
                except (ValueError, copies.GitError) as e:
                    return self.send_json({"error": str(e)}, 400)
            elif d.get("mode") == "split":
                try:
                    start_split(c, text, d.get("parts") if d.get("split") == "own" else None)
                except (ValueError, copies.GitError) as e:
                    return self.send_json({"error": str(e)}, 400)
            elif split_waiting(c) and not sk and (reply_to == r["planner"] if reply_to else
                                                  mentioned(c, text) in ([], [r["planner"]])):
                split_feedback(c, text)  # a reply to the Planner's parts: it revises them
            else:
                post_user_message(c, text, sk, reply_to)
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
                why = retry_block(c, i)
                if why:
                    return self.send_json({"error": f"Can't retry this: {why}."}, 400)
                kick(c, lambda: retry(c, i))
            else:
                kick(c, lambda: compact(c))
        elif action == "race":  # the compare view: use / combine / discard
            code, out = race_action(c, d)
            return self.send_json(out, code)
        elif action == "git-init":  # "Initialize git here" (for ⚡ Race / 🧩 Split)
            try:
                copies.init_repo(c["folder"])
            except (copies.GitError, OSError) as e:
                return self.send_json({"error": f"Couldn't initialize git: {e}"}, 400)
            return self.send_json({"ok": True, "git": git_status(c["folder"], fresh=True)})
        elif action == "go":  # the plan (or the Split's parts) is OK: start the work
            if split_go(c):
                kick(c)
            elif not go_ahead(c):
                return self.send_json({"error": "Nothing is waiting for your OK."}, 409)
            kick(c)
        elif action == "agents":  # change roles / permission overrides
            try:
                set_agents(c, d.get("agents"))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        elif action == "stop":
            if split_waiting(c):  # nothing runs yet: cancel the Split
                cancel_split(c)
                return self.send_json({"ok": True})
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
                discard_race(c)
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
