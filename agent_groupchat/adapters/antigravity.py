"""Gemini through Google Antigravity's CLI (`agy -p ... --output-format stream-json`),
checked against agy 1.5.

Google stopped serving the Gemini CLI to personal accounts ("This client is no longer
supported for Gemini Code Assist for individuals"); Antigravity is its replacement.

Permissions: a headless run can't ask for approval, and when a tool would need it agy
ends the whole turn with no reply. So every level starts from Default mode (not Auto) and
denies, with permission rules, each kind of tool it doesn't allow: a denied tool just
fails and the agent carries on. Subagents and run_workflow scripts obey the same rules.
  talk  deny reading, writing, commands, MCP tools and URL reads
  read  deny writing, commands and MCP tools
  edit  deny commands and MCP tools; edits inside the project folder are allowed
No work/auto: agy only runs commands safely inside its own terminal sandbox (on Ubuntu it
is blocked by the AppArmor user-namespace restriction), and outside it every command needs
an approval that a group-chat turn can't give.
"""
import json
import uuid

from . import base
from .base import Adapter, add_file, first_line, rel

DENY = {
    "talk": ["read_file(*)", "write_file(*)", "command(*)", "mcp(*)", "read_url(*)"],
    "read": ["write_file(*)", "command(*)", "mcp(*)"],
    "edit": ["command(*)", "mcp(*)"],
}
EDIT_TOOLS = ("write_file", "edit_file", "replace_file_content", "multi_replace_file_content",
              "sed_file", "notebook_edit")
PATH_KEYS = ("Path", "AbsolutePath", "TargetFile", "FilePath", "DirectoryPath", "SearchPath")
NEEDS_APPROVAL = "headless mode cannot prompt"


def settings(level):
    """Per-run settings (never saved): Default mode plus the level's deny rules."""
    deny = DENY.get(level) or DENY["talk"]
    return json.dumps({"agentMode": "", "permissions": {"deny": deny}})


def param(args, *keys):
    for k in keys:
        if args.get(k):
            return str(args[k])
    return ""


def describe_tool(name, args, folder):
    path = rel(param(args, *PATH_KEYS), folder)
    if name in ("read_file", "view_file"):
        return f"📖 reading {path}"
    if name in EDIT_TOOLS:
        return f"✏️ editing {path}"
    if name in ("run_command", "run_workflow"):
        return "$ " + (param(args, "CommandLine", "Description") or name).split("\n")[0]
    if name in ("grep_search", "find_by_name"):
        return f"🔎 searching for {param(args, 'Query', 'Pattern', 'query', 'pattern')}"
    if name == "list_dir":
        return f"📂 listing {path or '.'}"
    if name == "search_web":
        return f"🌐 searching the web: {param(args, 'query', 'Query')}"
    if name == "read_url_content":
        return f"🌐 reading {param(args, 'Url', 'url')}"
    if name in ("invoke_subagent", "browser_subagent"):
        return "🤖 subagent"
    return f"🔧 {name}"


class AntigravityAdapter(Adapter):
    id = "antigravity"
    name = "Antigravity"
    kind = "Google's Gemini, through the Antigravity CLI"
    color = "#4285f4"
    skill_syntax = "none"
    command = ["agy"]
    levels = ("talk", "read", "edit")
    level_notes = {
        "work": "Antigravity can't run commands here: a group-chat turn can't approve them, "
                "and its terminal sandbox isn't offered.",
        "auto": "Antigravity can't run commands here: a group-chat turn can't approve them, "
                "and its terminal sandbox isn't offered.",
    }
    mode_texts = {
        "talk": "TALK ONLY: file, command and web-page tools are switched off this turn; "
                "discuss only.",
        "read": "READ: you may read and search files, but don't modify anything; commands are "
                "switched off.",
        "edit": "EDIT: you may edit files in the project folder; commands are switched off.",
    }

    def run(self, ctx, prompt, level):
        sid = ctx.session
        text = prompt if sid else ctx.system_prompt + "\n\n" + prompt  # (intro: new sessions)
        cmd = [*self.command, "--output-format", "stream-json", "--settings", settings(level)]
        if sid:
            cmd += ["--conversation", sid]
        if ctx.model:
            cmd += ["--model", ctx.model]
        stdin = None
        if len(text) > base.ARGV_LIMIT:  # too long for the command line: one NDJSON message
            stdin = json.dumps({"event": "user", "message": {"role": "user", "content": text}}) + "\n"
            cmd += ["--input-format", "stream-json", "--print="]
        else:
            cmd += [f"--print={text}"]  # attached, so a prompt starting with "-" isn't a flag
        out = {"files": [], "result": None, "steps": {}, "last": None, "started": False}

        def on_event(ev):
            t = ev.get("event")
            if t == "init":
                out["started"] = True
                if ev.get("conversation_id"):
                    ctx.save_session(ev["conversation_id"])
            elif t == "step_update":
                s = ev.get("step_update") or {}
                kind, idx = s.get("step_type"), s.get("step_index")
                if kind == "agent_response" and s.get("text_delta"):
                    if idx != out["last"]:  # a new message: the preview shows the latest one
                        out["last"], out["steps"][idx] = idx, ""
                        ctx.preview("")
                    out["steps"][idx] += s["text_delta"]
                    ctx.preview(s["text_delta"], append=True)
                elif kind == "tool":
                    info = s.get("tool_info") or {}
                    name, args = s.get("tool_name") or info.get("name") or "", info.get("parameters") or {}
                    if s.get("state") == "ACTIVE":
                        ctx.step(describe_tool(name, args, ctx.folder))
                    elif s.get("state") == "DONE" and name in EDIT_TOOLS:
                        add_file(out, rel(param(args, *PATH_KEYS), ctx.folder))
                elif kind == "subagent" and s.get("state") == "ACTIVE":
                    ctx.step("🤖 subagent")
            elif t == "result":
                out["result"] = ev.get("result") or {}

        code, err, why = ctx.stream(cmd, on_event, stdin=stdin)
        res = out["result"] or {}
        u = res.get("usage") or {}
        meta = {"files": out["files"], "cost": None,
                "tokens": [u.get("input_tokens", 0) + u.get("cache_read_tokens", 0),
                           u.get("output_tokens", 0)] if u else None}
        if why:
            return f"({why})", meta
        # The last message is the answer (earlier ones are "let me check…" while it works).
        reply = (out["steps"].get(out["last"]) or "").strip() or (res.get("response") or "").strip()
        if res.get("status") == "SUCCESS" and reply and not code:
            ctx.step(first_line(reply))
            return reply, meta
        msg = res.get("error") or err or (f"exit code {code}" if code else "Antigravity sent no reply")
        if NEEDS_APPROVAL in msg:
            msg = ("Antigravity tried something that needs your approval, which a group-chat turn "
                   "can't ask for, so Antigravity stopped the turn. " + msg)
        return f"(error) {msg[-800:]}", meta

    def resume_hint(self, session):
        return f"agy --conversation {session}"
