# Group Chat: you + Claude Code + Codex

> 🚧 This is being turned into **agent-groupchat**: any AIs you have, several copies of each, and roles
> with real permissions. See [SPEC.md](SPEC.md). For now it works with Claude Code + Codex, as below.

Start it with:  `python3 -m agent_groupchat`  (from this folder)  →  http://127.0.0.1:8765  (change the port with `GROUPCHAT_PORT`)

- **Sidebar**: all your chats. Click one to reopen it; it reopens the last chat automatically.
- **New chat**: pick a name, a project folder, a mode and the number of discussion rounds.
- **@claude / @codex** at the start of a message asks only that one.
- **Discussion**: after both answer you, they can take turns replying to each other for N rounds
  (each one stays quiet if it has nothing to add).
- **Hand-offs**: if one AI addresses the other in its reply (`@claude …`, `Codex, can you …`),
  the other gets it when your message was for only one of them. If you wrote to both, only
  Discussion rounds decide whether they reply to each other.
- **Live progress + Stop**: while they work you see what each one is doing (`✏️ editing server.py`,
  `$ pytest`) and for how long. **■ Stop** kills the running replies and the rest of that turn;
  the sessions stay resumable.
- **Lead + Review** (header → Lead): your messages go to the lead, which does the work; then the
  other AI reviews it **read-only** and says what to fix (or LGTM). You decide what's next.
  `@claude` / `@codex` still asks either one directly.
- **Reply stats**: under each reply, how long it took, cost (≈$, Claude), tokens, and files changed.
  **▸ Diff** shows exactly what that reply changed (also edits made with shell commands).
- **Live preview**: the reply appears while it's being written (Claude word by word, Codex per message).
- **Status + Retry**: under your messages you see who got it and whether it's queued, working,
  done or failed. A failed / stopped reply has **↻ Retry**; **↩ Reply** writes to that AI only.
- **Lead + Review reviewer** can read files and run read-only checks (tests, `git diff`) but has no
  file-editing tools; if it changes files anyway, its reply is flagged ⚠.
- **Compact**: Claude summarizes the chat, then both continue in fresh sessions from the summary,
  so long chats get cheaper again. The old sessions stay resumable from the terminal.
- **Markdown**: replies render headings, lists, tables, links and code blocks with a Copy button.
- **Export**: downloads the chat as Markdown.
- **Security**: only this page can drive the server. Requests need the right `Host`, the
  `Origin` if one is sent, and a token that is new on each launch, so other websites can't start chats.
- **Real sessions**: every chat is a real Claude Code session plus a real Codex session in the
  chat's folder, so skills, hooks, CLAUDE.md / AGENTS.md, memory and MCP servers load
  as usual. The bottom of each chat shows `claude -r <id>` / `codex resume <id>` to continue it
  in a terminal (run them from the chat's folder).
- Chats are saved in `~/.agent-groupchat/chats/` (override with `AGENT_GROUPCHAT_HOME`). Deleting a chat on the page doesn't delete the
  Claude/Codex sessions themselves.

## Modes
| Mode | Claude | Codex |
|---|---|---|
| Talk only | read, search, web, skills | read-only sandbox |
| Can work  | edit files + run commands, sandboxed to the folder | `workspace-write` sandbox |
| Can work, without a sandbox | edit files in the folder, **no** commands | read-only |
| Auto | Claude Code auto mode: commands + edits anywhere, risky actions blocked by the classifier | "approve for me": sandboxed to the folder, and when blocked (internet, other folders) its automatic reviewer approves or denies |

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
