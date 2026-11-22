# agent-groupchat: you + your AI agents

> 🚧 This is being turned into **agent-groupchat**: any AIs you have, several copies of each, and roles
> with real permissions. See [SPEC.md](SPEC.md). Agents can be Claude Code, Codex, Gemini CLI, Aider,
> or any OpenAI-compatible chat API (OpenAI, Ollama, OpenRouter, Groq, LM Studio…).

Start it with:  `python3 -m agent_groupchat`  (from this folder)  →  http://127.0.0.1:8765  (change the port with `GROUPCHAT_PORT`)

- **Sidebar**: all your chats. Click one to reopen it; it reopens the last chat automatically.
- **New chat**: pick a project folder and the **agents** (AI, name, model, roles, permission), or start
  from a saved **Team**. **💾 Save as team** keeps a setup for next time (also from a chat's Team dialog).
- **⚙ Settings**: *Agents* (which CLIs are installed, with a **Test** button; add chat APIs), *Roles*
  (copy a built-in role or write your own: name, job, default permission), *Teams* (rename, delete).
- **Old chats**: if you used the earlier `~/groupchat` version, the page offers to import copies of its chats.
- **Agents**: any number, including several copies of the same AI ("Claude 1", "Claude 2"), each its own
  session. Give them names and, optionally, a model each (e.g. Opus for one copy, Sonnet for another).
- **@name** anywhere in a message asks only that agent: `@claude-2`, `@claude2`, `@codex`.
  `@claude` reaches every Claude copy.
- **Discussion**: after they all answer you, they can take turns replying to each other for N rounds
  (each one stays quiet if it has nothing to add).
- **Hand-offs**: if an agent addresses another one in its reply (`@codex …`, `Claude 2, can you …`),
  that one gets it when your message was for only some of them. If you wrote to everyone, only
  Discussion rounds decide whether they reply to each other.
- **Live progress + Stop**: while they work you see what each one is doing (`✏️ editing server.py`,
  `$ pytest`) and for how long. **■ Stop** kills the running replies and the rest of that turn;
  the sessions stay resumable.
- **Roles** (header → **Team**, or when creating the chat): Planner, Critic, Lead, Tester, Reviewer,
  Participant (no special job), plus your own roles in `~/.agent-groupchat/config.json`. A plain
  message runs them in order: **Planner + Critic → ⏸️ your OK → Lead → Tester → Reviewer**. Roles
  nobody holds are skipped; agents without a role answer directly, as before. Reply while the plan
  waits to change it, or press **▶ Go ahead**.
- **Fix rounds**: if the Tester or Reviewer ends with `VERDICT: FIX`, the Lead fixes it automatically
  (0–3 rounds, default 1), then it comes back to you.
- **Reply stats**: under each reply, how long it took, cost (≈$, Claude), tokens, and files changed.
  **▸ Diff** shows exactly what that reply changed (also edits made with shell commands).
- **Live preview**: the reply appears while it's being written (Claude word by word, Codex per message).
- **Status + Retry**: under your messages you see who got it and whether it's queued, working,
  done or failed. A failed / stopped reply has **↻ Retry**; **↩ Reply** writes to that AI only.
- **Read-only roles** (Planner, Critic, Reviewer) can read files and run read-only checks (tests,
  `git diff`) but get no file-editing tools; if one changes files anyway, its reply is flagged ⚠.
  So is a Tester that changes non-test files.
- **Compact**: the first agent summarizes the chat, then all continue in fresh sessions from the summary,
  so long chats get cheaper again. The old sessions stay resumable from the terminal.
- **Markdown**: replies render headings, lists, tables, links and code blocks with a Copy button.
- **Export**: downloads the chat as Markdown.
- **Security**: only this page can drive the server. Requests need the right `Host`, the
  `Origin` if one is sent, and a token that is new on each launch, so other websites can't start chats.
- **Real sessions**: every agent is a real Claude Code or Codex session in the
  chat's folder, so skills, hooks, CLAUDE.md / AGENTS.md, memory and MCP servers load
  as usual. The bottom of each chat shows `claude -r <id>` / `codex resume <id>` to continue it
  in a terminal (run them from the chat's folder).
- Chats are saved in `~/.agent-groupchat/chats/` (override with `AGENT_GROUPCHAT_HOME`). Deleting a chat on the page doesn't delete the
  agents' sessions themselves.

## Adding agents
Claude Code, Codex, Gemini CLI and Aider are used if they're installed (New chat marks the ones
that aren't). **Aider needs a model name** (e.g. `sonnet`, `gpt-4.1`, `ollama_chat/llama3.1`); it
runs with every prompt answered automatically, so shell commands, git commits, `.gitignore` edits
and self-updates are switched off, and its history files live in `~/.agent-groupchat/sessions/`.

**Chat APIs** (talk only: they see the chat, not your files) are added under **⚙ Settings → Agents**,
or by hand in `~/.agent-groupchat/config.json`:
```json
{"apis": [
  {"id": "llama", "name": "Llama", "base_url": "http://localhost:11434/v1", "model": "llama3.1"},
  {"id": "gpt", "name": "GPT", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1",
   "key_env": "OPENAI_API_KEY"}
]}
```
`key_env` is the **name** of the environment variable that holds the key; the key itself is never
stored (Settings refuses a value that looks like a key).

## Roles and permissions
Each role has a default permission; under **Team** you can raise or lower it per agent (⚠ = raised).
An agent with two roles gets the higher default. **🔒 Lock** drops every agent to *Talk only* at once.
The same agent can't be Lead and Reviewer (two copies of one AI can).

| Permission | Claude Code | Codex | Gemini CLI | Aider | Chat API |
|---|---|---|---|---|---|
| Talk only | no tools at all | read-only sandbox, told to only discuss | every tool denied | ask mode, told to only discuss | ✓ (no tools) |
| Read | read/search tools, skills, `git diff/log/status`, tests (in the sandbox) | read-only sandbox | read/search tools | ask mode | – |
| Edit files | edit tools in the folder, **no** commands | – (no such setting) | `auto_edit`: edits, no shell | code mode, no shell, no commits | – |
| Edit + run commands | edits + commands, sandboxed to the folder | `workspace-write` sandbox | `yolo` inside Gemini's sandbox (needs Docker/Podman; Seatbelt on macOS) | – (no sandbox) | – |
| Auto | auto mode, risky actions blocked by the classifier | "approve for me" + automatic reviewer | – | – | – |

Defaults: Planner, Critic, Reviewer → Read; Lead, Tester, Participant → Edit + run commands.
A permission an AI can't enforce is greyed out. Without a working sandbox, "Edit + run commands"
drops to "Edit files" (Claude) or "Read" (Codex), and the agent's chip says *(limited)*.

## One-time fix: enable the sandbox (Ubuntu 23.10+ / 24.04)
Both CLIs confine commands with `bubblewrap`, but Ubuntu's AppArmor blocks it
(`bwrap: setting up uid map: Permission denied`). Allow it with:

```bash
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/bwrap
bwrap --ro-bind / / true && echo "sandbox OK"
```

This lets `bwrap` (and only `bwrap`) create user namespaces again. It's the setup Ubuntu
documents for sandboxing tools. The page notices within about a minute.

## Files
`agent_groupchat/server.py` (chat engine + web server), `agent_groupchat/adapters/` (one file per AI:
`claude.py`, `codex.py`), `agent_groupchat/static/index.html` (page). Your data is in `~/.agent-groupchat/`: `chats/` and `logs/server.log`
(the log is used when the server runs in the background). Stop it with Ctrl+C in its terminal.

## Tests
```
python3 -m unittest discover -s tests -t .
```
They use fake `claude` / `codex` programs (`tests/bin/`), so they run in seconds and cost nothing.
