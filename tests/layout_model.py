"""In-memory tiling tree used by CLI fixtures to verify real pane geometry."""


def leaf(pane):
    return {"pane": pane}


def split_at(tree, target, pane, direction, ratio):
    if tree.get("pane") == target:
        return {"direction": direction, "ratio": ratio, "first": tree, "second": leaf(pane)}
    if "pane" in tree:
        return tree
    return {**tree, "first": split_at(tree["first"], target, pane, direction, ratio),
            "second": split_at(tree["second"], target, pane, direction, ratio)}


def remove(tree, pane):
    if "pane" in tree:
        return None if tree["pane"] == pane else tree
    first, second = remove(tree["first"], pane), remove(tree["second"], pane)
    if first is None:
        return second
    if second is None:
        return first
    return {**tree, "first": first, "second": second}


def rects(tree, rect):
    if "pane" in tree:
        return [{"pane_id": tree["pane"], "rect": rect}]
    first, second = dict(rect), dict(rect)
    axis, size = ("x", "width") if tree["direction"] == "right" else ("y", "height")
    first[size] *= tree["ratio"]
    second[size] -= first[size]
    second[axis] += first[size]
    return rects(tree["first"], first) + rects(tree["second"], second)


class LayoutModel:
    def __init__(self, state):
        self.state = state
        if "trees" not in state:
            state["trees"] = {}
            for pane in state["panes"]:
                tab = pane["tab_id"]
                existing = state["trees"].get(tab)
                state["trees"][tab] = leaf(pane["pane_id"]) if existing is None else {
                    "direction": "down", "ratio": 0.5, "first": existing, "second": leaf(pane["pane_id"])}
        state.setdefault("area", {"x": 0, "y": 0, "width": 180, "height": 60})
        state.setdefault("next_tab", 10)

    def pane(self, pane_id):
        return next(p for p in self.state["panes"] if p["pane_id"] == pane_id)

    def call(self, args):
        if args[:2] == ["pane", "layout"]:
            pane = self.pane(args[3])
            tab = pane["tab_id"]
            return {"layout": {"tab_id": tab, "workspace_id": pane["workspace_id"],
                               "focused_pane_id": args[3], "zoomed": False,
                               "area": self.state["area"],
                               "panes": rects(self.state["trees"][tab], self.state["area"])}}
        if args[:2] == ["pane", "split"]:
            source = self.pane(args[2])
            pane_id = f"{source['workspace_id']}:p{self.state['next_pane']}"
            self.state["next_pane"] += 1
            pane = {"pane_id": pane_id, "workspace_id": source["workspace_id"],
                    "tab_id": source["tab_id"], "cwd": args[args.index("--cwd") + 1]}
            self.state["panes"].append(pane)
            self.state["processes"][pane_id] = {"shell_pid": 1, "foreground_processes": [{"pid": 1, "name": "bash"}]}
            tab = source["tab_id"]
            self.state["trees"][tab] = split_at(self.state["trees"][tab], args[2], pane_id,
                                               args[args.index("--direction") + 1],
                                               float(args[args.index("--ratio") + 1]))
            return {"pane": pane}
        if args[:2] == ["pane", "move"]:
            pane = self.pane(args[2])
            source = pane["tab_id"]
            tree = remove(self.state["trees"][source], pane["pane_id"])
            if tree is None:
                del self.state["trees"][source]
            else:
                self.state["trees"][source] = tree
            if "--new-tab" in args:
                target = f"{pane['workspace_id']}:t{self.state['next_tab']}"
                self.state["next_tab"] += 1
                self.state["trees"][target] = leaf(pane["pane_id"])
            else:
                target = args[args.index("--tab") + 1]
                self.state["trees"][target] = split_at(
                    self.state["trees"][target], args[args.index("--target-pane") + 1], pane["pane_id"],
                    args[args.index("--split") + 1], float(args[args.index("--ratio") + 1]))
            pane["tab_id"] = target
            return {"move_result": {"changed": True, "pane": dict(pane)}}
        return {}
