"""Backtrack engine: checkpoints of a working tree in a shadow git store.

The shadow store lives outside the project (in the plugin data directory), so the
project's own .git is never touched. Only stdlib plus the `git` binary are used.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

MB = 1024 * 1024
DEFAULT_EXCLUDE_DIRS = (
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".next", ".cache", ".tox",
)

_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class GitError(RuntimeError):
    pass


class RestoreRefused(RuntimeError):
    pass


@dataclass
class Scan:
    ok: bool = True
    reason: Optional[str] = None
    big: List[str] = field(default_factory=list)      # files over the per-file cap
    nested: List[str] = field(default_factory=list)   # nested git repos (left alone)
    files: int = 0
    bytes: int = 0


@dataclass
class Snap:
    sha: Optional[str]
    tree: Optional[str]
    reused: bool
    skipped: Optional[str]
    scan: Scan


def _escape(pattern: str) -> str:
    out = []
    for i, ch in enumerate(pattern):
        if ch in "\\*?[]" or (i == 0 and ch in "!#"):
            out.append("\\")
        out.append(ch)
    s = "".join(out)
    if s.endswith(" "):
        s = s[:-1] + "\\ "
    return s


class Store:
    def __init__(self, root, data_dir, *, max_file_bytes: int = 5 * MB,
                 max_total_bytes: int = 500 * MB, max_files: int = 50000,
                 exclude_dirs: Sequence[str] = DEFAULT_EXCLUDE_DIRS):
        self.root = Path(root).resolve()
        self.data_dir = Path(data_dir)
        key = hashlib.sha1(str(self.root).encode()).hexdigest()[:12]
        self.dir = self.data_dir / "stores" / key
        self.git_dir = self.dir / "shadow.git"
        self.log_path = self.dir / "actions.jsonl"
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_files = max_files
        self.exclude_dirs = tuple(exclude_dirs)
        self._lock = _lock_for(str(self.git_dir))
        self._diff_cache: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}

    # ---- git plumbing -------------------------------------------------
    def _env(self) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_DIR=str(self.git_dir), GIT_WORK_TREE=str(self.root),
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="backtrack", GIT_AUTHOR_EMAIL="backtrack@localhost",
            GIT_COMMITTER_NAME="backtrack", GIT_COMMITTER_EMAIL="backtrack@localhost",
            GIT_TERMINAL_PROMPT="0", LC_ALL="C",
        )
        return env

    def git(self, *args: str, input: Optional[bytes] = None,
            ok_codes: Tuple[int, ...] = (0,)) -> bytes:
        cmd = ["git", "-c", "core.quotepath=false", "-c", "core.autocrlf=false",
               "-c", "core.safecrlf=false", "-c", "gc.auto=0", *args]
        try:
            p = subprocess.run(cmd, env=self._env(), input=input, capture_output=True,
                               cwd=str(self.root), timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitError(f"git {args[0]} failed to run: {exc}") from exc
        if p.returncode not in ok_codes:
            raise GitError(f"git {args[0]} exited {p.returncode}: "
                           f"{p.stderr.decode(errors='replace').strip()[:300]}")
        return p.stdout

    def _ensure(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        p = subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(self.git_dir)],
                           env=env, capture_output=True, timeout=60)
        if p.returncode != 0:
            raise GitError(f"git init failed: {p.stderr.decode(errors='replace')[:300]}")
        (self.dir / "root.txt").write_text(str(self.root))

    def _head(self) -> Optional[str]:
        out = self.git("rev-parse", "-q", "--verify", "refs/heads/main", ok_codes=(0, 1))
        return out.decode().strip() or None

    def tree_of(self, sha: str) -> str:
        return self.git("rev-parse", f"{sha}^{{tree}}").decode().strip()

    # ---- scanning -------------------------------------------------------
    def _scan(self) -> Scan:
        sc = Scan()
        excl = set(self.exclude_dirs)
        stack = [self.root]
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name in excl:
                                continue
                            if os.path.exists(os.path.join(e.path, ".git")):
                                sc.nested.append(os.path.relpath(e.path, self.root))
                                continue
                            stack.append(Path(e.path))
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    sc.files += 1
                    rel = os.path.relpath(e.path, self.root)
                    if st.st_size > self.max_file_bytes and not e.is_symlink():
                        sc.big.append(rel)
                        continue
                    sc.bytes += st.st_size
                    if sc.files > self.max_files:
                        sc.ok, sc.reason = False, f"more than {self.max_files} files in {self.root}"
                        return sc
                    if sc.bytes > self.max_total_bytes:
                        sc.ok = False
                        sc.reason = f"tracked files exceed {self.max_total_bytes // MB} MB in {self.root}"
                        return sc
        return sc

    def _write_excludes(self, sc: Scan) -> None:
        lines = [f"{name}/" for name in self.exclude_dirs]
        lines += ["/" + _escape(p.replace(os.sep, "/")) + "/" for p in sc.nested]
        lines += ["/" + _escape(p.replace(os.sep, "/")) for p in sc.big]
        info = self.git_dir / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text("\n".join(lines) + "\n")

    def _stage(self) -> Tuple[Optional[str], Scan, Optional[str]]:
        """Stage the live tree into the shadow index. Returns (tree, scan, skip_reason)."""
        self._ensure()
        sc = self._scan()
        if not sc.ok:
            return None, sc, sc.reason
        self._write_excludes(sc)
        try:
            untrack = sc.big + sc.nested
            if untrack:
                self.git("rm", "--cached", "-r", "-q", "--ignore-unmatch",
                         "--pathspec-from-file=-", "--pathspec-file-nul",
                         input=b"\0".join(p.encode() for p in untrack))
            self.git("add", "-A")
            tree = self.git("write-tree").decode().strip()
        except GitError as exc:
            return None, sc, f"snapshot failed: {exc}"
        return tree, sc, None

    # ---- snapshots ------------------------------------------------------
    def snapshot(self, label: str = "checkpoint") -> Snap:
        with self._lock:
            tree, sc, skip = self._stage()
            if skip:
                return Snap(None, None, False, skip, sc)
            head = self._head()
            if head and self.tree_of(head) == tree:
                return Snap(head, tree, True, None, sc)
            args = ["commit-tree", tree, "-m", label[:200]]
            if head:
                args += ["-p", head]
            sha = self.git(*args).decode().strip()
            self.git("update-ref", "refs/heads/main", sha)
            return Snap(sha, tree, False, None, sc)

    def current_tree(self) -> Tuple[Optional[str], Optional[str]]:
        with self._lock:
            tree, _sc, skip = self._stage()
            return tree, skip

    # ---- diffs ----------------------------------------------------------
    def changed_paths(self, tree_a: str, tree_b: str) -> List[Tuple[str, str]]:
        key = (tree_a, tree_b)
        if key in self._diff_cache:
            return self._diff_cache[key]
        out = self.git("diff-tree", "-r", "--name-status", "-z", "--no-renames", tree_a, tree_b)
        parts = out.decode(errors="replace").split("\0")
        res = [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2) if parts[i]]
        self._diff_cache[key] = res
        return res

    def diff_text(self, tree_a: str, tree_b: str, max_chars: int = 6000) -> str:
        out = self.git("diff", "--no-color", "--no-ext-diff", "--patch-with-stat",
                       tree_a, tree_b).decode(errors="replace")
        if len(out) > max_chars:
            out = out[:max_chars] + "\n... (diff truncated)"
        return out

    # ---- restore --------------------------------------------------------
    def restore(self, sha: str) -> dict:
        """Make the tracked working tree identical to checkpoint `sha`."""
        with self._lock:
            pre = self.snapshot("pre-restore")
            if pre.sha is None:
                raise RestoreRefused(f"cannot restore safely, no pre-restore checkpoint: {pre.skipped}")
            target_tree = self.tree_of(sha)
            changes = self.changed_paths(pre.tree, target_tree)
            self.git("read-tree", target_tree)
            removed: List[str] = []
            written: List[str] = []
            for status, path in changes:
                if status in ("D", "T"):
                    p = self.root / path
                    try:
                        os.remove(p)
                        removed.append(path)
                    except FileNotFoundError:
                        pass
                    parent = p.parent
                    while parent != self.root:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
            to_write = [path for status, path in changes if status in ("A", "M", "T")]
            if to_write:
                self.git("checkout-index", "-f", "-z", "--stdin",
                         input=b"\0".join(p.encode() for p in to_write))
                written = to_write
            tree_now, _sc, skip = self._stage()
            verified = tree_now == target_tree
            return {"target": sha, "tree": target_tree, "pre_restore": pre.sha,
                    "removed": removed, "written": written, "verified": verified,
                    "verify_note": skip or (None if verified else
                                            "live tree differs from checkpoint (files over the size cap or excluded)")}

    # ---- action log -----------------------------------------------------
    def _append(self, rec: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def _records(self) -> List[dict]:
        if not self.log_path.exists():
            return []
        recs: Dict[int, dict] = {}
        order: List[int] = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rid = r.get("id")
            if r.get("type") == "after":
                base = recs.get(rid)
                if base is not None:
                    base.update({k: r[k] for k in ("after", "status") if k in r})
                    if r.get("skipped"):
                        base["after_skipped"] = r["skipped"]
                continue
            recs[rid] = dict(r)
            order.append(rid)
        out = [recs[i] for i in order]
        reverted: Dict[int, int] = {}
        for r in out:
            if r["type"] == "restore":
                for i in r.get("reverts", []):
                    reverted[i] = r["id"]
                for i in r.get("unreverts", []):
                    reverted.pop(i, None)
        for r in out:
            r["reverted_by"] = reverted.get(r["id"])
        return out

    def _next_id(self) -> int:
        recs = self._records()
        return (recs[-1]["id"] + 1) if recs else 1

    def begin_action(self, tool: str, summary: str, session_id=None, task_id=None, call_id=None) -> int:
        with self._lock:
            snap = self.snapshot(f"before {tool}: {summary}")
            rid = self._next_id()
            self._append({"type": "action", "id": rid, "ts": time.time(), "tool": tool,
                          "summary": summary[:300], "session": session_id, "task": task_id,
                          "call": call_id, "before": snap.sha, "skipped": snap.skipped,
                          "big": snap.scan.big[:20]})
            return rid

    def end_action(self, rid: int, status: str = "ok") -> None:
        with self._lock:
            snap = self.snapshot(f"after #{rid}")
            self._append({"type": "after", "id": rid, "after": snap.sha, "status": status,
                          "skipped": snap.skipped})

    def get(self, rid: int) -> Optional[dict]:
        for r in self._records():
            if r["id"] == rid:
                return r
        return None

    def timeline(self, limit: int = 50, include_noop: bool = False) -> List[dict]:
        rows = []
        for r in self._records():
            row = dict(r)
            row["files"] = []
            row["noop"] = False
            if r.get("before") and r.get("after"):
                ta, tb = self.tree_of(r["before"]), self.tree_of(r["after"])
                row["noop"] = ta == tb
                if ta != tb:
                    row["files"] = [{"status": s, "path": p} for s, p in self.changed_paths(ta, tb)]
            row["undoable"] = bool(r.get("before")) and r["type"] == "action" and not r.get("reverted_by")
            if row["noop"] and not include_noop:
                continue
            rows.append(row)
        return rows[-limit:]

    def last_undoable(self) -> Optional[dict]:
        for r in reversed(self._records()):
            if r["type"] == "action" and r.get("before") and not r.get("reverted_by"):
                return r
        return None

    def preview(self, rec: dict, max_chars: int = 6000) -> dict:
        """What restoring to the state before `rec` would change right now."""
        cur, skip = self.current_tree()
        if cur is None:
            raise RestoreRefused(skip or "cannot read the working tree")
        target = self.tree_of(rec["before"])
        changes = self.changed_paths(cur, target)
        reverts = [r["id"] for r in self._records()
                   if r["type"] == "action" and r["id"] >= rec["id"] and not r.get("reverted_by")]
        return {"target_id": rec["id"], "changes": [{"status": s, "path": p} for s, p in changes],
                "diff": self.diff_text(cur, target, max_chars) if changes else "",
                "reverts": reverts}

    def undo_to(self, rec: dict) -> dict:
        """Restore the tree to the state before `rec` and log it (so it can be redone)."""
        if not rec.get("before"):
            raise RestoreRefused(f"action #{rec['id']} has no checkpoint"
                                 + (f": {rec['skipped']}" if rec.get("skipped") else ""))
        with self._lock:
            reverts = [r["id"] for r in self._records()
                       if r["type"] == "action" and r["id"] >= rec["id"] and not r.get("reverted_by")]
            res = self.restore(rec["before"])
            rid = self._next_id()
            self._append({"type": "restore", "id": rid, "ts": time.time(), "tool": "backtrack",
                          "summary": f"undo to before #{rec['id']}", "before": res["pre_restore"],
                          "reverts": reverts, "target_id": rec["id"]})
            self._append({"type": "after", "id": rid, "after": res["target"],
                          "status": "verified" if res["verified"] else "unverified"})
            res["restore_id"] = rid
            res["reverted"] = reverts
            return res

    def redo(self) -> dict:
        for r in reversed(self._records()):
            if r["type"] == "restore" and r.get("reverts") and not r.get("redone"):
                with self._lock:
                    res = self.restore(r["before"])
                    rid = self._next_id()
                    self._append({"type": "restore", "id": rid, "ts": time.time(), "tool": "backtrack",
                                  "summary": f"redo of undo #{r['id']}", "before": res["pre_restore"],
                                  "unreverts": r["reverts"], "redo_of": r["id"]})
                    self._append({"type": "after", "id": rid, "after": res["target"],
                                  "status": "verified" if res["verified"] else "unverified"})
                    res["restore_id"] = rid
                    return res
            if r["type"] == "restore" and r.get("redo_of"):
                break
        raise RestoreRefused("nothing to redo")


# ---- registry of tracked roots (so a separate API process can find stores) --------
def register_root(data_dir, root) -> None:
    path = Path(data_dir) / "roots.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        data = {}
    data[str(Path(root).resolve())] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def known_roots(data_dir) -> List[str]:
    path = Path(data_dir) / "roots.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [r for r, _ in sorted(data.items(), key=lambda kv: kv[1], reverse=True)]


READ_ONLY_FIRST_WORDS = {
    "ls", "cat", "head", "tail", "pwd", "whoami", "date", "which", "wc", "grep", "rg",
    "tree", "stat", "file", "du", "df", "env", "uname", "id", "hostname", "less", "more",
}
READ_ONLY_GIT = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files"}
MUTATING_HINTS = ("write", "patch", "edit", "delete", "remove", "move", "rename", "create",
                  "mkdir", "replace", "apply", "save", "terminal", "shell", "bash", "exec",
                  "run_command")
SHELL_METACHARS = set(">;&|`$(\n<")


def is_mutating_tool(name: str, args: Optional[dict], extra: Sequence[str] = (),
                     ignore: Sequence[str] = ()) -> bool:
    n = (name or "").lower()
    if n in {i.lower() for i in ignore} or n.startswith("backtrack"):
        return False
    if n in {e.lower() for e in extra}:
        return True
    if n.startswith(("read", "search", "list", "get", "web", "browser", "memory", "recall")):
        return False
    if not any(h in n for h in MUTATING_HINTS):
        return False
    if any(h in n for h in ("terminal", "shell", "bash", "exec", "run_command")):
        cmd = ""
        if isinstance(args, dict):
            cmd = str(args.get("command") or args.get("cmd") or args.get("code") or "")
        if cmd and not (set(cmd) & SHELL_METACHARS):
            words = cmd.split()
            if words and (words[0] in READ_ONLY_FIRST_WORDS or
                          (words[0] == "git" and len(words) > 1 and words[1] in READ_ONLY_GIT)):
                return False
    return True


def summarize(name: str, args: Optional[dict]) -> str:
    if not isinstance(args, dict):
        return name
    for key in ("command", "cmd", "path", "file_path", "filepath", "filename", "file", "code"):
        if args.get(key):
            return " ".join(str(args[key]).split())[:120]
    return json.dumps(args, default=str)[:120]
