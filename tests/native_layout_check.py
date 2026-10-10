"""Opt-in verification against a self-owned isolated Herdr headless session."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


def run():
    binary = shutil.which("herdr")
    if not binary:
        raise SystemExit("herdr is required")
    with tempfile.TemporaryDirectory(prefix="hsg-", dir="/tmp") as directory:
        root = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env.update(XDG_CONFIG_HOME=str(root / "c"), XDG_STATE_HOME=str(root / "s"),
                   XDG_RUNTIME_DIR=str(root / "r"),
                   HERDR_CONFIG_PATH=str(root / "c/herdr/config.toml"))
        (root / "r").mkdir()
        session = "grid-test"
        prefix = [binary, "--session", session]

        class NativeHerdr:
            def __init__(self):
                self.moves = 0

            def call(self, *arguments, timeout=10):
                if arguments[:2] == ("pane", "move"):
                    self.moves += 1
                response = subprocess.run([*prefix, *arguments], env=env, capture_output=True,
                                          text=True, timeout=timeout)
                raw = response.stdout if response.returncode == 0 else response.stderr
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    raise main.PluginError(raw.strip()) from None
                if response.returncode or parsed.get("error"):
                    raise main.PluginError(str(parsed))
                return parsed["result"]

        herdr = NativeHerdr()
        with (root / "server.log").open("w") as log:
            server = subprocess.Popen([*prefix, "server"], env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        herdr.call("workspace", "list")
                        break
                    except main.PluginError:
                        if server.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError((root / "server.log").read_text())
                        time.sleep(0.1)
                created = herdr.call("workspace", "create", "--cwd", str(root), "--label", "grid-test")
                base = created["root_pane"]["pane_id"]
                workspace = created["workspace"]["workspace_id"]
                for _ in range(3):
                    herdr.call("pane", "split", base, "--direction", "down", "--ratio", "0.3",
                               "--cwd", str(root), "--no-focus")
                before = {p["pane_id"]: herdr.call("pane", "process-info", "--pane", p["pane_id"])["process_info"]["shell_pid"]
                          for p in herdr.call("pane", "list", "--workspace", workspace)["panes"]}
                main.arrange_grid(herdr, base)
                layout = main.layout_snapshot(herdr, base)
                assert len(layout["panes"]) == 4, layout
                assert len({p["rect"]["x"] for p in layout["panes"]}) == 2, layout
                assert len({p["rect"]["y"] for p in layout["panes"]}) == 2, layout
                after = {p["pane_id"]: herdr.call("pane", "process-info", "--pane", p["pane_id"])["process_info"]["shell_pid"]
                         for p in herdr.call("pane", "list", "--workspace", workspace)["panes"]}
                assert before == after, (before, after)
                assert layout["focused_pane_id"] == base, layout
                assert len(herdr.call("tab", "list", "--workspace", workspace)["tabs"]) == 1
                moves = herdr.moves
                main.arrange_grid(herdr, base)
                assert herdr.moves == moves, "Repeated layout should not move panes"
                print("Native 2x2 geometry, stable pane IDs/shell PIDs, focus and idempotency: PASS")
                herdr.call("pane", "split", base, "--direction", "right", "--ratio", "0.5",
                           "--cwd", str(root), "--no-focus")
                main.arrange_grid(herdr, base)
                layout = main.layout_snapshot(herdr, base)
                ordered = sorted(layout["panes"], key=lambda p: (p["rect"]["y"], p["rect"]["x"]))
                ids = [p["pane_id"] for p in ordered]
                rows = main.grid_rows(5, layout["area"]["width"], layout["area"]["height"])
                expected = main.grid_rects(ids, rows, layout["area"])
                assert all(all(abs(p["rect"][key] - expected[p["pane_id"]][key]) <= 2
                               for key in ("x", "y", "width", "height")) for p in ordered), layout
                moves = herdr.moves
                main.arrange_grid(herdr, base)
                assert moves == herdr.moves
                print("Native five-pane grid geometry and idempotency: PASS")
            finally:
                # This server and every pane in it belong exclusively to the test.
                subprocess.run([*prefix, "server", "stop"], env=env, capture_output=True, timeout=10)
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.terminate()
                    server.wait(timeout=5)


if __name__ == "__main__":
    run()
