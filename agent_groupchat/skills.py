"""Finding the skills (SKILL.md files) each agent can run from a folder."""
import os
import re

HOME = os.path.expanduser("~")


def frontmatter(path):
    meta = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read(8000).splitlines()
    except OSError:
        return meta
    if not lines or lines[0].strip() != "---":
        return meta
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([\w-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            meta[key] = "" if val in (">", ">-", "|", "|-") else val.strip("\"'")
        elif key and line.startswith((" ", "\t")):  # YAML folded/indented continuation
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta


def ancestors(folder):
    """folder and its parents, stopping at $HOME (inclusive) or the root (/ or C:\\)."""
    out, cur = [], os.path.abspath(folder)
    while True:
        out.append(cur)
        parent = os.path.dirname(cur)
        if cur == HOME or parent == cur:
            break
        cur = parent
    return out


def scan_skill_dirs(roots, recursive=False):
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        if recursive:
            for dp, dns, fns in os.walk(root):
                if "SKILL.md" in fns:
                    found.append(os.path.join(dp, "SKILL.md"))
                    dns[:] = []
        else:
            for name in sorted(os.listdir(root)):
                p = os.path.join(root, name, "SKILL.md")
                if os.path.isfile(p):
                    found.append(p)
    return found
