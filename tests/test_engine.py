"""Engine tests: real git, real files in a temp directory. No mocks."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine  # noqa: E402
from engine import Store, RestoreRefused  # noqa: E402


def digest_tree(root: Path, skip=("node_modules", ".git")):
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in skip]
        for fn in fns:
            p = Path(dp) / fn
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('a')\n")
    (root / "src" / "deep" / "b.txt").write_bytes(b"\x00\x01binary\xff")
    (root / "README.md").write_text("# hello\n")
    return root


@pytest.fixture
def store(proj, tmp_path):
    return Store(proj, tmp_path / "data")


def test_snapshot_reused_when_unchanged(store):
    a = store.snapshot("one")
    b = store.snapshot("two")
    assert a.sha and a.sha == b.sha and b.reused


def test_undo_delete_modify_create_is_byte_identical(store, proj):
    before = digest_tree(proj)
    rid = store.begin_action("terminal", "rm -rf src")
    (proj / "src" / "a.py").write_text("changed\n")
    (proj / "src" / "deep" / "b.txt").unlink()
    (proj / "new" / "x").mkdir(parents=True)
    (proj / "new" / "x" / "created.txt").write_text("new")
    (proj / "README.md").unlink()
    store.end_action(rid)

    assert digest_tree(proj) != before
    res = store.undo_to(store.get(rid))
    assert res["verified"] is True
    assert digest_tree(proj) == before
    assert not (proj / "new").exists(), "empty created dirs are pruned"


def test_timeline_lists_files_and_hides_noops(store, proj):
    r1 = store.begin_action("terminal", "ls")
    store.end_action(r1)  # nothing changed
    r2 = store.begin_action("write_file", "src/a.py")
    (proj / "src" / "a.py").write_text("x")
    store.end_action(r2)
    rows = store.timeline()
    assert [r["id"] for r in rows] == [r2]
    assert rows[0]["files"] == [{"status": "M", "path": "src/a.py"}]
    assert [r["id"] for r in store.timeline(include_noop=True)] == [r1, r2]


def test_undo_stack_and_redo(store, proj):
    ids = []
    for name in ("one", "two"):
        rid = store.begin_action("write_file", name)
        (proj / f"{name}.txt").write_text(name)
        store.end_action(rid)
        ids.append(rid)
    assert store.last_undoable()["id"] == ids[1]
    store.undo_to(store.last_undoable())
    assert (proj / "one.txt").exists() and not (proj / "two.txt").exists()
    assert store.last_undoable()["id"] == ids[0]       # next undo pops the earlier action
    store.undo_to(store.last_undoable())
    assert not (proj / "one.txt").exists()
    store.redo()                                        # redo the last undo
    assert (proj / "one.txt").exists() and not (proj / "two.txt").exists()
    assert store.last_undoable()["id"] == ids[0]


def test_preview_matches_what_restore_does(store, proj):
    rid = store.begin_action("terminal", "rm README.md")
    (proj / "README.md").unlink()
    store.end_action(rid)
    pv = store.preview(store.get(rid))
    assert pv["changes"] == [{"status": "A", "path": "README.md"}]
    assert "README.md" in pv["diff"]
    assert not (proj / "README.md").exists(), "preview must not change files"


def test_excluded_dirs_are_never_touched(store, proj):
    nm = proj / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "i.js").write_text("v1")
    rid = store.begin_action("terminal", "x")
    (nm / "i.js").write_text("v2")
    (proj / "README.md").write_text("changed")
    store.end_action(rid)
    store.undo_to(store.get(rid))
    assert (nm / "i.js").read_text() == "v2"
    assert (proj / "README.md").read_text() == "# hello\n"


def test_big_files_are_left_alone_and_reported(proj, tmp_path):
    st = Store(proj, tmp_path / "data", max_file_bytes=1000)
    (proj / "big.bin").write_bytes(b"a" * 5000)
    rid = st.begin_action("terminal", "x")
    (proj / "big.bin").write_bytes(b"b" * 5000)
    (proj / "README.md").write_text("changed")
    st.end_action(rid)
    assert st.get(rid)["big"] == ["big.bin"]
    res = st.undo_to(st.get(rid))
    assert (proj / "big.bin").read_bytes() == b"b" * 5000   # untouched, not tracked
    assert (proj / "README.md").read_text() == "# hello\n"
    assert res["verified"] is True


def test_over_total_cap_skips_and_refuses_restore(proj, tmp_path):
    st = Store(proj, tmp_path / "data", max_total_bytes=10)
    rid = st.begin_action("terminal", "x")
    rec = st.get(rid)
    assert rec["before"] is None and "exceed" in rec["skipped"]
    with pytest.raises(RestoreRefused):
        st.undo_to(rec)


def test_projects_own_git_repo_is_untouched(store, proj):
    run = lambda *a: subprocess.run(["git", *a], cwd=proj, capture_output=True, text=True, check=True).stdout
    run("init", "-q", "-b", "main")
    run("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    log_before, status_before = run("log", "--oneline"), run("status", "--porcelain")
    rid = store.begin_action("terminal", "rm")
    (proj / "README.md").unlink()
    store.end_action(rid)
    store.undo_to(store.get(rid))
    assert run("log", "--oneline") == log_before
    assert run("status", "--porcelain") == status_before
    assert (proj / "README.md").exists()


def test_nested_git_repo_is_left_alone(store, proj):
    nested = proj / "vendor" / "lib"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
    (nested / "f.txt").write_text("v1")
    rid = store.begin_action("terminal", "x")
    (nested / "f.txt").write_text("v2")
    store.end_action(rid)
    store.undo_to(store.get(rid))
    assert (nested / "f.txt").read_text() == "v2"


def test_filenames_with_odd_characters(store, proj):
    odd = proj / "we ird #1 [x]*.txt"
    odd.write_text("keep")
    rid = store.begin_action("terminal", "x")
    odd.unlink()
    store.end_action(rid)
    store.undo_to(store.get(rid))
    assert odd.read_text() == "keep"


def test_symlink_and_exec_bit_roundtrip(store, proj):
    script = proj / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    (proj / "link").symlink_to("README.md")
    rid = store.begin_action("terminal", "x")
    script.unlink()
    (proj / "link").unlink()
    store.end_action(rid)
    store.undo_to(store.get(rid))
    assert os.access(script, os.X_OK)
    assert os.readlink(proj / "link") == "README.md"


@pytest.mark.parametrize("name,args,expected", [
    ("write_file", {"path": "a"}, True),
    ("patch", {}, True),
    ("terminal", {"command": "rm -rf build"}, True),
    ("terminal", {"command": "ls -la"}, False),
    ("terminal", {"command": "ls > out.txt"}, True),
    ("terminal", {"command": "git status"}, False),
    ("terminal", {"command": "git checkout ."}, True),
    ("read_file", {"path": "a"}, False),
    ("web_search", {}, False),
    ("backtrack_timeline", {}, False),
])
def test_tool_classifier(name, args, expected):
    assert engine.is_mutating_tool(name, args) is expected


def test_classifier_respects_config():
    assert engine.is_mutating_tool("my_custom", {}, extra=["my_custom"])
    assert not engine.is_mutating_tool("write_file", {}, ignore=["write_file"])
