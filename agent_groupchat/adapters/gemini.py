"""Gemini CLI (`gemini -p ... --output-format stream-json`), checked against v0.70.

Levels map to Gemini's own approval modes and policy engine (tools that would need a
confirmation are denied in headless mode, so nothing hangs):
  talk  a policy that denies every tool
  read  --approval-mode default: read/search tools only, no edits, no shell
  edit  --approval-mode auto_edit: edits inside the workspace, still no shell
  work  --approval-mode yolo inside Gemini's own sandbox (-s: Docker/Podman, or
        Seatbelt on macOS). Without one of those, work isn't offered: Gemini never
        runs commands unsandboxed here.
No auto: Gemini has no safety reviewer like Claude's auto mode or Codex's guardian.
"""
import os
import platform
import shutil
import uuid

from .base import Adapter, add_file, first_line, rel

TALK_POLICY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_talk_policy.toml")
EDIT_TOOLS = ("write_file", "replace")


def sandbox_runtime():
    """The container/sandbox Gemini's -s would use, or None."""
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    return next((r for r in ("docker", "podman") if shutil.which(r)), None)


def permissions(level):
    if level == "work":
        return ["--approval-mode", "yolo", "--sandbox"]
    if level == "edit":
        return ["--approval-mode", "auto_edit"]
    if level == "read":
        return ["--approval-mode", "default"]
    return ["--approval-mode", "default", "--policy", TALK_POLICY]  # talk: every tool denied


def describe_tool(name, args, folder):
    path = rel(args.get("file_path") or args.get("dir_path") or args.get("path"), folder)
    if name == "read_file" or name == "read_many_files":
        return f"📖 reading {path}"
    if name in EDIT_TOOLS:
        return f"✏️ editing {path}"
    if name == "run_shell_command":
        return "$ " + (args.get("description") or (args.get("command") or "").split("\n")[0])
    if name in ("grep_search", "glob", "search_file_content"):
        return f"🔎 searching for {args.get('pattern', '')}"
    if name == "google_web_search":
        return f"🌐 searching the web: {args.get('query', '')}"
    if name == "web_fetch":
        return "🌐 reading a web page"
    if name == "list_directory":
        return f"📂 listing {path or '.'}"
    return f"🔧 {name}"


class GeminiAdapter(Adapter):
    id = "gemini"
    name = "Gemini"
    kind = "Google's Gemini CLI"
    color = "#4285f4"
    skill_syntax = "none"  # headless Gemini doesn't load skill slash-commands
    command = ["gemini"]
    levels = ("talk", "read", "edit", "work")
    level_notes = {"auto": "Gemini CLI has no auto mode with a safety reviewer."}
    mode_texts = {
        "talk": "TALK ONLY: your tools are switched off this turn; discuss only.",
        "read": "READ: you may read and search files, but don't modify anything and don't run "
                "commands.",
        "edit": "EDIT: you may edit files in the project folder but cannot run shell commands.",
        "work": "WORK: you may edit files and run commands; commands run in your sandbox.",
    }

    def available(self, level, sandbox):
        if level == "work":  # needs Gemini's own sandbox, not bubblewrap
            return sandbox_runtime() is not None
        return level in self.levels

    def why_not(self, level, sandbox):
        if level == "work" and level in self.levels and not sandbox_runtime():
            return ("needs Docker or Podman (Gemini's command sandbox), and neither is "
                    "installed")
        return super().why_not(level, True)

    def run(self, ctx, prompt, level):
        sid = ctx.session
        if sid:
            text, session_flags = prompt, ["--resume", sid]
        else:  # a new session gets the group-chat intro at the top of its first prompt
            sid = str(uuid.uuid4())
            text, session_flags = ctx.system_prompt + "\n\n" + prompt, ["--session-id", sid]
        cmd = [*self.command, "--output-format", "stream-json", "--skip-trust",
               *session_flags, *permissions(level)]
        if ctx.model:
            cmd += ["--model", ctx.model]
        cmd += ["--prompt", text]
        out = {"files": [], "text": "", "result": None, "error": None, "started": False}

        def on_event(ev):
            t = ev.get("type")
            if t == "init":
                out["started"] = True
                ctx.save_session(ev.get("session_id") or sid)
            elif t == "message" and ev.get("role") == "assistant":
                if not out["text"]:
                    ctx.preview("")
                out["text"] += ev.get("content") or ""
                ctx.preview(ev.get("content") or "", append=True)
            elif t == "tool_use":
                args = ev.get("parameters") or {}
                ctx.step(describe_tool(ev.get("tool_name", ""), args, ctx.folder))
                if ev.get("tool_name") in EDIT_TOOLS:
                    add_file(out, rel(args.get("file_path"), ctx.folder))
            elif t == "error" and ev.get("severity") == "error":
                ctx.step("⚠️ " + (ev.get("message") or "error"))
            elif t == "result":
                out["result"] = ev
                if ev.get("status") == "error":
                    out["error"] = (ev.get("error") or {}).get("message") or "Gemini reported an error"

        code, err, why = ctx.stream(cmd, on_event)
        stats = (out["result"] or {}).get("stats") or {}
        meta = {"files": out["files"], "cost": None,
                "tokens": [stats.get("input_tokens", 0), stats.get("output_tokens", 0)]
                if stats else None}
        if why:
            return f"({why})", meta
        reply = out["text"].strip()
        if out["error"] or code or not reply:
            msg = out["error"] or err or (f"exit code {code}" if code else "Gemini sent no reply")
            if code == 41:
                msg = "Gemini CLI isn't logged in (run `gemini` once in a terminal). " + msg
            return f"(error) {msg[-800:]}", meta
        if reply:
            ctx.step(first_line(reply))
        return reply, meta

    def resume_hint(self, session):
        return f"gemini --resume {session}"
