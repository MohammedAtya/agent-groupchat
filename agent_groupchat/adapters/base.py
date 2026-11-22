"""The adapter interface: one adapter per AI agent.

The chat engine knows nothing about any particular CLI. It hands an adapter a
RunContext (folder, session, callbacks for live progress) plus the prompt and the
permission level, and gets back the reply text and its stats.
"""
import os
import shutil
import subprocess
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
    model: Optional[str] = None      # model for this agent, or None for the CLI's default
    sandbox: bool = False            # is the OS command sandbox (bubblewrap) working?
    state_dir: str = ""              # where an adapter may keep its own state (chat APIs: history)


TALK_TEXT = "TALK ONLY: discuss only; don't open, change or run anything."


class Adapter:
    id = ""            # e.g. "claude"; also `@claude` reaches every copy of this AI in a chat
    name = ""          # default display name, e.g. "Claude"
    kind = ""          # what it is, told to the other agents, e.g. "Anthropic's Claude Code"
    color = "#888888"  # accent color on the page
    intro_note = ""    # extra agent-specific sentence appended to this agent's intro
    # Permission levels (see roles.LEVELS) this agent can really ENFORCE, which of them need
    # the OS sandbox, and caveats shown on the page. A level it can't enforce is never given:
    # the agent drops to the next lower level it can enforce.
    levels = ("talk",)
    needs_sandbox = ()
    level_notes = {}
    # What this agent may do at each level, in its own words (it's told this).
    mode_texts = {"talk": TALK_TEXT}
    # How a /skill from the chat is invoked:
    #   "slash":  the prompt starts with /name args (Claude Code)
    #   "dollar": the message mentions $name (Codex)
    skill_syntax = "slash"

    def run(self, ctx: RunContext, prompt: str, level: str):
        """Run one turn at permission `level`. -> (reply text, meta dict: files, cost, tokens).
        Reply text starting with "(error)", "(stopped)" or "(timed out)" marks a failure."""
        raise NotImplementedError

    def skill_files(self, folder):
        """-> (SKILL.md paths, command .md paths) this agent can run from `folder`."""
        return [], []

    def mode_text(self, level):
        return self.mode_texts.get(level) or TALK_TEXT

    def available(self, level, sandbox):
        return level in self.levels and (sandbox or level not in self.needs_sandbox)

    def why_not(self, level, sandbox):
        """Why `level` can't be enforced right now ("" if it can)."""
        if level not in self.levels:
            return self.level_notes.get(level) or f"{self.name} can't enforce this level."
        if not sandbox and level in self.needs_sandbox:
            return "needs the command sandbox, which isn't working on this computer"
        return ""

    def resume_hint(self, session):
        """Terminal command that continues this session, e.g. 'claude -r <id>'."""
        return ""

    command = []  # the CLI, e.g. ["claude"] (tests point it at a fake)

    def installed(self):
        return bool(self.command) and shutil.which(self.command[0]) is not None

    def detect(self):
        """(ok, message) for the Test button. Default: the CLI runs `--version`. No tokens spent."""
        if not self.installed():
            return False, f"`{self.command[0] if self.command else self.id}` isn't installed (not on PATH)."
        from ..platform_support import resolve_command  # (Windows: no .cmd via cmd.exe)
        try:
            r = subprocess.run(resolve_command([*self.command, "--version"]), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=20,
                               stdin=subprocess.DEVNULL)
        except Exception as e:
            return False, f"`{self.command[0]} --version` failed: {e}"
        out = (r.stdout or r.stderr).strip().splitlines()
        return r.returncode == 0, (out[0] if out else f"exit code {r.returncode}")


class MissingAdapter(Adapter):
    """Stands in for an AI that a chat uses but that isn't configured anymore."""
    kind = "an AI that isn't configured anymore"

    def __init__(self, id):
        self.id, self.name = id, id

    def installed(self):
        return False

    def run(self, ctx, prompt, level):
        return (f"(error) This agent's AI ({self.id}) isn't configured anymore. Add it back in "
                "the settings, or remove the agent from this chat."), {}

    def detect(self):
        return False, f"{self.id} isn't configured."


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
