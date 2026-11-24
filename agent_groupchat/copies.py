"""Copies of the project for ⚡ Race and 🧩 Split (SPEC §14): one git worktree per agent.

The user's folder is never touched until they choose a copy ("Use this one"), and then
only its working tree changes (nothing is committed or staged for them).

  snapshot(folder)        a commit of the folder exactly as it is now: the user's staged
                          state plus every change and new (not ignored) file inside the chat's
                          folder, made with a temporary index (theirs stays as it is)
  unsafe_links(...)       symlinks in the snapshot that point outside the project
  make_copy(...)          a worktree on its own branch, starting from that snapshot
  copy_ignored(...)       copy the user's git-ignored files (.env, node_modules, .venv…) into a
                          copy, so tests can run there; they stay ignored, so they never come back
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
import threading

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


# The app's own git commands run one at a time: copies of one repo share its lock files
# (refs, worktree list), and two at once can make one fail (seen on slow CI machines).
# They're quick; the agents themselves still work in parallel.
_git_lock = threading.RLock()


def run(cwd, *args, env=None, input=None):
    """Run git. Input/output are bytes: in text mode Python turns every \\n written to a pipe
    into \\r\\n on Windows, and decoding would change non-UTF-8 files."""
    with _git_lock:
        return _run(cwd, *args, env=env, input=input)


def _run(cwd, *args, env=None, input=None):
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
    start from the entries of their index (so staged files, even force-added ignored ones, are
    in), then add every change and new non-ignored file inside the chat's folder. Kept alive
    under `ref`. -> commit sha.
    The entries are copied WITHOUT their stat data (size, times): with it, a file changed in the
    same second at the same size looks unchanged to git ("racy git") and would be missed."""
    root = repo_root(folder)
    if not root:
        raise GitError("not a git repo")
    scope = scope_of(root, folder)
    tmpdir = tempfile.mkdtemp(prefix="gc-index-")
    index = os.path.join(tmpdir, "index")
    env = {"GIT_INDEX_FILE": index}
    try:
        head = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
        entries = git_bytes(root, "ls-files", "--stage", "-z")  # the user's index: mode, sha, stage, path
        if entries:
            git(root, "update-index", "-z", "--index-info", env=env, input=entries)
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
    """Symlinks in `commit` that lead outside the repo, resolving them step by step like the
    OS would (so chains such as l1 -> sub/l2/.. with sub/l2 -> .. are caught): an agent writing
    through one in its copy would change the user's real files."""
    links = {}
    for path, (mode, sha) in _tree(root, commit).items():
        if mode == "120000":
            links[path] = git_bytes(root, "cat-file", "blob", sha).decode("utf-8", "replace")

    def escapes(path):
        todo, done, hops = path.split("/"), [], 0
        while todo:
            part = todo.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not done:
                    return True  # above the repo's top
                done.pop()
                continue
            here = "/".join(done + [part])
            if here in links:
                hops += 1
                target = links[here]
                if hops > 40 or os.path.isabs(target) or target.startswith(("/", "\\")) \
                        or re.match(r"^[A-Za-z]:", target):
                    return True
                todo = target.replace("\\", "/").split("/") + todo  # relative to the link's folder
                continue
            done.append(part)
        return False
    return sorted(p for p in links if escapes(p))


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


# Git-ignored things never copied: caches and build output (re-made when needed).
IGNORED_SKIP = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".tox",
                ".nox", ".hypothesis", "dist", "build", "target", ".next", ".nuxt", ".turbo",
                ".parcel-cache", ".gradle", "coverage", "htmlcov", ".DS_Store", "Thumbs.db"}
MAX_IGNORED_BYTES = 500 * 1024 ** 2  # per copy: bigger ignored folders are left out (and named)
MAX_IGNORED_FILES = 60_000


def _size(path, limit_bytes, limit_files):
    """(bytes, files) of a file or folder, without following symlinks; bytes is None when
    it stopped counting because it went past a limit."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size, 1
    total, n = 0, 0
    for top, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in IGNORED_SKIP]
        for f in files + [d for d in dirs if os.path.islink(os.path.join(top, d))]:
            if f in IGNORED_SKIP or _special(os.path.join(top, f)):
                continue
            try:
                total += os.lstat(os.path.join(top, f)).st_size
            except OSError:
                continue
            n += 1
            if total > limit_bytes or n > limit_files:
                return None, n  # past the limit: stopped counting
    return total, n


def _special(path):
    """A named pipe, socket or device file: never copied (it holds no content)."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return True
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode)


def _skip(folder, names):
    """copytree's ignore: caches, build output and special files."""
    return [n for n in names if n in IGNORED_SKIP or _special(os.path.join(folder, n))]


def _in_folder(folder, change):
    """Run change() on an entry of `folder`. The copy is ours, so a read-only folder (e.g. a
    0555 package in node_modules) is opened for a moment if needed."""
    try:
        return change()
    except PermissionError:
        mode = os.stat(folder).st_mode
        os.chmod(folder, mode | stat.S_IWUSR | stat.S_IXUSR)
        try:
            return change()
        finally:
            os.chmod(folder, mode)


def _remove(path):
    """Delete a file, link or folder in the copy, read-only folders included. -> gone?"""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            for top, dirs, _ in os.walk(path):
                for d in [top] + [os.path.join(top, x) for x in dirs]:
                    if not os.path.islink(d):
                        os.chmod(d, os.stat(d).st_mode | stat.S_IWUSR | stat.S_IXUSR)
            _in_folder(os.path.dirname(path), lambda: shutil.rmtree(path))
        elif os.path.lexists(path):
            _in_folder(os.path.dirname(path), lambda: os.remove(path))
    except OSError:
        pass
    return not os.path.lexists(path)


def _retarget(link, target):
    def change():
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(target, link, target_is_directory=os.path.isdir(target))  # (Windows needs to know)
    _in_folder(os.path.dirname(link), change)


def _links_in(path):
    """Every symlink at or under `path` (not following them)."""
    if os.path.islink(path):
        return [path]
    found = []
    if os.path.isdir(path):
        for top, dirs, files in os.walk(path):
            found += [os.path.join(top, n) for n in files + dirs if os.path.islink(os.path.join(top, n))]
    return found


def _real(path):
    """realpath, comparable: Windows may add a \\?\ prefix (readlink, realpath) and ignores case."""
    p = os.path.realpath(path)
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normcase(p)


def _inside(path, folder):
    path, folder = _real(path), _real(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _link_maps(src, dest):
    """([(user path, copy path)] for the chat's folder and, if different, the repo's top;
    the copy's top folder)."""
    maps, root = [(os.path.realpath(src), os.path.realpath(dest))], os.path.realpath(dest)
    try:
        top_src = os.path.realpath(git(src, "rev-parse", "--show-toplevel").strip())
        root = os.path.realpath(git(dest, "rev-parse", "--show-toplevel").strip())
        if top_src != maps[0][0]:
            maps.append((top_src, root))
    except GitError:
        pass
    return maps, root


def _contain_links(links, maps, copy_root):
    """No symlink in the copy may lead out of it. 1) An absolute link into the user's project
    is re-pointed to the same place in the copy. 2) Then, until nothing changes, every link
    whose whole chain ends outside the copy (the user's files, elsewhere, another agent's
    copy) or nowhere is deleted. -> (deleted links, links that couldn't be deleted)."""
    for link in links:
        try:
            target = os.readlink(link)
        except OSError:
            continue
        if os.path.isabs(target):
            real = _real(target)
            for src, dest in maps:
                if _inside(real, src):
                    try:
                        _retarget(link, os.path.join(dest, os.path.relpath(real, _real(src))))
                    except OSError:
                        pass  # step 2 deletes it if it still leads out
                    break
    left, deleted, stuck = list(links), [], []
    changed = True
    while changed:
        changed = False
        for link in list(left):
            if not os.path.lexists(link):
                left.remove(link)
                continue
            if _inside(link, copy_root) and os.path.exists(link):
                continue
            left.remove(link)
            changed = True
            (deleted if _remove(link) else stuck).append(link)
    return deleted, stuck


def copy_ignored(src, dest, max_bytes=None, max_files=None):
    """Copy the git-ignored files and folders of the user's folder `src` into the copy's
    folder `dest` (real copies, so an agent can't change the user's files through them).
    Caches, build output and special files are skipped; whatever would go past the size limits
    is left out. Symlinks are copied as links and then contained (_contain_links).
    -> {"files", "bytes", "names": [copied], "skipped": [[name, bytes or None (= huge)]],
        "links": [deleted links that led out of the copy], "failed": [names not copied]}.
    Raises OSError if the copy can't be made safe (a link or half copy it can't delete): then
    the copy must not be used."""
    max_bytes = MAX_IGNORED_BYTES if max_bytes is None else max_bytes
    max_files = MAX_IGNORED_FILES if max_files is None else max_files
    out = {"files": 0, "bytes": 0, "names": [], "skipped": [], "links": [], "failed": []}
    try:
        raw = git_bytes(src, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                        "--directory")
    except GitError:
        return out
    entries = sorted(x.decode("utf-8", "surrogateescape").rstrip("/") for x in raw.split(b"\0") if x)
    maps, copy_root = _link_maps(src, dest)
    links, owner = [], {}
    for rel in entries:
        if any(part in IGNORED_SKIP for part in rel.split("/")):
            continue
        s = os.path.join(src, *rel.split("/"))
        d = os.path.join(dest, *rel.split("/"))
        if not os.path.lexists(s) or os.path.lexists(d) or _special(s):
            continue
        try:
            size, n = _size(s, max_bytes - out["bytes"], max_files - out["files"])
        except OSError:
            continue
        if not n:  # an empty folder (or only caches): nothing worth copying
            continue
        if size is None or out["bytes"] + size > max_bytes or out["files"] + n > max_files:
            out["skipped"].append([rel, size])  # size None: more than the limit
            continue
        try:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            if os.path.isdir(s) and not os.path.islink(s):
                shutil.copytree(s, d, symlinks=True, ignore=_skip)
            else:
                shutil.copy2(s, d, follow_symlinks=False)
        except (OSError, shutil.Error):
            if not _remove(d):  # never keep half a copy (its links were never checked)
                raise OSError(f"couldn't remove the half-copied {rel} from the copy")
            out["failed"].append(rel)
            continue
        found = _links_in(d)
        links += found
        owner.update({link: rel for link in found})
        out["files"] += n
        out["bytes"] += size
        out["names"].append(rel)
    deleted, stuck = _contain_links(links, maps, copy_root)
    for link in deleted:
        out["links"].append(os.path.relpath(link, dest).replace(os.sep, "/"))
        out["files"] -= 1
        if owner[link] == out["links"][-1] and owner[link] in out["names"]:
            out["names"].remove(owner[link])
    for rel in sorted({owner[link] for link in stuck}):  # a link we can't delete: drop it all
        if rel in out["names"]:
            out["names"].remove(rel)
        if not _remove(os.path.join(dest, *rel.split("/"))):
            raise OSError(f"couldn't remove {rel} from the copy")
        out["failed"].append(rel)
    return out


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
    """Write `data` (bytes, already in working-tree form) as `path` with git's file mode,
    keeping the existing file's permission bits (a 0600 file stays 0600)."""
    dest = os.path.join(root, *path.split("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isdir(dest) and not os.path.islink(dest):
        os.rmdir(dest)  # a folder the copy turned into a file (empty by now; else OSError)
    old = os.lstat(dest).st_mode if os.path.lexists(dest) and not os.path.islink(dest) else None
    if os.path.islink(dest) or (os.path.lexists(dest) and mode == "120000"):
        os.remove(dest)
    if mode == "120000":
        try:
            os.symlink(data.decode("utf-8", "surrogateescape"), dest)
            return
        except (OSError, NotImplementedError):
            pass  # (no symlinks, e.g. on Windows without the right): write it as a file
    tmp = dest + ".gc-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        if os.name != "nt":
            perm = stat.S_IMODE(old) if old is not None else stat.S_IMODE(os.stat(tmp).st_mode)
            ex = stat.S_IXUSR | (stat.S_IXGRP if perm & stat.S_IRGRP else 0) | (stat.S_IXOTH if perm & stat.S_IROTH else 0)
            os.chmod(tmp, perm | ex if mode == "100755" else perm & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


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
        dest = os.path.join(root, *p.split("/"))
        if p not in t_now and os.path.lexists(dest) and not os.path.isdir(dest):
            clash.append(p)  # a file git ignores in the user's folder (e.g. a local config)
            continue
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
    touched = []
    try:
        for p, entry in sorted(plan, key=lambda x: x[1] is not None):  # deletions first
            touched.append(p)
            if entry is None:
                dest = os.path.join(root, *p.split("/"))
                if os.path.lexists(dest):
                    os.remove(dest)
                _prune_empty(folder, os.path.dirname(dest))
            else:
                mode, sha = entry
                data = git_bytes(root, "cat-file", "blob", sha) if mode == "120000" \
                    else _checkout_bytes(root, p, sha)
                _write(root, p, mode, data)
    except (OSError, GitError) as e:  # put back what was already changed: all or nothing
        _rollback(root, touched, t_now)
        return False, files, f"error: {e}"
    return True, files, ""


def _prune_empty(stop, folder):
    """Remove folders left empty by deletions, up to (not including) `stop`, the chat's folder."""
    stop = os.path.realpath(stop)
    while os.path.realpath(folder).startswith(stop + os.sep):
        try:
            os.rmdir(folder)
        except OSError:
            return
        folder = os.path.dirname(folder)


def _rollback(root, paths, tree_now):
    """Put `paths` back as they were (from the snapshot taken just before)."""
    for p in reversed(paths):
        dest = os.path.join(root, *p.split("/"))
        try:
            if p in tree_now:
                mode, sha = tree_now[p]
                _write(root, p, mode, git_bytes(root, "cat-file", "blob", sha) if mode == "120000"
                       else _checkout_bytes(root, p, sha))
            elif os.path.lexists(dest) and not os.path.isdir(dest):
                os.remove(dest)  # (only paths that weren't there: ignored files are refused above)
        except (OSError, GitError):
            pass


def merge_copies(root, base, branches, path, branch, scope="."):
    """A new copy at `path` starting at `base` with every branch in `branches` merged in.
    -> files with conflicts (text ones keep conflict markers for an agent to fix; binary and
    edit-vs-delete ones have none, so unresolved() checks that the fixer changed them)."""
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


def unresolved(path, merged_at, conflicts):
    """Conflicted files a fixer didn't resolve: conflict markers still in them, or (binary /
    edit-vs-delete conflicts, which have no markers) not changed at all since the merge."""
    touched = {p for _, p in name_status(path, merged_at, "HEAD")}
    left = set(conflict_markers(path, conflicts))
    for f in conflicts:
        if f not in touched and (not os.path.exists(os.path.join(path, *f.split("/")))
                                 or not conflict_markers(path, [f])):
            left.add(f)
    return sorted(left)


# ---- the copies share the user's stash and branches (git worktrees): protect them
def refs_state(root):
    """(stash entries, {branch: sha}) of the user's repo, to restore after an agent's turn."""
    stash = git(root, "log", "-g", "--format=%H %gs", "refs/stash", check=False).splitlines()
    branches = {}
    for line in git(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").splitlines():
        name, sha = line.rsplit(" ", 1)
        if not name.startswith("refs/heads/agent/"):
            branches[name] = sha
    return stash, branches


def check_refs(root, before, base):
    """After an agent's turn in a copy (copies share the user's stash and branches):
    - stash entries made inside a copy are dropped: the user's next `git stash pop` must never
      bring an agent's unapproved work into their folder (a copy's stash has the snapshot `base`
      in its history; the user's own don't);
    - stash entries or branches that disappeared are NOT put back (it may have been the user),
      but reported with the command that restores them.
    -> lines for a note (empty if nothing happened)."""
    stash_before, branches_before = before
    stash_now, branches_now = refs_state(root)
    notes = []
    for i in reversed(range(len(stash_now))):  # drop from the end so the indexes stay valid
        sha, _, msg = stash_now[i].partition(" ")
        parent = git(root, "rev-parse", "-q", "--verify", f"{sha}^1", check=False).strip()
        if parent and run(root, "merge-base", "--is-ancestor", base, parent).returncode == 0:
            git(root, "stash", "drop", "-q", f"stash@{{{i}}}", check=False)
            notes.append(f"removed a stash entry an agent made in its copy ({msg or sha[:8]})")
    have = {l.split(" ", 1)[0] for l in stash_now}
    for line in stash_before:
        sha, _, msg = line.partition(" ")
        if sha not in have:
            notes.append(f"a stash entry disappeared ({msg or sha[:8]}); if that wasn't you: "
                         f"git stash store -m \"{msg}\" {sha}")
    for name, sha in branches_before.items():
        if name not in branches_now:
            short = name[len("refs/heads/"):]
            notes.append(f"branch {short} disappeared; if that wasn't you: git branch {short} {sha}")
    return notes


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
