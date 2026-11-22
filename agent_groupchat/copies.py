"""Copies of the project for ⚡ Race and 🧩 Split (SPEC §14): one git worktree per agent.

The user's folder is never touched until they choose a copy ("Use this one"), and then
only its working tree changes (nothing is committed or staged for them).

  snapshot(folder)        a commit of the folder exactly as it is now: the user's staged
                          state plus every change and new (not ignored) file inside the chat's
                          folder, made with a temporary index (theirs stays as it is)
  unsafe_links(...)       symlinks in the snapshot that point outside the project
  make_copy(...)          a worktree on its own branch, starting from that snapshot
  save_copy(copy)         commit what the agent did in its copy
  use_copy(...)           put a copy's changes into the user's folder, byte-exact and
                          uncommitted; files both sides changed are merged line by line, and
                          it refuses (changing nothing) if the same lines clash
  merge_copies(...)       merge several copies into a new one (Split, Combine) -> conflicts
  remove_copies(...)      delete worktrees, branches and snapshot refs -> problems

Every git command here runs with the repo's hooks switched off and without the GIT_*
environment variables that could point it at another repository or index.
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile

# Commits the app makes inside copies use this identity (never the user's own config).
IDENT = ["-c", "user.name=agent-groupchat", "-c", "user.email=agent-groupchat@localhost",
         "-c", "commit.gpgsign=false"]
# Hooks would run the repo's own scripts (e.g. post-checkout) on the app's behalf, before the
# user chose anything: point git at an empty folder instead.
NO_HOOKS = tempfile.mkdtemp(prefix="gc-no-hooks-")
SAFE = ["-c", f"core.hooksPath={NO_HOOKS}", "-c", "core.fsmonitor=false",
        "-c", "advice.detachedHead=false"]


class GitError(Exception):
    pass


def clean_env(extra=None):
    """The environment without GIT_* variables (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, …
    would send git to another repository or index)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    env.update(extra or {})
    return env


def run(cwd, *args, env=None, input=None):
    """Run git. Input/output are bytes: in text mode Python turns every \\n written to a pipe
    into \\r\\n on Windows, and decoding would change non-UTF-8 files."""
    return subprocess.run(["git", *SAFE, *args], cwd=cwd, capture_output=True,
                          input=input if isinstance(input, (bytes, type(None))) else input.encode("utf-8"),
                          env=clean_env(env))


def git_bytes(cwd, *args, env=None, check=True, input=None):
    r = run(cwd, *args, env=env, input=input)
    if check and r.returncode:
        raise GitError((r.stderr.decode("utf-8", "replace") or r.stdout.decode("utf-8", "replace")).strip()
                       or f"git {args[0]} failed")
    return r.stdout


def git(cwd, *args, env=None, check=True, input=None):
    """Run git in `cwd`. -> stdout as text (for names and messages, not file contents)."""
    return git_bytes(cwd, *args, env=env, check=check, input=input).decode("utf-8", "replace")


def git_available():
    return shutil.which("git") is not None


def repo_root(folder):
    """The top folder of the git repo `folder` is in, or None."""
    if not git_available():
        return None
    try:
        out = git(folder, "rev-parse", "--show-toplevel").strip()
    except (GitError, OSError):
        return None
    return os.path.realpath(out) if out else None


def scope_of(root, folder):
    """The chat's folder relative to the repo top, with / ('.' for the top itself)."""
    rel = os.path.relpath(os.path.realpath(folder), root)
    return "." if rel == "." else rel.replace(os.sep, "/")


def in_scope(path, scope):
    return scope == "." or path == scope or path.startswith(scope + "/")


def status(folder):
    """For the page: can Race/Split run here, and if not, why."""
    if not git_available():
        return {"ok": False, "reason": "git isn't installed", "can_init": False}
    if repo_root(folder):
        return {"ok": True, "reason": "", "can_init": False}
    return {"ok": False, "reason": "the project folder isn't a git repo", "can_init": True}


def init_repo(folder):
    """"Initialize git here": git init + a first commit of the current files (files unchanged)."""
    if repo_root(folder):
        raise GitError("this folder is already in a git repo")
    git(folder, "init", "-q")
    git(folder, "add", "-A")
    git(folder, *IDENT, "commit", "-q", "--allow-empty", "-m", "Start (agent-groupchat)")
    return repo_root(folder)


def snapshot(folder, ref=None):
    """Commit the folder exactly as it is now, without touching the user's index or files:
    start from a copy of their index (so staged files, even force-added ignored ones, are
    in), then add every change and new non-ignored file inside the chat's folder. Kept alive
    under `ref`. -> commit sha."""
    root = repo_root(folder)
    if not root:
        raise GitError("not a git repo")
    scope = scope_of(root, folder)
    real_index = os.path.join(root, git(root, "rev-parse", "--git-path", "index").strip())
    tmpdir = tempfile.mkdtemp(prefix="gc-index-")
    index = os.path.join(tmpdir, "index")
    env = {"GIT_INDEX_FILE": index}
    try:
        head = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
        if os.path.exists(real_index):
            shutil.copyfile(real_index, index)
        elif head:
            git(root, "read-tree", head, env=env)
        git(root, "add", "-A", "--", scope, env=env)
        r = run(root, "write-tree", env=env)
        if r.returncode:
            raise GitError("your folder has unresolved merge conflicts; finish or abort that merge first")
        tree = r.stdout.decode().strip()
        sha = git(root, *IDENT, "commit-tree", tree, *(["-p", head] if head else []),
                  "-m", "agent-groupchat snapshot", env=env).strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if ref:
        git(root, "update-ref", ref, sha)
    return sha


def _tree(root, commit):
    """{path: (mode, blob sha)} of a commit."""
    out = git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries = {}
    for item in out.split(b"\0"):
        if item:
            meta, path = item.split(b"\t", 1)
            mode, _, sha = meta.decode().split(" ")
            entries[path.decode("utf-8", "surrogateescape")] = (mode, sha)
    return entries


def unsafe_links(root, commit):
    """Symlinks in `commit` pointing outside the repo (absolute, or ../ past the top): an agent
    writing through one in its copy would change the user's real files."""
    bad = []
    for path, (mode, sha) in _tree(root, commit).items():
        if mode != "120000":
            continue
        target = git_bytes(root, "cat-file", "blob", sha).decode("utf-8", "replace")
        if os.path.isabs(target) or target.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", target):
            bad.append(path)
            continue
        joined = os.path.normpath(os.path.join(os.path.dirname(path), target)).replace(os.sep, "/")
        if joined == ".." or joined.startswith("../"):
            bad.append(path)
    return sorted(bad)


def has_submodules(root, commit):
    return any(mode == "160000" for mode, _ in _tree(root, commit).values())


def make_copy(root, base, path, branch, scope="."):
    """A worktree at `path` on `branch`, starting at commit `base` (hooks don't run).
    -> the chat's folder inside the copy (made if the snapshot had no files there)."""
    if os.path.exists(path):
        remove_worktree(root, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    git(root, "worktree", "add", "-q", "-f", "-B", branch, path, base)
    folder = path if scope == "." else os.path.join(path, *scope.split("/"))
    os.makedirs(folder, exist_ok=True)
    return folder


def head(path):
    return git(path, "rev-parse", "HEAD").strip()


def save_copy(path, message="agent work"):
    """Commit everything the agent changed in its copy. -> True if there was anything."""
    git(path, "add", "-A")
    if not git(path, "status", "--porcelain").strip():
        return False
    git(path, *IDENT, "commit", "-q", "--no-verify", "-m", message)
    return True


def reset_copy(path, commit):
    """Throw away everything in the copy after `commit` (commits and loose changes)."""
    git(path, "reset", "-q", "--hard", commit)
    git(path, "clean", "-q", "-fd")


def name_status(root, a, b):
    """[(status letter, path)] between two commits. No rename detection: a rename is a
    delete and an add, so both paths count."""
    out = git_bytes(root, "diff", "--name-status", "--no-renames", "-z", a, b).split(b"\0")
    items = [x.decode("utf-8", "surrogateescape") for x in out if x]
    return [(items[i][0], items[i + 1]) for i in range(0, len(items) - 1, 2)]


def changed_files(root, base, branch):
    return sorted(p for _, p in name_status(root, base, branch))


def diff(root, base, branch, stat=False, binary=True):
    """What `branch` changed since `base`, for people to read (and the Reviewer)."""
    args = ["diff", "--no-color", "--find-renames"] + (["--stat"] if stat else
                                                       ["--binary"] if binary else [])
    return git(root, *args, base, branch)


def conflict_markers(folder, files):
    """Files (relative to `folder`) that still contain <<<<<<< / >>>>>>> conflict markers."""
    left = []
    for f in files:
        try:
            with open(os.path.join(folder, *f.split("/")), "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if re.search(rb"(^|\n)(<{7}|>{7})( |\r?\n|$)", data):
            left.append(f)
    return sorted(left)


def _write(root, path, mode, data):
    """Write `data` (bytes, already in working-tree form) as `path` with git's file mode."""
    dest = os.path.join(root, *path.split("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.islink(dest) or (os.path.exists(dest) and mode == "120000"):
        os.remove(dest)
    if mode == "120000":
        try:
            os.symlink(data.decode("utf-8", "surrogateescape"), dest)
            return
        except (OSError, NotImplementedError):
            pass  # (no symlinks, e.g. on Windows without the right): write it as a file
    tmp = dest + ".gc-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    if mode == "100755" and os.name != "nt":
        os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, dest)


def _checkout_bytes(root, path, sha):
    """A blob as it should be on disk for `path` (line endings and filters applied)."""
    return git_bytes(root, "cat-file", "--filters", f"--path={path}", sha)


def _merge3(root, base_sha, ours_sha, theirs_sha):
    """Line-level 3-way merge of three blobs. -> merged blob sha, or None if they clash."""
    blobs = [git_bytes(root, "cat-file", "blob", s) if s else b"" for s in (ours_sha, base_sha, theirs_sha)]
    if any(b"\0" in b for b in blobs):
        return None  # binary: can't merge lines
    tmp = tempfile.mkdtemp(prefix="gc-merge-")
    try:
        names = []
        for name, data in zip(("ours", "base", "theirs"), blobs):
            p = os.path.join(tmp, name)
            with open(p, "wb") as f:
                f.write(data)
            names.append(p)
        r = run(tmp, "merge-file", "-p", "-q", *names)
        if r.returncode != 0:
            return None
        return git(root, "hash-object", "-w", "--stdin", "--no-filters", input=r.stdout).strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def use_copy(root, base, branch, scope="."):
    """Put `branch`'s changes (since `base`) into the user's working tree, uncommitted.
    -> (ok, files, why). why: "" | "outside" (the copy changed files outside the chat's
    folder) | "clash" (the user changed the same lines meanwhile). Nothing is written
    unless everything can be."""
    changes = name_status(root, base, branch)
    files = sorted(p for _, p in changes)
    if not files:
        return True, [], ""
    outside = [p for p in files if not in_scope(p, scope)]
    if outside:
        return False, outside, "outside"
    folder = root if scope == "." else os.path.join(root, *scope.split("/"))
    now = snapshot(folder)
    user = {p for _, p in name_status(root, base, now)}
    t_base, t_copy, t_now = _tree(root, base), _tree(root, branch), _tree(root, now)
    plan, clash = [], []
    for st, p in changes:
        if p not in user:  # only the copy changed it: take the copy's version exactly
            plan.append((p, t_copy.get(p)))
            continue
        mine, theirs, orig = t_now.get(p), t_copy.get(p), t_base.get(p)
        if mine == theirs:
            continue  # both made the same change
        if not mine or not theirs or "120000" in (mine[0], theirs[0]) or mine[0] != theirs[0]:
            clash.append(p)  # deleted on one side, or a symlink / mode change on both
            continue
        merged = _merge3(root, orig[1] if orig else None, mine[1], theirs[1])
        if not merged:
            clash.append(p)
        else:
            plan.append((p, (theirs[0], merged)))
    if clash:
        return False, sorted(clash), "clash"
    for p, entry in plan:
        if entry is None:
            dest = os.path.join(root, *p.split("/"))
            if os.path.lexists(dest):
                os.remove(dest)
        else:
            mode, sha = entry
            data = git_bytes(root, "cat-file", "blob", sha) if mode == "120000" \
                else _checkout_bytes(root, p, sha)
            _write(root, p, mode, data)
    return True, files, ""


def merge_copies(root, base, branches, path, branch, scope="."):
    """A new copy at `path` starting at `base` with every branch in `branches` merged in.
    -> files with conflicts (left with conflict markers for an agent to fix)."""
    make_copy(root, base, path, branch, scope)
    conflicts = []
    for b in branches:
        r = run(path, *IDENT, "merge", "--no-ff", "--no-edit", "--no-verify", "-q", b,
                env={"GIT_MERGE_AUTOEDIT": "no"})
        if r.returncode:
            unmerged = [x.decode("utf-8", "surrogateescape") for x in
                        git_bytes(path, "diff", "--name-only", "-z", "--diff-filter=U").split(b"\0") if x]
            if not unmerged:
                raise GitError((r.stderr or r.stdout).decode("utf-8", "replace").strip())
            conflicts += unmerged
            # keep the conflict markers in the files, record the merge so the next one can go on
            git(path, "add", "-A")
            git(path, *IDENT, "commit", "-q", "--no-edit", "--no-verify", "-m", f"merge {b} (with conflicts)")
    return sorted(set(conflicts))


def remove_worktree(root, path):
    """-> '' or what went wrong. --force twice also removes a locked worktree."""
    r = run(root, "worktree", "remove", "--force", "--force", path)
    shutil.rmtree(path, ignore_errors=True)
    git(root, "worktree", "prune", check=False)
    listed = os.path.realpath(path) in {os.path.realpath(l[9:]) for l in
                                        git(root, "worktree", "list", "--porcelain").splitlines()
                                        if l.startswith("worktree ")}
    return f"{path}: {r.stderr.decode('utf-8', 'replace').strip()}" if listed else ""


def remove_copies(root, paths, branches, refs=()):
    """Delete copies, their branches and snapshot refs. -> problems (empty if all went)."""
    problems = [e for e in (remove_worktree(root, p) for p in paths) if e]
    for b in branches:
        r = run(root, "branch", "-D", b)
        if r.returncode and git(root, "branch", "--list", b).strip():
            problems.append(f"branch {b}: {r.stderr.decode('utf-8', 'replace').strip()}")
    for ref in refs:
        git(root, "update-ref", "-d", ref, check=False)
    return problems
