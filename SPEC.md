# agent-groupchat: Spec v1

A local web app where **you and any number of AI agents** work together in one group chat, on one project folder.
Bring whatever AIs you have (Claude Code, Codex, Gemini CLI, Aider, or any OpenAI-compatible chat API), run
several copies of the same one, and give each a **role** (Planner, Lead, Tester, Reviewer…) with its own
**permissions**.

It grows out of the existing Claude + Codex group chat in `~/groupchat/`, which keeps working untouched while this is built.

Status: design confirmed on 2026-11-22. M0–M6 built (tests green on Linux, macOS and Windows); M7 (packaging + first public release) in progress.

---

## 1. Goals and non-goals

**Goals**
- A tool strangers can install with one command and run on **Linux, macOS and Windows**.
- Works with **any mix of AIs** the user has, including several copies of the same one.
- Roles and permissions are **really enforced** through each AI's own permission system, not just requested in the prompt.
- Safe by default: an agent never gets more power than the user can see on its badge.

**Not in v1** (planned for v2)
- Agents editing in parallel, each in **its own copy of the project** (git worktree per agent, e.g. `agent/codex-1`),
  followed by a side-by-side compare where you pick or merge. In v1, editing agents take turns.
- A desktop app or `.exe`.

---

## 2. The repo

| Item | Decision |
|---|---|
| Name | `agent-groupchat` |
| Visibility | Public, on the user's GitHub account |
| License | MIT |
| Source folder | Clean copy in `~/groupchat_project/`; `~/groupchat/` is not turned into the repo |
| Never committed | `chats/`, `server.log`, `__pycache__/`, the vendored `.agents/skills/` (link to the original repo in the README instead) |
| Language | Python 3.10+, **standard library only**, no `pip install` step for dependencies |
| Install | `pipx install agent-groupchat` or `uvx agent-groupchat`; developers: `git clone` + `python -m agent_groupchat` |

**Package layout (proposed)**
```
agent_groupchat/
  __main__.py        # starts the server, opens the browser (Python's webbrowser module, all 3 OSes)
  server.py          # HTTP + security (Host/Origin/token checks, carried over as-is)
  chat.py            # chats, messages, routing, @mentions, storage
  workflow.py        # role order, plan pause, fix rounds, edit lock
  permissions.py     # levels, role defaults, overrides, Talk-only lock
  roles.py           # built-in + custom roles
  config.py          # ~/.agent-groupchat/ config: agents, roles, teams
  diffs.py           # snapshots + per-reply diffs (carried over)
  adapters/
    base.py          # the Adapter interface
    claude.py  codex.py  gemini.py  aider.py  openai_api.py
  static/index.html
tests/
  fake_agent.py      # a pretend AI CLI used by all tests
```

---

## 3. Agents (the AIs)

### 3.1 Two kinds
- **Agent CLIs**: Claude Code, Codex, Gemini CLI, Aider (later more). They run in the chat's folder and can read, edit and run
  commands. They keep every current feature: modes, diffs, live progress, resumable sessions.
- **Chat APIs**: one adapter for any **OpenAI-compatible** endpoint (OpenAI, Ollama, OpenRouter, Groq, LM Studio…).
  Always **talk only**: no files, no tools.

### 3.2 Adapter interface
Every AI is an adapter with the same small interface:
- `detect()`: is it installed / reachable? (used for auto-detection and the **Test** button)
- `levels()`: which permission levels this agent can **enforce** (see §5.3)
- `run(session, prompt, level, folder, on_event)` → reply text, stats (time, cost, tokens, files changed), session id
- `resume_hint(session)`: e.g. `claude -r <id>`, shown at the bottom of the chat

Claude and Codex become the first two adapters and must behave **exactly as they do today** before anything else changes.

### 3.3 Copies of the same AI
- Add as many copies of any AI as you like. Each copy is a **separate session** with its own memory of the chat.
- Auto-named "Claude 1", "Claude 2"…, and renamable ("Claude Tester"). `@claude2` / `@claude-tester` addresses one copy.
- Each copy can use **its own model** (e.g. Opus as Lead, a cheaper model as Tester). Default: the CLI's own default.

### 3.4 Where agents are configured
- `~/.agent-groupchat/config.json`, **plus** a settings page that edits that same file.
- **Presets**: installed CLIs (`claude`, `codex`, `gemini`, `aider`) are found automatically on first launch; no setup needed.
- **API keys are never stored**: the config holds only the *name* of the environment variable that holds the key.

---

## 4. Roles

### 4.1 Built-in roles (can be copied, not edited)
| Role | Job | Default permission |
|---|---|---|
| **Planner** | Breaks the request into steps and asks questions before any work starts | read |
| **Lead** | Does the work | edit + commands |
| **Tester** | Writes and runs tests, reports what fails; must not change non-test code | edit + commands (non-test changes flagged ⚠) |
| **Reviewer** | Reads the Lead's changes, says what to fix or LGTM | read (+ read-only commands like tests, `git diff`) |
| **Critic** | Challenges the idea or approach, looks for what everyone missed | read |
| **Participant** | No special job (today's behavior) | edit + commands |

### 4.2 Custom roles
Name + job instructions + default permission. Made in the settings page (Roles tab) or the config file.

### 4.3 Assigning roles
- Roles are set **per chat**, per agent, when the chat is created, and can be changed during the chat.
- One agent can hold **several roles** (Claude 1 = Lead + Tester).
- Several agents can share **one role** (two Reviewers reply in parallel).
- **Only blocked combination:** the *same agent* as Lead and Reviewer of its own work.
  Two copies of the same AI (Claude 1 Lead, Claude 2 Reviewer) are fine, with no warning.
- No roles set = everyone is a Participant = today's behavior.

---

## 5. Permissions

### 5.1 Levels
`talk` → `read` → `edit` → `edit + commands` → `auto`

- **auto**: the agent decides on its own and risky actions are blocked (Claude Code auto mode / Codex "approve for me").
  Only for agents that support it; you must confirm before turning it on.

### 5.2 How an agent's permission is decided
1. Each role gives a **default** level.
2. If the agent holds several roles, it gets the **highest** of their defaults.
3. You can **raise or lower** it per agent. Raised above the role default → the agent's badge shows **⚠ raised**.
4. **🔒 Talk-only lock** (one button per chat): everyone drops to `talk` at once; unlocking restores each agent's own level.
5. The old chat-wide modes (Talk only / Can work / …) are **removed**; points 1–4 replace them.

### 5.3 Enforcement per agent: never quietly loosened
Every level maps to the agent's **real** permission flags. If an agent can't enforce a level, the settings page and
New chat screen **grey that combination out and give the reason**. The app never silently gives an agent more power.

| Level | Claude Code | Codex | Gemini CLI | Aider | Chat API |
|---|---|---|---|---|---|
| talk | ✅ `--tools ""` (no tools) | ⚠ read-only sandbox, told to only discuss¹ | ✅ policy denying every tool | ⚠ ask mode, told to only discuss¹ | ✅ (has no tools) |
| read | ✅ read tools + `git diff/log/status`, tests in the sandbox | ✅ `read-only` sandbox | ✅ `--approval-mode default` | ✅ ask mode | ❌ |
| edit | ✅ edit tools, no Bash | ❌ no "edit but no commands" setting | ✅ `--approval-mode auto_edit` | ✅ code mode, shell commands off, no auto-commits | ❌ |
| edit + commands | ✅ sandboxed Bash (bubblewrap / macOS) | ✅ `workspace-write` (bubblewrap / macOS) | ✅ `yolo` inside `--sandbox` (needs Docker/Podman, or Seatbelt on macOS) | ❌ no sandbox | ❌ |
| auto | ✅ auto mode | ✅ approve-for-me (needs the sandbox) | ❌ | ❌ | ❌ |

¹ The AI can still read files; it's told to only discuss, and the after-reply check (§5.4) flags any change.
A level an AI can't enforce is greyed out; a level that needs a missing sandbox drops to the next
level down, and the agent's chip says *(limited)* with the reason. Verified against Claude Code 2.1,
Codex CLI 0.171, Gemini CLI 0.70 (installed source) and Aider 0.86 (docs + source).

### 5.4 After-reply check
After every reply, the app compares the folder before and after. If an agent changed files its level didn't allow
(or a Tester changed non-test files), the reply is flagged **⚠**. This already exists for the Reviewer and is generalized.

### 5.5 Native Windows
Neither Claude Code's nor Codex's command sandbox works properly on native Windows (they do in WSL). There,
`edit + commands` and `auto` are unavailable and a banner suggests WSL for full features.

---

## 6. Workflow: who answers when

### 6.1 Order of turns
For each user message in a chat with roles:

**Planner + Critic → ⏸ wait for your OK on the plan → Lead → Tester → Reviewer → back to you**

- Roles nobody holds are skipped. Without a Planner, it starts at the Lead.
- The pause after the plan exists so a plan you don't like doesn't waste tokens on work.
- Agents with **no role** (Participants) answer as today: together, then N discussion rounds.
- `@name` still asks one agent directly, outside the order.

### 6.2 Automatic fixes
If the Tester or Reviewer finds problems, the Lead automatically does **1** round of fixes, then it comes back to you.
Per-chat setting: 0–3 rounds.

### 6.3 Editing agents take turns
Agents that can edit run **one at a time** (edit lock). talk/read agents run in parallel.
(v2: each editing agent works in its own copy of the project instead; see §1.)

### 6.4 Carried over unchanged
Hand-offs (`@codex …` in a reply), PASS for "nothing to add", Stop, Retry / Reply, live progress, live preview,
reply stats, ▸ Diff, Compact, Markdown rendering, Export, skills (`/skill-name`), real resumable sessions.

---

## 7. Screens

### 7.1 New chat
Pick a folder → **+ Add agent** (or "add another copy") → name it, give it role(s) → its permissions fill in
automatically and can be changed → set fix rounds → start.
**Save as Team** stores this setup (e.g. "Claude builds, Codex reviews") so it can be reused in one click.

### 7.2 Settings: 3 tabs
- **Agents**: auto-detected CLIs (✅ found / ❌ not installed); add chat APIs (address, model, *name of the env variable*
  that holds the key); **Test** button sends a tiny "hello" and shows ✅/❌.
- **Roles**: built-ins (copy to adjust) and your own roles (name, job, default permission).
- **Teams**: rename, edit, delete saved teams.

### 7.3 In the chat
Each agent shows a badge with its name, role(s), permission level, and ⚠ if raised. 🔒 Talk-only lock button in the header.

---

## 8. Storage
Everything lives in **`~/.agent-groupchat/`** on all 3 OSes: `config.json` (agents, roles, teams), `chats/`, `logs/`.
The app folder holds only code, so updating or reinstalling never touches your data, and your data can't end up in git.

---

## 9. Platforms
| OS | Status |
|---|---|
| Linux | Full. Sandbox via `bubblewrap`; README keeps the Ubuntu 23.10+ AppArmor fix |
| macOS | Full. Both CLIs sandbox natively |
| Windows (WSL) | Full |
| Windows (native) | Works; command-running levels unavailable (§5.5) |

The bash launcher is replaced by the Python entry point (`agent-groupchat`), which works the same everywhere.

---

## 10. Tests
- **Fake AIs**: `tests/fake_agent.py` acts like an agent CLI and gives fixed answers, instantly and for free.
- What's covered: `@name` routing (incl. copies), role order + plan pause, fix rounds, permission mapping per adapter
  (the right flags are passed), ⚠ raised badge, Talk-only lock, edit lock (editors take turns), Reviewer/Tester ⚠ checks,
  config/teams load and save, Host/Origin/token security checks.
- **GitHub Actions** runs all tests on Linux, macOS and Windows on every push.

---

## 11. README
What it is + a GIF of a chat in action · one-command install · quick start · supported AIs and which permission
levels each can enforce (the §5.3 table) · **Safety** (raised permissions, Auto, no sandbox on native Windows,
"more agents = more cost") · link to Matt Pocock's skills · MIT license.

---

## 12. Milestones
Built in this group chat with **Lead + Review**: Claude leads, Codex reviews each milestone, the user approves between milestones.

| # | Milestone | Done when |
|---|---|---|
| M0 | Repo setup: copy `server.py`, `index.html`, README into `groupchat_project`; `.gitignore`, `LICENSE`, `git init`; storage moves to `~/.agent-groupchat/` | Runs from the new folder; no private files tracked |
| M1 | Adapter refactor: `Adapter` interface; Claude + Codex as adapters; fake-agent test harness | Behaves exactly like today; tests green |
| M2 | Any number of agents + copies, names, `@name`, per-copy model | A chat with Claude 1 + Claude 2 + Codex works |
| M3 | Roles, permissions (§5), workflow order, plan pause, fix rounds, edit lock | All §10 role/permission tests green |
| M4 | New adapters: Gemini CLI, Aider, OpenAI-compatible API; finalize §5.3 table | Each passes the Test button and its adapter tests |
| M5 | New chat screen, Teams, Settings page (Agents / Roles / Teams) | Everything in §7 is usable from the browser |
| M6 | Cross-platform: Python launcher, per-OS sandbox checks, Windows fallback + banner | CI green on all 3 OSes |
| M7 | Packaging (`pyproject.toml`, entry point), README + GIF, first public release on GitHub + PyPI | `pipx install agent-groupchat` works on a clean machine |

Pushing to GitHub and publishing to PyPI happen **only when the user says so**.

---

## 13. Small defaults I chose without asking (say if you want any changed)
- On first launch, existing chats in `~/groupchat/chats/` are **offered** for one-time import (copied, never moved).
- Max agents per chat: no hard limit, but a warning above 5 ("more agents = more cost and slower turns").
- The Talk-only lock is off by default for new chats.
- Custom role names must be unique; built-in role names are reserved.
- A Tester's "test files" = paths matching `test*`, `*_test.*`, `*.test.*`, `*.spec.*`, or anything under `tests/`, `test/`, `__tests__/` (configurable per role).
