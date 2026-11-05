"""Backtrack: per-action checkpoints and undo for files changed by Hermes Agent."""
from __future__ import annotations

import logging

from .runtime import Runtime, DEFAULTS

log = logging.getLogger("backtrack")

TIMELINE_SCHEMA = {
    "name": "backtrack_timeline",
    "description": (
        "List the recent file-changing actions Backtrack has checkpointed in the current workspace "
        "(tool, summary, files changed). Read-only. Use it to explain what changed or what the user "
        "can undo with /undo."
    ),
    "parameters": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "How many recent actions (1-50)", "default": 10}},
    },
}


def _cli_setup(parser):
    sub = parser.add_subparsers(dest="backtrack_cmd")
    sub.add_parser("list", help="Show the checkpoint timeline")
    sub.add_parser("redo", help="Redo the last undo")
    d = sub.add_parser("diff", help="Show what undoing an action would change")
    d.add_argument("id", type=int)
    u = sub.add_parser("undo", help="Undo to before an action (default: the last one)")
    u.add_argument("id", type=int, nargs="?")
    u.add_argument("--yes", action="store_true", help="Apply instead of previewing")


def register(ctx):
    def get_setting(key, default):
        getter = getattr(ctx, "get_config", None)
        return getter(key, default=default) if getter else default

    rt = Runtime(get_setting)

    ctx.register_hook("pre_tool_call", rt.pre_tool_call)
    ctx.register_hook("post_tool_call", rt.post_tool_call)
    ctx.register_command("undo", rt.cmd_undo,
                         description="Preview or apply an undo of the agent's last file-changing action")
    ctx.register_command("backtrack", rt.cmd_backtrack,
                         description="Backtrack timeline: list, diff <id>, redo")
    ctx.register_tool(name="backtrack_timeline", toolset="backtrack",
                      schema=TIMELINE_SCHEMA, handler=rt.tool_timeline)

    def _cli_handler(args):
        parts = [getattr(args, "backtrack_cmd", None) or "list"]
        if parts[0] == "undo":
            return print(rt.cmd_undo(" ".join(filter(None, [str(args.id) if args.id else "",
                                                           "--yes" if args.yes else ""]))))
        if parts[0] == "diff":
            return print(rt.cmd_backtrack(f"diff {args.id}"))
        print(rt.cmd_backtrack(parts[0]))

    try:  # signature is not documented in the public guide: fail soft, keep the rest working
        ctx.register_cli_command(name="backtrack", help="Inspect and undo agent file changes",
                                 setup_fn=_cli_setup, handler_fn=_cli_handler)
    except Exception as exc:
        log.warning("backtrack: CLI subcommand not registered (%s); slash commands still work", exc)
