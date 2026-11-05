# Backtrack

Undo for your agent. Backtrack checkpoints the files Hermes changes **before every file-changing
tool call**, so any action can be previewed, undone and redone, from a slash command, the CLI, or a
Desktop timeline.

Hermes's built-in memory-rewind versions Hermes's own memory and skill files. Backtrack covers your
**project**, including damage done by terminal commands.

## Use

```
/undo                 preview undoing the last action (diff + file list, nothing changes)
/undo --yes           apply it; the restore is verified by tree hash
/undo 7 --yes         restore the state before action #7 (reverts #7 and later)
/backtrack            timeline        /backtrack diff 7        /backtrack redo
hermes backtrack list|diff <id>|undo [id] [--yes]|redo
```

The Desktop pane lists every action with Preview and Restore buttons. The agent also gets one
read-only tool, `backtrack_timeline`.

## How it works

- `pre_tool_call` snapshots the working tree into a **shadow git store** under the plugin data
  directory (never your project's `.git`). `post_tool_call` snapshots again, so each action shows
  exactly which files it changed. Unchanged trees reuse the previous checkpoint.
- Backtrack never blocks, modifies or auto-approves a tool call. It only observes.
- Restoring first takes a pre-restore checkpoint, so every undo can itself be redone.
- Verification compares the git tree hash of the live tree against the checkpoint.

## Honest limits

- Only files under the workspace are tracked. Sent messages, API calls, installs and files outside
  the workspace cannot be undone.
- Skipped on purpose: `node_modules`, `.venv`, `__pycache__`, `.next`, caches, nested git repos, files
  over 5 MB. If the tree exceeds the total cap (500 MB / 50,000 files) the action is logged as "not
  checkpointed" with the reason, and restore is refused rather than guessed.
- The workspace is `workspace` setting, else `TERMINAL_CWD`, else the Hermes process's current
  directory.

## Settings (`plugins.entries.backtrack.settings`)

`enabled` (true), `workspace` (""), `max_file_mb` (5), `max_total_mb` (500), `max_files` (50000),
`extra_tools` ([]), `ignore_tools` ([]), `exclude_dirs` (list).

## Develop

```
cd tests && python3 -m pytest -q          # real git + real files, no mocks
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

Requires `git` on PATH. No other dependencies.
