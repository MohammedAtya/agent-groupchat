"""Copies of the project for ⚡ Race and 🧩 Split (SPEC §14): one git worktree per agent.

The user's folder is never touched until they choose a copy ("Use this one"), and then
only its working tree changes (nothing is committed or staged for them).

  snapshot(folder)        a commit of the folder exactly as it is now, uncommitted and new
                          (not ignored) files included, made with a temporary index: the
                          user's own staging area and files stay as they are
  make_copy(...)          a worktree on its own branch, starting from that snapshot
  save_copy(copy)         commit what the agent did in its copy -> the copy's diff
  use_copy(...)           put a copy's changes into the user's folder (uncommitted); refuses
                          and names the files if they clash with changes made meanwhile
  merge_copies(...)       merge several copies into a new one (Split, Combine) -> conflicts
  remove_copies(...)      delete worktrees, branches and snapshot refs
"""
import os
import shutil
import subprocess
import tempfile

# Commits the app makes inside copies use this identity (never the user's own config).
IDENT = ["-c", "user.name=agent-groupchat", "-c", "user.email=agent-groupchat@localhost",
         "-c", "commit.gpgsign=false"]


class GitError(Exception):
    pass


def git(cwd, *args, env=None, check=True, input=None):
    """Run git in `cwd`. -> stdout (str). Raises GitError with git's message on failure."""
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", input=input,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})})
    if check and r.returncode:
        raise GitError((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
    return r.stdout


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
    """Commit the folder exactly as it is now (tracked changes + new, non-ignored files)
    without touching the user's index or files. Kept alive under `ref`. -> commit sha."""
    root = repo_root(folder)
    if not root:
        raise GitError("not a git repo")
    fd, index = tempfile.mkstemp(prefix="gc-index-")
    os.close(fd)
    os.remove(index)  # git creates it
    env = {"GIT_INDEX_FILE": index}
    try:
        head = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
        if head:
            git(root, "read-tree", head, env=env)
        git(root, "add", "-A", env=env)
        tree = git(root, "write-tree", env=env).strip()
        sha = git(root, *IDENT, "commit-tree", tree, *(["-p", head] if head else []),
                  "-m", "agent-groupchat snapshot", env=env).strip()
    finally:
        if os.path.exists(index):
            os.remove(index)
    if ref:
        git(root, "update-ref", ref, sha)
    return sha


def make_copy(root, base, path, branch):
    """A worktree at `path` on `branch`, starting at commit `base`."""
    if os.path.exists(path):
        remove_worktree(root, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    git(root, "worktree", "add", "-q", "-f", "-B", branch, path, base)
    return path


def save_copy(path, message="agent work"):
    """Commit everything the agent changed in its copy. -> True if there was anything."""
    git(path, "add", "-A")
    if not git(path, "status", "--porcelain").strip():
        return False
    git(path, *IDENT, "commit", "-q", "-m", message)
    return True


def diff(root, base, branch, stat=False, binary=True):
    """What `branch` changed since `base` (the copy's diff). binary=False: for people to read."""
    args = ["diff", "--no-color", "--find-renames"] + (["--stat"] if stat else
                                                       ["--binary"] if binary else [])
    return git(root, *args, base, branch)


def changed_files(root, base, branch):
    out = git(root, "diff", "--name-only", "--find-renames", base, branch)
    return [l for l in out.splitlines() if l]


def changed_since(root, base):
    """Files that differ between `base` and the user's folder now. Compares two snapshots,
    so files git doesn't track yet (but that `base` included) count correctly."""
    now = snapshot(root)
    return {f for f in git(root, "diff", "--name-only", "--no-renames", base, now).splitlines() if f}


def use_copy(root, base, branch):
    """Put `branch`'s changes (since `base`) into the user's working tree, uncommitted.
    -> (True, files) or (False, clashing files) without changing anything. A clash is a
    file the copy changed that the user also changed since the snapshot."""
    files = changed_files(root, base, branch)
    if not files:
        return True, []
    clash = sorted(set(files) & changed_since(root, base))
    if clash:
        return False, clash
    patch = diff(root, base, branch)
    try:
        git(root, "apply", "--whitespace=nowarn", "-", input=patch)
    except GitError:
        return False, files
    return True, files


def merge_copies(root, base, branches, path, branch):
    """A new copy at `path` starting at `base` with every branch in `branches` merged in.
    -> list of files with conflicts (left with conflict markers for an agent to fix)."""
    make_copy(root, base, path, branch)
    conflicts = []
    for b in branches:
        r = subprocess.run(["git", *IDENT, "merge", "--no-ff", "--no-edit", "-q", b], cwd=path,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env={**os.environ, "LC_ALL": "C", "GIT_MERGE_AUTOEDIT": "no"})
        if r.returncode:
            unmerged = git(path, "diff", "--name-only", "--diff-filter=U").split()
            if not unmerged:
                raise GitError((r.stderr or r.stdout).strip())
            conflicts += unmerged
            # keep the conflict markers in the files, record the merge so the next one can go on
            git(path, "add", "-A")
            git(path, *IDENT, "commit", "-q", "--no-edit", "-m", f"merge {b} (with conflicts)")
    return sorted(set(conflicts))


def remove_worktree(root, path):
    git(root, "worktree", "remove", "--force", path, check=False)
    shutil.rmtree(path, ignore_errors=True)
    git(root, "worktree", "prune", check=False)


def remove_copies(root, paths, branches, refs=()):
    """Delete copies, their branches and snapshot refs. Missing ones are fine."""
    for p in paths:
        remove_worktree(root, p)
    for b in branches:
        git(root, "branch", "-D", b, check=False)
    for r in refs:
        git(root, "update-ref", "-d", r, check=False)
