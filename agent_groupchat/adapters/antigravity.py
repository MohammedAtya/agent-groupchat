"""Gemini through Google Antigravity's CLI (`agy -p ... --output-format stream-json`),
checked against agy 1.5.18.

Google stopped serving the Gemini CLI to personal accounts ("This client is no longer
supported for Gemini Code Assist for individuals"); Antigravity is its replacement.

Permissions: a headless run can't ask for approval, and when a tool would need it agy
ends the whole turn with no reply. So every level starts from Default mode (not Auto) and
denies, with permission rules, each kind of tool it doesn't allow: a denied tool just
fails and the agent carries on. Subagents and run_workflow scripts obey the same rules.
  talk  deny reading, writing, commands, MCP tools and URL reads
  read  deny writing, commands and MCP tools
  edit  deny commands and MCP tools; edits inside the project folder are allowed
  work  agy's terminal sandbox (--sandbox) in accept-edits mode with the tool policy
        "proceed-in-sandbox": commands run without asking but only inside the sandbox, where
        the project folder is the only writable place (checked: writes to the home folder
        fail, and the file tools' writes outside the project are auto-denied); MCP denied.
        No allow rule: one would let commands run outside the sandbox.
        Only offered when that sandbox can start (Ubuntu's AppArmor user-namespace
        restriction without a profile granting agy `userns`, or user namespaces switched off,
        blocks it): then the level is limited, with the fix (README). If this check were
        wrong, agy falls back to asking before every command, which a headless turn
        auto-denies (verified on 1.5.18 with these settings): commands are refused, never run
        unsandboxed. (They would with an allow rule, so the allow and ask lists are always sent
        empty, also overriding rules saved in the user's agy settings.)
No auto: Antigravity's Auto mode isn't offered yet.
"""
import json
import os
import platform
import re
import shutil
import uuid

from . import base
from .base import Adapter, add_file, first_line, rel

DENY = {
    "talk": ["read_file(*)", "write_file(*)", "command(*)", "mcp(*)", "read_url(*)"],
    "read": ["write_file(*)", "command(*)", "mcp(*)"],
    "edit": ["command(*)", "mcp(*)"],
    "work": ["mcp(*)"],
}
EDIT_TOOLS = ("write_file", "edit_file", "replace_file_content", "multi_replace_file_content",
              "sed_file", "notebook_edit")
PATH_KEYS = ("Path", "AbsolutePath", "TargetFile", "FilePath", "DirectoryPath", "SearchPath")
NEEDS_APPROVAL = "headless mode cannot prompt"


def settings(level):
    """Per-run settings (never saved): Default mode plus the level's deny rules; at work,
    commands proceed only inside the terminal sandbox. The allow and ask lists are always
    sent EMPTY: --settings keeps the lists it isn't given from the user's saved settings, and
    a saved allow rule (e.g. "always allow" clicked in agy) lets matching commands run
    unsandboxed when the sandbox can't start (verified on 1.5.18; empty lists stop it)."""
    if level == "work":
        return json.dumps({"agentMode": "", "toolPermission": "proceed-in-sandbox",
                           "permissions": {"allow": [], "ask": [], "deny": DENY["work"]}})
    deny = DENY.get(level) or DENY["talk"]
    return json.dumps({"agentMode": "", "permissions": {"allow": [], "ask": [], "deny": deny}})


USERNS_SYSCTL = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
USERNS_CLONE = "/proc/sys/kernel/unprivileged_userns_clone"   # Debian-style switch
MAX_USERNS = "/proc/sys/user/max_user_namespaces"
APPARMOR_DIR = "/etc/apparmor.d"


PROFILE_RE = re.compile(r"^\s*profile\s+\S+\s+(\S+)[^{]*\{(.*?)^\s*\}", re.M | re.S)
USERNS_RE = re.compile(r"^\s*(?:allow\s+)?userns\s*,", re.M)
SKIPPED_SUFFIXES = ("~", ".bak", ".orig", ".rej", ".swp", ".dpkg-new", ".dpkg-old", ".dpkg-dist",
                    ".dpkg-bak", ".dpkg-remove", ".pacsave", ".pacnew", ".rpmnew", ".rpmsave", ".ucf-dist",
                    ".ucf-new", ".ucf-old")


def _apparmor_grants_userns(paths):
    """Does an AppArmor profile attach to exactly one of `paths` and allow `userns`?
    (Comments don't count, nor `deny userns`, nor a profile for another path.)"""
    try:
        names = os.listdir(APPARMOR_DIR)
    except OSError:
        return False
    for n in names:
        f = os.path.join(APPARMOR_DIR, n)
        if (not os.path.isfile(f) or n.startswith(".") or n.endswith(SKIPPED_SUFFIXES)
                or os.path.exists(os.path.join(APPARMOR_DIR, "disable", n))):
            continue  # (apparmor_parser skips backups and disabled profiles too)
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read(200_000)
        except OSError:
            continue
        text = re.sub(r"#.*", "", text)
        for attach, body in PROFILE_RE.findall(text):
            if attach.strip('"') in paths and USERNS_RE.search(body):
                return True
    return False


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def sandbox_problem(command="agy"):
    """'' if agy's terminal sandbox can start on this computer, else why not (with the fix).
    agy runs commands unsandboxed when it can't start it, so "work" needs this to be ''."""
    system = platform.system()
    if system == "Darwin":
        return ""  # macOS: Seatbelt
    if system != "Linux":
        return "Antigravity's terminal sandbox isn't available on this system"
    if _read(USERNS_CLONE) == "0" or _read(MAX_USERNS) == "0":
        return ("this Linux doesn't allow unprivileged user namespaces, which Antigravity's terminal "
                "sandbox needs (kernel.unprivileged_userns_clone / user.max_user_namespaces)")
    if _read(USERNS_SYSCTL) != "1":
        return ""
    exe = shutil.which(command)
    paths = {p for p in (exe, exe and os.path.realpath(exe)) if p}
    if paths and _apparmor_grants_userns(paths):
        return ""
    return ("Ubuntu's AppArmor blocks Antigravity's terminal sandbox, so it can't run commands "
            "safely: add the one-time AppArmor profile for agy (README → Enable Antigravity's "
            "sandbox)")


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
    levels = ("talk", "read", "edit", "work")
    level_notes = {
        "auto": "Antigravity's Auto mode isn't offered here yet: use Edit + run commands "
                "(sandboxed).",
    }
    mode_texts = {
        "talk": "TALK ONLY: file, command and web-page tools are switched off this turn; "
                "discuss only.",
        "read": "READ: you may read and search files, but don't modify anything; commands are "
                "switched off.",
        "edit": "EDIT: you may edit files in the project folder; commands are switched off.",
        "work": "WORK: you may edit files and run commands; commands run in your terminal "
                "sandbox, where only the project folder can be changed.",
    }

    def available(self, level, sandbox):
        if level == "work":  # needs agy's own terminal sandbox (not bubblewrap)
            return not sandbox_problem(self.command[0])
        return level in self.levels

    def why_not(self, level, sandbox):
        if level == "work":
            return sandbox_problem(self.command[0])
        return super().why_not(level, True)

    def run(self, ctx, prompt, level):
        sid = ctx.session
        text = prompt if sid else ctx.system_prompt + "\n\n" + prompt  # (intro: new sessions)
        cmd = [*self.command, "--output-format", "stream-json", "--settings", settings(level)]
        if level == "work":  # commands only inside the terminal sandbox; edits in the project
            cmd += ["--sandbox", "--mode", "accept-edits"]
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
