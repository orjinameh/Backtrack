"""Runtime glue: settings, hooks, slash commands, tool handler and API helpers.

Everything here is defensive: a hook must never raise into the agent loop.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Dict, Optional

from . import engine

log = logging.getLogger("backtrack")

DEFAULTS = {
    "enabled": True,
    "workspace": "",
    "max_file_mb": 5,
    "max_total_mb": 500,
    "max_files": 50000,
    "extra_tools": [],
    "ignore_tools": [],
    "exclude_dirs": list(engine.DEFAULT_EXCLUDE_DIRS),
}
CONFIRM_WORDS = {"--yes", "-y", "yes", "apply", "confirm", "now"}


def default_data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # provided by Hermes
        return Path(plugin_data_dir("backtrack"))
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
        return Path(home) / "plugin-data" / "backtrack"


def resolve_root(workspace: str = "") -> Path:
    for candidate in (workspace, os.environ.get("TERMINAL_CWD", ""), os.getcwd()):
        if candidate and Path(candidate).is_dir():
            return Path(candidate).resolve()
    return Path.cwd()


class Runtime:
    def __init__(self, get_setting: Optional[Callable[[str, object], object]] = None,
                 data_dir: Optional[Path] = None):
        self._get = get_setting or (lambda key, default: default)
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self._pending: Dict[object, deque] = defaultdict(deque)
        self.last_root: Optional[Path] = None

    # ---- settings / stores -------------------------------------------------
    def setting(self, key: str):
        try:
            return self._get(key, DEFAULTS[key])
        except Exception:
            return DEFAULTS[key]

    def store(self, root: Optional[Path] = None) -> engine.Store:
        root = root or self.last_root or resolve_root(str(self.setting("workspace") or ""))
        return engine.Store(
            root, self.data_dir,
            max_file_bytes=int(float(self.setting("max_file_mb")) * engine.MB),
            max_total_bytes=int(float(self.setting("max_total_mb")) * engine.MB),
            max_files=int(self.setting("max_files")),
            exclude_dirs=tuple(self.setting("exclude_dirs") or engine.DEFAULT_EXCLUDE_DIRS),
        )

    # ---- hooks ----------------------------------------------------------------
    def pre_tool_call(self, tool_name=None, args=None, task_id=None, **kwargs):
        try:
            if not self.setting("enabled"):
                return None
            if not engine.is_mutating_tool(tool_name, args, self.setting("extra_tools"),
                                           self.setting("ignore_tools")):
                return None
            root = resolve_root(str(self.setting("workspace") or ""))
            self.last_root = root
            engine.register_root(self.data_dir, root)
            st = self.store(root)
            call_id = kwargs.get("tool_call_id")
            rid = st.begin_action(tool_name, engine.summarize(tool_name, args),
                                  session_id=kwargs.get("session_id"), task_id=task_id, call_id=call_id)
            self._pending[call_id or (task_id, tool_name)].append((rid, root))
        except Exception as exc:  # never break the agent loop
            log.warning("backtrack pre_tool_call failed: %s", exc)
        return None  # Backtrack never blocks or modifies a tool call

    def post_tool_call(self, tool_name=None, args=None, result=None, task_id=None, **kwargs):
        try:
            key = kwargs.get("tool_call_id") or (task_id, tool_name)
            q = self._pending.get(key)
            if not q:
                return None
            rid, root = q.popleft()
            if not q:
                self._pending.pop(key, None)
            status = "ok"
            if kwargs.get("status") in ("error", "failed") or kwargs.get("error_type"):
                status = "error"
            self.store(root).end_action(rid, status)
        except Exception as exc:
            log.warning("backtrack post_tool_call failed: %s", exc)
        return None

    # ---- text formatting ------------------------------------------------------------
    @staticmethod
    def _fmt_changes(changes, limit: int = 12) -> str:
        sym = {"A": "restore", "M": "revert", "D": "remove", "T": "retype"}
        lines = [f"  {sym.get(c['status'], c['status'])}: {c['path']}" for c in changes[:limit]]
        if len(changes) > limit:
            lines.append(f"  ... and {len(changes) - limit} more")
        return "\n".join(lines)

    def cmd_undo(self, raw_args: str = "") -> str:
        try:
            words = (raw_args or "").split()
            confirm = any(w in CONFIRM_WORDS for w in words)
            nums = [w for w in words if w.isdigit()]
            st = self.store()
            rec = st.get(int(nums[0])) if nums else st.last_undoable()
            if rec is None or rec.get("type") != "action":
                return "Backtrack: nothing to undo yet." if not nums else f"Backtrack: no action #{nums[0]}."
            if not rec.get("before"):
                return f"Backtrack: action #{rec['id']} has no checkpoint ({rec.get('skipped') or 'unknown reason'})."
            pv = st.preview(rec)
            if not pv["changes"]:
                return f"Backtrack: files already match the state before #{rec['id']}. Nothing to restore."
            head = (f"Undo to before #{rec['id']} ({rec['tool']}: {rec['summary']}) "
                    f"- reverts actions {pv['reverts']}")
            if not confirm:
                return (f"{head}\n{self._fmt_changes(pv['changes'])}\n\n{pv['diff']}\n\n"
                        f"Nothing changed yet. Run `/backtrack undo{' ' + nums[0] if nums else ''} --yes` to apply.")
            res = st.undo_to(rec)
            ok = "verified: tree hash matches the checkpoint" if res["verified"] else \
                f"NOT verified ({res['verify_note']})"
            return (f"Restored to before #{rec['id']}: {len(res['written'])} written, "
                    f"{len(res['removed'])} removed - {ok} ({res['tree'][:12]}).\n"
                    f"Changed your mind? `/backtrack redo`.")
        except engine.RestoreRefused as exc:
            return f"Backtrack refused: {exc}"
        except Exception as exc:
            log.warning("backtrack /undo failed: %s", exc)
            return f"Backtrack error: {exc}"

    def cmd_backtrack(self, raw_args: str = "") -> str:
        try:
            words = (raw_args or "").split()
            sub = words[0] if words else "list"
            st = self.store()
            if sub in ("undo", "restore"):  # same as /undo; needed if Hermes owns the /undo name
                return self.cmd_undo(" ".join(words[1:]))
            if sub == "redo":
                res = st.redo()
                return (f"Redone: {len(res['written'])} written, {len(res['removed'])} removed - "
                        f"{'verified' if res['verified'] else 'NOT verified'}.")
            if sub == "diff":
                if len(words) < 2 or not words[1].isdigit():
                    return "Usage: /backtrack diff <action id>"
                rec = st.get(int(words[1]))
                if not rec or not rec.get("before"):
                    return f"Backtrack: no checkpoint for action {words[1]}."
                pv = st.preview(rec)
                return (self._fmt_changes(pv["changes"]) + "\n\n" + pv["diff"]) if pv["changes"] else \
                    "Files already match that checkpoint."
            if sub in ("list", "all"):
                rows = st.timeline(limit=15, include_noop=(sub == "all"))
                if not rows:
                    return f"Backtrack: no file-changing actions recorded for {st.root}."
                out = [f"Backtrack timeline for {st.root}"]
                for r in rows:
                    mark = "reverted" if r.get("reverted_by") else (r.get("status") or "")
                    out.append(f"#{r['id']} [{r['tool']}] {r['summary']} - {len(r['files'])} file(s) {mark}".rstrip())
                return "\n".join(out)
            return "Usage: /backtrack [list|all|diff <id>|undo [id] [--yes]|redo]"
        except engine.RestoreRefused as exc:
            return f"Backtrack refused: {exc}"
        except Exception as exc:
            log.warning("backtrack command failed: %s", exc)
            return f"Backtrack error: {exc}"

    # ---- agent tool ----------------------------------------------------------------------
    def tool_timeline(self, args: dict = None, **kwargs) -> str:
        try:
            limit = int((args or {}).get("limit", 10))
            st = self.store()
            rows = st.timeline(limit=max(1, min(limit, 50)))
            slim = [{"id": r["id"], "tool": r["tool"], "summary": r["summary"], "status": r.get("status"),
                     "files": r["files"][:20], "reverted": bool(r.get("reverted_by"))} for r in rows]
            return json.dumps({"root": str(st.root), "actions": slim})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ---- API helpers (used by plugin_api.py) ----------------------------------------------
    def _store_for_api(self, root: Optional[str]) -> engine.Store:
        roots = engine.known_roots(self.data_dir)
        if root:
            if str(Path(root).resolve()) not in roots:
                raise engine.RestoreRefused("unknown workspace")
            return self.store(Path(root))
        if not roots:
            raise LookupError("no workspace has been tracked yet")
        return self.store(Path(roots[0]))

    def api_timeline(self, root: Optional[str] = None, limit: int = 50) -> dict:
        try:
            st = self._store_for_api(root)
        except LookupError as exc:
            return {"ok": True, "empty": True, "message": str(exc), "roots": [], "actions": []}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        try:
            return {"ok": True, "root": str(st.root), "roots": engine.known_roots(self.data_dir),
                    "actions": st.timeline(limit=limit)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def api_diff(self, rid: int, root: Optional[str] = None) -> dict:
        try:
            st = self._store_for_api(root)
            rec = st.get(rid)
            if not rec or not rec.get("before"):
                return {"ok": False, "error": f"no checkpoint for action {rid}"}
            pv = st.preview(rec)
            return {"ok": True, **pv}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def api_restore(self, rid: int, root: Optional[str] = None) -> dict:
        try:
            st = self._store_for_api(root)
            rec = st.get(rid)
            if not rec or rec.get("type") != "action":
                return {"ok": False, "error": f"no action {rid}"}
            res = st.undo_to(rec)
            return {"ok": True, "verified": res["verified"], "tree": res["tree"],
                    "written": len(res["written"]), "removed": len(res["removed"]),
                    "verify_note": res["verify_note"]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def api_redo(self, root: Optional[str] = None) -> dict:
        try:
            res = self._store_for_api(root).redo()
            return {"ok": True, "verified": res["verified"]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
