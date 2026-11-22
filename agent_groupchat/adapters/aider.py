"""Aider (https://aider.chat), one non-interactive run per turn.

Aider has no machine-readable output and no system-prompt flag, so:
- the reply is read back from its chat-history file, which lives in the app's data folder
  (one per session, restored on the next turn with --restore-chat-history);
- the group-chat intro goes at the top of a session's first message (like Codex).
Every prompt Aider could show is answered by --yes-always, so anything risky it would say
yes to is switched off explicitly: creating a git repo, auto-commits, editing .gitignore,
pip self-updates, browser pages, lint/test runs. Shell commands are never run (--yes-always
answers those "no", and suggesting them is off).
"""
import os
import re
import subprocess
import tempfile
import uuid

from .base import Adapter, add_file, first_line, rel

ASK_TEXT = ("READ (Aider ask mode): you may read the files shared with you, but you can't "
            "change anything.")

SAFE_FLAGS = ["--yes-always", "--no-pretty", "--no-stream", "--no-fancy-input",
              "--no-check-update", "--no-show-release-notes", "--no-analytics",
              "--no-show-model-warnings", "--no-detect-urls", "--disable-playwright",
              "--no-suggest-shell-commands", "--no-auto-lint", "--no-auto-test",
              "--no-gitignore", "--no-auto-commits", "--no-dirty-commits", "--timeout", "600"]
APPLIED_RE = re.compile(r"^Applied edit to (.+?)\s*$")


def in_git_repo(folder):
    try:
        return subprocess.run(["git", "-C", folder, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def new_reply(history_text):
    """The assistant's reply from the part of Aider's chat-history file written this turn:
    '#### ' lines are the user's message, '> ' lines are Aider's own output (edits applied,
    warnings), '# aider chat started' is a header; the rest is the reply."""
    keep = [l for l in history_text.splitlines()
            if not l.startswith(("#### ", "> ", "# aider chat started")) and l.strip() != "####"
            and l.strip() != ">"]
    return "\n".join(keep).strip()


class AiderAdapter(Adapter):
    id = "aider"
    name = "Aider"
    kind = "Aider, an AI pair-programming CLI"
    color = "#14b8a6"
    skill_syntax = "none"
    command = ["aider"]
    needs_model = True  # without one, Aider offers a browser login
    levels = ("talk", "read", "edit")
    level_notes = {
        "talk": "Aider can't be stopped from reading files: it stays in ask mode (no changes) "
                "and is told to only discuss.",
        "work": "Aider has no command sandbox, so it's never allowed to run commands.",
        "auto": "Aider has no auto mode with a safety reviewer.",
    }
    mode_texts = {
        "talk": "TALK ONLY (Aider ask mode): discuss only; don't ask to see or change files.",
        "read": ASK_TEXT,
        "edit": "EDIT (Aider code mode): you may edit files in the project folder; you can't run "
                "commands. Edits are not committed to git.",
    }

    def run(self, ctx, prompt, level):
        sid = ctx.session or str(uuid.uuid4())
        os.makedirs(ctx.state_dir, exist_ok=True)
        hist = os.path.join(ctx.state_dir, f"aider-{sid}.chat.md")
        inputs = os.path.join(ctx.state_dir, f"aider-{sid}.input")
        start = os.path.getsize(hist) if os.path.exists(hist) else 0
        text = prompt if ctx.session else ctx.system_prompt + "\n\n" + prompt
        if text.lstrip().startswith("/"):  # Aider would run it as one of its /commands
            text = "(message) " + text
        cmd = [*self.command, "--chat-mode", "code" if level == "edit" else "ask", *SAFE_FLAGS,
               "--chat-history-file", hist, "--input-history-file", inputs,
               *(["--restore-chat-history"] if ctx.session else []),
               *([] if in_git_repo(ctx.folder) else ["--no-git"])]
        if ctx.model:
            cmd += ["--model", ctx.model]
        out = {"files": [], "lines": []}

        def on_event(ev):  # Aider prints plain text; stream() hands over each line
            line = ev.get("text", "") if ev.get("type") == "_text" else ""
            if not line.strip():
                return
            out["lines"].append(line)
            m = APPLIED_RE.match(line.strip())
            if m:
                p = rel(os.path.join(ctx.folder, m.group(1)), ctx.folder)
                add_file(out, p)
                ctx.step(f"✏️ editing {p}")

        fd, msg = tempfile.mkstemp(suffix=".txt", prefix="gc-aider-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            ctx.step("💭 thinking")
            code, err, why = ctx.stream([*cmd, "--message-file", msg], on_event)
        finally:
            os.unlink(msg)
        meta = {"files": out["files"], "cost": None, "tokens": None}
        if why:
            return f"({why})", meta
        try:
            with open(hist, encoding="utf-8", errors="replace") as f:
                f.seek(start)
                reply = new_reply(f.read())
        except OSError:
            reply = ""
        if not reply:  # Aider exits 0 even when the model call failed: show its last words
            tail = "\n".join(out["lines"][-6:]) or err or f"exit code {code}"
            return f"(error) {tail[-800:]}", meta
        ctx.save_session(sid)
        ctx.preview(reply)
        ctx.step(first_line(reply))
        return reply, meta

    def resume_hint(self, session):
        return ("aider --restore-chat-history --chat-history-file "
                f"~/.agent-groupchat/sessions/aider-{session}.chat.md")
