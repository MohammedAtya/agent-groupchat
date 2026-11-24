"""All available agent adapters, keyed by id: the built-in CLIs plus the chat APIs
configured in config.json ("apis"). ADAPTERS is updated in place by load_adapters(), so
modules that imported it see changes."""
import json
import os
import re

from .aider import AiderAdapter
from .antigravity import AntigravityAdapter
from .base import Adapter, MissingAdapter, RunContext
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter
from .openai_api import OpenAIChatAdapter

BUILTIN = [ClaudeAdapter(), CodexAdapter(), AntigravityAdapter(), GeminiAdapter(), AiderAdapter()]
ADAPTERS = {a.id: a for a in BUILTIN}
_loaded = {"file": None, "mtime": None}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
URL_RE = re.compile(r"^https?://[^\s]+$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,80}$")


def parse_apis(items):
    """Validate config.json "apis" -> [OpenAIChatAdapter]. Raises ValueError."""
    out, seen = [], {a.id for a in BUILTIN}
    for it in items or []:
        if not isinstance(it, dict):
            raise ValueError(f"Each API must be an object: {it!r}")
        aid = str(it.get("id") or "").strip().lower()
        name = str(it.get("name") or "").strip()
        url, model = str(it.get("base_url") or "").strip(), str(it.get("model") or "").strip()
        key_env = str(it.get("key_env") or "").strip()
        if not ID_RE.match(aid) or aid in seen:
            raise ValueError(f"API id {aid!r}: lowercase letters, digits and -, unique, "
                             "and not claude/codex/gemini/aider/antigravity.")
        if not name or len(name) > 30:
            raise ValueError(f"API {aid}: a name of 1-30 characters is needed.")
        if not URL_RE.match(url):
            raise ValueError(f"API {aid}: base_url must start with http:// or https://")
        if not model or len(model) > 100 or any(c.isspace() for c in model):
            raise ValueError(f"API {aid}: a model name is needed (no spaces).")
        if key_env and not ENV_RE.match(key_env):
            raise ValueError(f"API {aid}: key_env is the NAME of an environment variable, "
                             "like OPENAI_API_KEY (never the key itself).")
        seen.add(aid)
        out.append(OpenAIChatAdapter(aid, name, url, model, key_env))
    return out


def load_adapters(config_file=None, force=False):
    """(Re)build ADAPTERS from the built-ins and config.json. Cheap when nothing changed.
    A broken config keeps the built-ins and returns the error message."""
    try:
        mtime = os.path.getmtime(config_file) if config_file else None
    except OSError:
        mtime = None
    if not force and _loaded["file"] == config_file and _loaded["mtime"] == mtime:
        return ""
    _loaded.update(file=config_file, mtime=mtime)
    error, apis = "", []
    if mtime is not None:
        try:
            with open(config_file, encoding="utf-8") as f:
                apis = parse_apis(json.load(f).get("apis"))
        except (OSError, ValueError, AttributeError) as e:
            error = f"config.json: {e}"
    ADAPTERS.clear()
    ADAPTERS.update({a.id: a for a in BUILTIN + apis})
    return error


def get(aid):
    return ADAPTERS.get(aid) or MissingAdapter(aid)


__all__ = ["ADAPTERS", "Adapter", "RunContext", "load_adapters", "get", "parse_apis"]
