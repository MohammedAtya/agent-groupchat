"""The adapter interface: one adapter per AI agent.

The chat engine knows nothing about any particular CLI. It hands an adapter a
RunContext (folder, session, callbacks for live progress) plus the prompt and the
permission ability, and gets back the reply text and its stats.
"""
import os
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class RunContext:
    folder: str                      # the chat's project folder (the agent's working dir)
    session: Optional[str]           # session id to resume, or None to start a new session
    system_prompt: str               # the group-chat intro for this agent
    save_session: Callable[[str], None]          # remember the session id (call as soon as known)
    step: Callable[[str], None]                  # one line of live activity ("✏️ editing x.py")
    preview: Callable[..., None]                 # preview(text, append=False): live reply text
    stream: Callable[[list, Callable[[dict], None]], tuple]
    # stream(cmd, on_event) runs cmd in `folder`, calls on_event for every JSON line of its
    # stdout and returns (exit code, stderr tail, None | "stopped" | "timed out").
    # It owns the process, so the user's Stop button and the timeout work for every agent.


class Adapter:
    id = ""            # key used in chats and @mentions, e.g. "claude"
    name = ""          # display name, e.g. "Claude"
    description = ""   # how the OTHER agents are told about this one
    intro_note = ""    # extra agent-specific sentence appended to this agent's intro
    # How a /skill from the chat is invoked:
    #   "slash":  the prompt starts with /name args (Claude Code)
    #   "dollar": the message mentions $name (Codex)
    skill_syntax = "slash"

    def run(self, ctx: RunContext, prompt: str, ability: str):
        """Run one turn. -> (reply text, meta dict: files, cost, tokens).
        Reply text starting with "(error)", "(stopped)" or "(timed out)" marks a failure."""
        raise NotImplementedError

    def skill_files(self, folder):
        """-> (SKILL.md paths, command .md paths) this agent can run from `folder`."""
        return [], []


# ---------------------------------------------------------------- helpers for adapters
def rel(path, folder):
    """Path relative to the project folder, or unchanged if it's outside it."""
    if not path:
        return ""
    r = os.path.relpath(path, folder) if os.path.isabs(path) else path
    return path if r.startswith("..") else r


def add_file(out, path):
    if path and path not in out["files"]:
        out["files"].append(path)


def first_line(text):
    return "💬 " + next((l for l in text.strip().splitlines() if l.strip()), "")


def error_text(text, err, code):
    return f"(error) {(text or err or f'exit code {code}')[-800:]}"
