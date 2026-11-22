"""~/.agent-groupchat/config.json: chat APIs, custom roles and saved teams.

{"apis":  [{"id", "name", "base_url", "model", "key_env"}],      # see adapters/openai_api.py
 "roles": [{"name", "job", "level"}],                             # custom roles
 "teams": [{"name", "agents": [...], "lock", "fix_rounds", "rounds"}],
 "legacy_import": "done" | "dismissed"}                           # old ~/groupchat chats
Written atomically; sections the page didn't send are kept as they are.
"""
import json
import os
import re
import threading

from .roles import BUILTIN, LEVELS

_lock = threading.Lock()
NAME_RE = re.compile(r"^[\w .'+&-]{1,40}$")
TEAM_RE = re.compile(r"^[\w .,'+&()/-]{1,40}$")
# key_env must be a variable NAME; refuse things that look like the key itself
KEY_LIKE_RE = re.compile(r"^(sk|pk|rk|key|AIza|gsk|xai)[-_]|^[A-Za-z0-9_]{60,}$", re.I)


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update(path, **sections):
    """Replace the given top-level sections of config.json, keep the rest."""
    with _lock:
        data = load(path)
        data.update(sections)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return data


def role_id(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def check_roles(items):
    """Validate custom roles from the settings page -> clean list. Raises ValueError."""
    if not isinstance(items, list):
        raise ValueError("roles must be a list")
    out, seen = [], set()
    reserved = {r.id for r in BUILTIN.values()} | {r.name.lower() for r in BUILTIN.values()}
    for r in items:
        if not isinstance(r, dict):
            raise ValueError(f"Each role must be an object: {r!r}")
        name = str(r.get("name") or "").strip()
        rid = role_id(name)
        if not NAME_RE.match(name) or not rid:
            raise ValueError(f"Role name {name!r}: 1-40 letters, digits, spaces.")
        if rid in reserved or name.lower() in reserved:
            raise ValueError(f"{name!r} is a built-in role. Copy it and give the copy a new name.")
        if rid in seen:
            raise ValueError(f"Two roles are called {name!r}.")
        level = r.get("level") or "read"
        if level not in LEVELS:
            raise ValueError(f"Role {name}: unknown permission {level!r}.")
        job = str(r.get("job") or "").strip()
        if len(job) > 2000:
            raise ValueError(f"Role {name}: the job description is too long (2000 characters max).")
        seen.add(rid)
        out.append({"id": rid, "name": name, "job": job, "level": level})
    return out


def check_key_env(apis):
    for a in apis or []:
        k = str((a or {}).get("key_env") or "")
        if k and KEY_LIKE_RE.search(k):
            raise ValueError(f"API {a.get('id')}: key_env looks like an API key. Put the key in an "
                             "environment variable (e.g. OPENAI_API_KEY) and enter that NAME here.")
