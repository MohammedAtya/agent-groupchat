"""Claude Code (`claude -p`, stream-json output)."""
import json
import os
import uuid

from ..skills import HOME, ancestors, scan_skill_dirs
from .base import TALK_TEXT, Adapter, add_file, error_text, first_line, rel

READ_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Skill", "TodoWrite"]
EDIT_TOOLS = READ_TOOLS + ["Edit", "Write", "NotebookEdit"]
WORK_TOOLS = EDIT_TOOLS + ["Bash"]
EDIT_NAMES = ("Edit", "MultiEdit", "Write", "NotebookEdit")
SANDBOX = json.dumps({"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True}})

# ability -> permission flags
PERMISSIONS = {
    "auto": ["--permission-mode", "auto"],
    "auto-limited": ["--permission-mode", "auto"],
    "work": ["--permission-mode", "acceptEdits", "--settings", SANDBOX,
             "--allowedTools", *WORK_TOOLS],
    # reviewer: auto mode minus the file-editing tools
    "review-auto": ["--permission-mode", "auto", "--disallowedTools", *EDIT_NAMES],
    # reviewer: sandboxed commands (tests, git diff), no edit tools
    "review-work": ["--permission-mode", "dontAsk", "--settings", SANDBOX,
                    "--allowedTools", *READ_TOOLS, "Bash", "--disallowedTools", *EDIT_NAMES],
    # acceptEdits auto-approves edits only inside the working folder; anything else
    # would need a prompt, which -p can't show, so it is denied.
    "edit-only": ["--permission-mode", "acceptEdits", "--allowedTools", *READ_TOOLS,
                  "--disallowedTools", "Bash"],
    "talk": ["--permission-mode", "dontAsk", "--allowedTools", *READ_TOOLS],
}


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


class ClaudeAdapter(Adapter):
    id = "claude"
    name = "Claude"
    kind = "Anthropic's Claude Code"
    color = "#c96442"
    skill_syntax = "slash"
    command = ["claude"]
    mode_texts = {
        "talk": TALK_TEXT,
        "work": "WORK mode: you may edit files and run commands inside the project folder "
                "(sandboxed).",
        "edit-only": "LIMITED WORK mode (sandbox unavailable): you may edit files in the project "
                     "folder but cannot run shell commands.",
        "auto": "AUTO mode: you run in Claude Code auto mode: you may run commands and edit "
                "files where the user asks; a safety classifier blocks risky actions.",
    }
    mode_texts["auto-limited"] = mode_texts["auto"]

    def run(self, ctx, prompt, ability):
        sid = ctx.session
        cmd = [*self.command, "-p", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--append-system-prompt", ctx.system_prompt]
        if sid:
            cmd += ["--resume", sid]
        else:
            sid = str(uuid.uuid4())
            cmd += ["--session-id", sid]
        cmd += PERMISSIONS.get(ability, PERMISSIONS["talk"])
        if ctx.model:
            cmd += ["--model", ctx.model]
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
                    ctx.preview("")
                elif e.get("type") == "content_block_delta" and \
                        (e.get("delta") or {}).get("type") == "text_delta":
                    ctx.preview(e["delta"].get("text", ""), append=True)
            elif t == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        inp = c.get("input") or {}
                        ctx.step(describe_tool(c.get("name", ""), inp, ctx.folder))
                        if c.get("name") in EDIT_NAMES:
                            add_file(out, rel(inp.get("file_path") or inp.get("notebook_path"),
                                              ctx.folder))
                    elif c.get("type") == "text" and (c.get("text") or "").strip():
                        ctx.step(first_line(c["text"]))
            elif t == "result":
                out["result"] = ev

        code, err, why = ctx.stream(cmd, on_event)
        if out["started"]:  # the session exists now (even if stopped), so keep resuming it
            ctx.save_session(sid)
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
        return error_text(text, err, code), meta

    def resume_hint(self, session):
        return f"claude -r {session}"

    def skill_files(self, folder):
        roots = [os.path.join(HOME, ".claude", "skills")]
        roots += [os.path.join(d, ".claude", "skills") for d in ancestors(folder) if d != HOME]
        files = scan_skill_dirs(roots)
        try:
            with open(os.path.join(HOME, ".claude", "plugins", "installed_plugins.json")) as f:
                plugins = json.load(f).get("plugins", {})
            for entries in plugins.values():
                for e in entries:
                    files += scan_skill_dirs([os.path.join(e["installPath"], "skills")],
                                             recursive=True)
        except Exception:
            pass
        cmds = []
        for d in [os.path.join(HOME, ".claude", "commands")] + \
                 [os.path.join(a, ".claude", "commands") for a in ancestors(folder) if a != HOME]:
            if os.path.isdir(d):
                cmds += [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".md")]
        return files, cmds
