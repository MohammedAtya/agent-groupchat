# Using agent-groupchat: a guide

This guide is about *how to use* agent-groupchat well. Installing, the list of AIs and the
details of each feature are in the [README](../README.md); this page links there instead of
repeating it.

## 1. Before your first chat

1. **Have at least one AI ready**: a CLI you've logged into once in a terminal, or an API key.
   See [Add an agent](../README.md#add-an-agent-a-cli) and
   [Add an AI with an API key](../README.md#add-an-ai-with-an-api-key).
2. **Check it in ⚙ Settings → Agents** with **Test**.
3. **Use git in your project.** Agents can change files; git lets you see and undo every change
   (and ⚡ Race / 🧩 Split need it).
4. **On Linux**, if an agent's chip says *(limited)*, do the one-time
   [sandbox fix](../README.md#enable-the-sandbox-linux) so agents can run commands.

## 2. Pick a setup

Start small: more agents means more cost and longer turns. Each row is a setup for **+ New**: the
first ones are one-click presets there (**Just chat**, **Build + review**, **Full team**, **Debate club**);
save your own with **💾 Save as team**.

| You want… | Agents and roles | Notes |
|---|---|---|
| A helper, like a normal AI chat | 1 agent, no role | The simplest start. |
| A second opinion | 2 different AIs, no roles, discussion rounds on | Both answer, then reply to each other. Use 🔒 Lock if you only want ideas, no edits. |
| Work done, then checked | **Lead** (e.g. Claude) + **Reviewer** (e.g. Codex) | The Reviewer can't edit; on `VERDICT: FIX` the Lead fixes it automatically. |
| The full team | **Planner** → **Lead** → **Tester** → **Reviewer** | You OK the plan before any work starts. Give the Tester a cheaper model. |
| Two areas at once | 2 **Leads** with a **focus** each (`backend` / `frontend`), plus a Reviewer | They take turns and build on each other. Different focuses, or you get the [overlap warning](../README.md#roles-and-the-order-of-turns). |
| Several testers | 2+ **Testers** with a focus each (`login`, `database`) | Each tests its own area. |
| A decision, not code (which library, which design) | 2+ agents (a Critic as judge is ideal), then **⚖ Debate** | They argue For / Against / Neutral, answer each other, and the judge recommends, with buttons to choose. |
| The best of several tries | 2+ agents that can edit, then **⚡ Race** | Same task, separate copies; you pick the winner. |
| A big task, faster | 2+ agents that can edit (+ a Planner), then **🧩 Split** | The Planner splits the work, or you write each agent's part; the parts are merged and checked. |

Which AI for which role:

- **Lead / Tester** (they edit and run commands): Claude Code, Codex, or Antigravity (on Ubuntu
  after its [one-time sandbox fix](../README.md#enable-antigravitys-sandbox-ubuntu)). Aider can
  edit but not run commands, so it can't run tests.
- **Reviewer / Planner / Critic** (they read and talk): any AI. A chat API (API key) is a cheap
  extra opinion here.
- **Same AI twice** is fine: *Claude 1* as Lead and *Claude 2* as Reviewer are separate sessions.

## 3. Working in a chat

- **Write the task like you'd brief a colleague**: what you want, where (files, folders), and how
  you'll know it's done ("the tests in `tests/test_api.py` pass").
- **Read the hint above the message box** before sending: it shows who will answer, in which
  order (`→ Claude plans → ⏸️ your OK → Codex works → Claude reviews`).
- **`@name`** talks to one agent only (`@codex is this safe?`), outside the role order. Good for
  quick questions without starting the whole team.
- **After a plan**, press **▶ Go ahead**, or reply to change it. Nothing is edited before that.
- **Watch, and stop early**: if an agent heads the wrong way, press **■ Stop**, then say what to
  do differently. **↻ Retry** reruns a failed reply.
- **Check changes with ▸ Diff** under a reply, and in git (`git diff`) before you commit. Click a
  line in the diff to comment on it, then **Send** the comments to the agent that made the change.
- **Read the ⚠ Loose ends** under a Lead's reply (and on Race/Split cards): what it says is still
  unfinished or temporary.
- **Change the team any time** in the panel: **👥 Edit roles & permissions** or the **🔒 Talk-only lock** (everyone
  talk-only, e.g. while you discuss a design).
- **Long chat getting slow or expensive?** **Compact** summarizes it and the agents continue in
  fresh sessions.
- **Keep going in a terminal**: the bottom of the chat shows the command to resume each agent's
  session.
- **Want more room?** **⟩ Hide panel** gives the chat the full width; the header still shows the agents'
  faces and what they're doing.
- **Doing something else meanwhile?** Turn on **🔔 Notifications** in the panel: you get a sound and a
  notification when the agents finish or need you, and the tab title shows how many replies came in.
- **When an agent offers buttons** under its reply, a click answers that agent; you can still type
  anything else instead.

## 4. Spend less

- Every agent reads the chat on every turn: use only the agents you need, and `@name` for small
  questions.
- Give checking roles (Tester, Reviewer) a cheaper model; keep the strong one as Lead.
- Keep discussion rounds at 0–1 and automatic fix rounds at 1 unless you need more.
- The cost (≈ $, where the CLI reports it) and tokens are shown under each reply.

## 5. When something goes wrong

| What you see | What to do |
|---|---|
| The AI says *(not installed)*, or **Test** says `` `agy` isn't installed (not on PATH) `` | Install it, check the command works in a terminal, then start agent-groupchat **from that terminal**. |
| An agent's reply is an `(error)` about logging in or authentication | Run that CLI once in a terminal and sign in (**Test** only checks it runs, not the login). |
| Gemini CLI: *Google no longer serves the Gemini CLI to personal accounts* | Use the **Antigravity** agent (`agy`) instead, or a Gemini chat API with an API key. |
| Antigravity is *(limited)* to Edit files | On Ubuntu its terminal sandbox is blocked: do the [one-time fix](../README.md#enable-antigravitys-sandbox-ubuntu), then reload the page. |
| *Antigravity tried something that needs your approval…* | It tried something outside its sandbox (e.g. a file outside the project), which a group-chat turn can't approve. Ask it to stay in the project, or give that step to Claude or Codex. |
| A permission is greyed out | That AI can't enforce it; hover for the reason. Pick another AI for that role. |
| The chip says *(limited)* / `bwrap: setting up uid map: Permission denied` | Linux sandbox: see [Enable the sandbox](../README.md#enable-the-sandbox-linux). |
| API **Test**: *The environment variable GEMINI_API_KEY isn't set.* | Set it (see [Add an AI with an API key](../README.md#add-an-ai-with-an-api-key)) and **restart** agent-groupchat from a terminal that has it. |
| API **Test**: *Can't reach …: HTTP Error 401* | The key is wrong or expired; check it with the provider. |
| API **Test**: *Reached the API, but it has no model 'x'.* | Copy the exact model name from the provider's model list. |
| In a Race/Split: *Not copied (too big): node_modules (over 500 MB)* | Copies get your git-ignored files up to 500 MB. Agents are told what's missing: allow them to install it in their copy (Edit + run commands), or use a normal message instead. |
| 🔔 is on but no desktop notification appears | The browser blocked them for this page: allow notifications for `127.0.0.1` in its site settings. The sound and the `(1)` in the tab title still work. |
| ⚡ Race / 🧩 Split are greyed out | They need a git repo (the page offers **Initialize git here**) and 2+ agents that can edit (Leads, or agents without a role). |
| ⚠ *changed files although its permission is …* under a reply | An agent changed files it shouldn't have. Check ▸ Diff and undo with git if needed. |
| An agent shows ⏳ *waiting for its turn to edit* | Normal: agents that edit take turns. |
| The page says the server isn't running | Start it again with `agent-groupchat`; your chats are kept in `~/.agent-groupchat/`. |

Found a bug or missing a feature? Open an [issue](https://github.com/MohammedAtya/agent-groupchat/issues).
