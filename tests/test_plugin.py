"""Plugin wiring tests. `FakeCtx` is a recording stand-in for Hermes's registration context only;
all files, git stores and restores underneath are real."""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import load_plugin  # noqa: E402


class FakeCtx:
    def __init__(self, config=None):
        self.hooks, self.commands, self.tools, self.cli = {}, {}, {}, None
        self.config = config or {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_command(self, name, handler, description=""):
        self.commands[name] = handler

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (schema, handler)

    def register_cli_command(self, **kw):
        self.cli = kw


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "work"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "notes.md").write_text("notes\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_CWD", str(root))
    pkg = load_plugin()
    ctx = FakeCtx()
    pkg.register(ctx)
    return pkg, ctx, root


def digest(root: Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def run_tool(ctx, name, args, call_id, action):
    """pre hook -> real filesystem change -> post hook, as the agent loop would."""
    ctx.hooks["pre_tool_call"](tool_name=name, args=args, task_id="t1", tool_call_id=call_id)
    action()
    ctx.hooks["post_tool_call"](tool_name=name, args=args, result="{}", task_id="t1",
                                duration_ms=1, tool_call_id=call_id)


def test_registers_everything(env):
    _, ctx, _ = env
    assert set(ctx.hooks) == {"pre_tool_call", "post_tool_call"}
    assert set(ctx.commands) == {"undo", "backtrack"}
    assert "backtrack_timeline" in ctx.tools
    assert ctx.cli["name"] == "backtrack"


def test_money_shot_cleanup_then_undo(env):
    _, ctx, root = env
    before = digest(root)

    def clean():
        (root / "src" / "app.py").unlink()
        (root / "src").rmdir()
        (root / "notes.md").write_text("overwritten")

    run_tool(ctx, "terminal", {"command": "rm -rf src && echo x > notes.md"}, "c1", clean)
    assert digest(root) != before

    preview = ctx.commands["backtrack"]("undo")
    assert "Nothing changed yet" in preview and "src/app.py" in preview
    assert "/backtrack undo --yes" in preview
    assert digest(root) != before, "preview must not touch files"

    done = ctx.commands["backtrack"]("undo --yes")
    assert "verified: tree hash matches" in done
    assert digest(root) == before

    listing = ctx.commands["backtrack"]("list")
    assert "terminal" in listing and "reverted" in listing

    assert "Redone" in ctx.commands["backtrack"]("redo")
    assert not (root / "src").exists()


def test_read_only_calls_leave_no_trace(env):
    _, ctx, root = env
    run_tool(ctx, "read_file", {"path": "notes.md"}, "c1", lambda: None)
    run_tool(ctx, "terminal", {"command": "ls -la"}, "c2", lambda: None)
    assert "no file-changing actions" in ctx.commands["backtrack"]("list")
    assert ctx.commands["undo"]("") == "Backtrack: nothing to undo yet."


def test_hooks_never_raise_and_never_block(env, monkeypatch):
    _, ctx, root = env
    monkeypatch.setenv("TERMINAL_CWD", str(root / "does-not-exist"))
    assert ctx.hooks["pre_tool_call"](tool_name="write_file", args={"path": "a"}, task_id="t") is None
    assert ctx.hooks["post_tool_call"](tool_name="write_file", args={}, result="{}", task_id="t") is None
    assert ctx.hooks["pre_tool_call"](tool_name=None, args=None) is None


def test_agent_tool_reports_real_timeline(env):
    _, ctx, root = env
    run_tool(ctx, "write_file", {"path": "src/app.py"}, "c1",
             lambda: (root / "src" / "app.py").write_text("changed"))
    out = json.loads(ctx.tools["backtrack_timeline"][1]({"limit": 5}))
    assert out["actions"][0]["files"] == [{"status": "M", "path": "src/app.py"}]


def test_api_helpers_roundtrip_and_empty_state(env):
    pkg, ctx, root = env
    rt = pkg.runtime.Runtime()  # fresh runtime, as the separate API process would have
    empty = rt.api_timeline()
    assert empty["ok"] and empty["empty"] and empty["actions"] == []

    before = digest(root)
    run_tool(ctx, "write_file", {"path": "notes.md"}, "c1", lambda: (root / "notes.md").write_text("x"))
    tl = rt.api_timeline()
    assert tl["ok"] and tl["root"] == str(root.resolve()) and len(tl["actions"]) == 1
    aid = tl["actions"][0]["id"]
    assert rt.api_diff(aid)["changes"] == [{"status": "M", "path": "notes.md"}]
    res = rt.api_restore(aid)
    assert res["ok"] and res["verified"]
    assert digest(root) == before
    assert rt.api_restore(aid, root="/etc")["ok"] is False   # unknown workspace is refused


def test_settings_can_disable_tracking(tmp_path, monkeypatch):
    root = tmp_path / "w"
    root.mkdir()
    (root / "f").write_text("1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_CWD", str(root))
    pkg = load_plugin()
    ctx = FakeCtx({"enabled": False})
    pkg.register(ctx)
    run_tool(ctx, "write_file", {"path": "f"}, "c1", lambda: (root / "f").write_text("2"))
    assert "no file-changing actions" in ctx.commands["backtrack"]("list")
