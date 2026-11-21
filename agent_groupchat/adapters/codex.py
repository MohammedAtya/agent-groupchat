"""OpenAI Codex CLI (`codex exec --json`)."""
import os
import re
import tempfile

from ..skills import HOME, ancestors, scan_skill_dirs
from .base import Adapter, add_file, error_text, first_line, rel


def config(ability):
    """ability -> Codex `-c` settings (sandbox + approvals)."""
    sandbox = "workspace-write" if ability in ("work", "auto") else "read-only"
    conf = ["-c", f'sandbox_mode="{sandbox}"']
    if ability == "auto":
        # Codex's "approve for me": when the sandbox blocks something (internet, a
        # folder outside the project...), Codex asks for escalation and its
        # automatic reviewer (guardian) approves or denies it, like Claude's auto mode.
        return conf + ["-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="auto_review"']
    return conf + ["-c", 'approval_policy="never"']


class CodexAdapter(Adapter):
    id = "codex"
    name = "Codex"
    description = "Codex (OpenAI's coding agent)"
    intro_note = (" Note: in your workspace-write sandbox, empty read-only .git/.codex/.agents/.aws "
                  "folders can appear in the project; they are sandbox mount points, not real "
                  "files, so don't report them as project contents.")
    skill_syntax = "dollar"
    command = ["codex"]

    def run(self, ctx, prompt, ability):
        sid = ctx.session
        conf = config(ability)
        out = {"files": [], "usage": None}

        def on_event(ev):
            t, it = ev.get("type"), ev.get("item") or {}
            kind = it.get("type")
            if t == "thread.started" and not sid:  # save right away so Stop keeps it resumable
                ctx.save_session(ev.get("thread_id"))
            elif t == "turn.completed":
                out["usage"] = ev.get("usage")
            elif t == "item.started" and kind == "command_execution":
                cmd = it.get("command") or ""
                m = re.fullmatch(r"/bin/(?:ba|z)?sh -lc (['\"])(.*)\1", cmd, re.S)
                ctx.step("$ " + (m.group(2) if m else cmd).split("\n")[0])
            elif t == "item.started" and kind == "reasoning":
                ctx.step("💭 thinking")
            elif t == "item.started" and kind == "web_search":
                ctx.step(f"🌐 searching the web: {it.get('query', '')}")
            elif t == "item.started" and kind == "mcp_tool_call":
                ctx.step(f"🔧 {it.get('server', '')}.{it.get('tool', '')}")
            elif t == "item.completed" and kind == "file_change":
                for c in it.get("changes") or []:
                    p = rel(c.get("path"), ctx.folder)
                    add_file(out, p)
                    ctx.step(f"✏️ editing {p}")
            elif t == "item.completed" and kind == "agent_message" and \
                    (it.get("text") or "").strip():
                ctx.step(first_line(it["text"]))
                ctx.preview(it["text"])

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            outfile = tf.name
        try:
            base = [*self.command, "exec", "--skip-git-repo-check", "--json", *conf]
            if sid:
                cmd = [*base, "-o", outfile, "resume", sid, prompt]
            else:  # a new session gets the group-chat intro at the top of its first prompt
                cmd = [*base, "-C", ctx.folder, "-o", outfile,
                       ctx.system_prompt + "\n\n" + prompt]
            code, err, why = ctx.stream(cmd, on_event)
            with open(outfile) as f:
                text = f.read().strip()
        finally:
            os.unlink(outfile)
        u = out["usage"] or {}
        meta = {"files": out["files"], "cost": None,
                "tokens": [u.get("input_tokens", 0), u.get("output_tokens", 0)] if u else None}
        if why:
            return f"({why})", meta
        return (text or error_text(None, err, code)), meta

    def skill_files(self, folder):
        roots = [os.path.join(HOME, ".codex", "skills"),
                 os.path.join(HOME, ".codex", "skills", ".system"),
                 os.path.join(HOME, ".agents", "skills")]
        roots += [os.path.join(d, ".agents", "skills") for d in ancestors(folder) if d != HOME]
        return scan_skill_dirs(roots), []
