"""Roles (what each agent's job is) and permission levels (what it may do).

A role gives an agent a job and a DEFAULT permission level. The user can raise or
lower the level per agent; an agent with several roles gets the highest default.
Each adapter maps a level to its CLI's real permission flags (see adapters/).
"""
import json
import os
import re
from dataclasses import dataclass

# Permission levels, weakest first.
LEVELS = ["talk", "read", "edit", "work", "auto"]
LEVEL_NAMES = {
    "talk": "Talk only",
    "read": "Read",
    "edit": "Edit files",
    "work": "Edit + run commands",
    "auto": "Auto",
}
LEVEL_HELP = {
    "talk": "Only discusses: no file access.",
    "read": "Reads files; may run read-only checks (git diff, tests) where that can be enforced.",
    "edit": "Edits files in the project folder, runs no commands.",
    "work": "Edits files and runs commands, sandboxed to the project folder.",
    "auto": "Decides on its own like auto mode in the terminal; risky actions are blocked.",
}


def rank(level):
    return LEVELS.index(level)


# Workflow stages, in the order they run for each message (see server.Flow).
STAGES = ["plan", "work", "test", "review"]


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    job: str            # told to the agent holding the role
    level: str          # default permission level
    stage: str = ""     # workflow stage it acts in; "" = answers like a normal participant
    builtin: bool = False
    verdict: bool = False  # ends its reply with VERDICT: LGTM / FIX (testers, reviewers)


BUILTIN_ROLES = [
    Role("planner", "Planner",
         "You are the Planner. Before any work starts, break the user's request into clear steps "
         "and ask the questions that must be answered first. Don't do the work yourself.",
         "read", stage="plan", builtin=True),
    Role("critic", "Critic",
         "You are the Critic. Challenge the idea or approach: point out risks, wrong assumptions "
         "and what everyone else missed. Be specific and constructive; don't do the work.",
         "read", stage="plan", builtin=True),
    Role("lead", "Lead",
         "You are the Lead. You do the actual work the user asks for (code, files, commands).",
         "work", stage="work", builtin=True),
    Role("tester", "Tester",
         "You are the Tester. Write and run tests for the Lead's work and report what fails. "
         "Only add or change TEST files; never change the code under test yourself.",
         "work", stage="test", builtin=True, verdict=True),
    Role("reviewer", "Reviewer",
         "You are the Reviewer. Read the Lead's changes and check them against what the user "
         "asked: bugs, missed requirements, risky edits. Say concretely what to fix. Don't modify "
         "files.", "read", stage="review", builtin=True, verdict=True),
    Role("participant", "Participant",
         "", "work", builtin=True),
]
BUILTIN = {r.id: r for r in BUILTIN_ROLES}
DEFAULT_ROLE = "participant"

# Same agent as Lead and Reviewer would review its own work.
FORBIDDEN_PAIRS = [("lead", "reviewer")]

# A Tester's "test files" (paths relative to the project folder).
TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__)/|(^|/)test[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$")


def is_test_file(path):
    return bool(TEST_FILE_RE.search(path.replace("\\", "/")))


def load_custom_roles(config_file):
    """Custom roles from config.json: {"roles": [{"id", "name", "job", "level"}]}.
    Bad entries are skipped; built-in ids are reserved."""
    try:
        with open(config_file) as f:
            items = json.load(f).get("roles") or []
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for r in items:
        try:
            rid = re.sub(r"[^a-z0-9-]+", "-", str(r.get("id") or r["name"]).lower()).strip("-")
            if rid and rid not in BUILTIN and r.get("level", "read") in LEVELS:
                out.append(Role(rid, str(r["name"])[:40], str(r.get("job", ""))[:2000],
                                r.get("level", "read")))
        except (KeyError, TypeError, AttributeError):
            continue
    return out


def all_roles(config_file=None):
    roles = dict(BUILTIN)
    if config_file and os.path.exists(config_file):
        for r in load_custom_roles(config_file):
            roles.setdefault(r.id, r)
    return roles
