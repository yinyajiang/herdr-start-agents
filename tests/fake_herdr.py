#!/usr/bin/env python3
"""Stateful CLI fixture; never contacts or starts a real Herdr server."""

import json
import os
from pathlib import Path
import sys


path = Path(os.environ["FAKE_HERDR_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state["calls"].append(args)
result = {}
error = None

if args[:2] == ["pane", "list"]:
    workspace = args[3]
    result = {"panes": [p for p in state["panes"] if p["workspace_id"] == workspace]}
elif args[:2] == ["agent", "list"]:
    result = {"agents": state["agents"]}
elif args[:2] == ["pane", "process-info"]:
    result = {"process_info": state["processes"][args[3]]}
elif args[:2] == ["pane", "rename"]:
    for pane in state["panes"]:
        if pane["pane_id"] == args[2]:
            pane["label"] = args[3]
elif args[:2] == ["pane", "split"]:
    source = next(p for p in state["panes"] if p["pane_id"] == args[2])
    pane_id = f"{source['workspace_id']}:p{state['next_pane']}"
    state["next_pane"] += 1
    pane = {"pane_id": pane_id, "workspace_id": source["workspace_id"],
            "tab_id": source["tab_id"], "cwd": args[args.index("--cwd") + 1]}
    state["panes"].append(pane)
    state["processes"][pane_id] = {"shell_pid": 1, "foreground_processes": [{"pid": 1, "name": "bash"}]}
    result = {"pane": pane}
elif args[:2] == ["agent", "start"]:
    name = args[2]
    kind = args[args.index("--kind") + 1]
    pane_id = args[args.index("--pane") + 1]
    if any(a.get("name") == name for a in state["agents"]):
        error = {"code": "agent_name_taken", "message": "name already used"}
    else:
        pane = next(p for p in state["panes"] if p["pane_id"] == pane_id)
        state["agents"].append({"name": name, "agent": kind, "pane_id": pane_id,
                                "workspace_id": pane["workspace_id"], "tab_id": pane["tab_id"]})
        state["processes"][pane_id]["foreground_processes"] = [{"pid": 100, "name": kind}]
        result = {"agent": state["agents"][-1]}
else:
    error = {"code": "unexpected_command", "message": repr(args)}

path.write_text(json.dumps(state))
print(json.dumps({"error": error} if error else {"result": result}),
      file=sys.stderr if error else sys.stdout)
sys.exit(1 if error else 0)
