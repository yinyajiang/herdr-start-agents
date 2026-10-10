from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import tomllib
import unittest
from unittest.mock import patch

import main
from layout_model import LayoutModel, rects


ROOT = Path(__file__).resolve().parents[1]


class FakeAPI:
    def __init__(self, panes, processes, agents=None, errors=None):
        self.panes = panes
        self.processes = processes
        self.agents = agents or []
        self.errors = errors or {}
        self.calls = []
        self.state = {"panes": panes, "processes": processes, "next_pane": 10}
        self.layout = LayoutModel(self.state)

    def call(self, *args, timeout=10):
        self.calls.append(args)
        error = self.errors.get(args[:2])
        if callable(error):
            error = error(args)
        if error:
            raise error
        if args[:2] == ("pane", "list"):
            return {"panes": self.panes}
        if args[:2] == ("agent", "list"):
            return {"agents": self.agents}
        if args[:2] == ("pane", "process-info"):
            return {"process_info": self.processes[args[3]]}
        return self.layout.call(list(args))


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = {
            "HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1", "HERDR_PANE_ID": "w1:p1",
            "HERDR_SOCKET_PATH": "/test/herdr.sock",
            "HERDR_PLUGIN_CONFIG_DIR": str(self.directory / "config"),
            "HERDR_PLUGIN_STATE_DIR": str(self.directory / "state"),
        }
        self.panes = [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
                       "cwd": str(self.directory)}]
        self.processes = {"w1:p1": {"shell_pid": 1, "foreground_processes": [{"pid": 1, "name": "bash"}]}}

    def config(self, config):
        path = main.init_config(self.env)
        path.write_text(json.dumps(config))

    def run_launch(self, fake):
        with patch.object(main, "Herdr", return_value=fake), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main.launch(self.env)

    def test_new_agents_equal_layout_cwd_labels_and_focus(self):
        fake = FakeAPI(self.panes, self.processes)
        self.assertEqual(self.run_launch(fake), 0)
        splits = [c for c in fake.calls if c[:2] == ("pane", "split")]
        self.assertEqual([c[4] for c in splits], ["right", "down", "down", "right"])
        self.assertTrue(all(c[c.index("--ratio") + 1] == "0.5" for c in splits))
        self.assertTrue(all("--no-focus" in c and str(self.directory) in c for c in splits))
        starts = [c for c in fake.calls if c[:2] == ("agent", "start")]
        self.assertEqual([c[4] for c in starts], ["codex", "omp", "cursor", "kimi"])
        self.assertEqual(starts[0][-2:], ("--", "--dangerously-bypass-approvals-and-sandbox"))
        self.assertNotIn("--", starts[1])
        self.assertEqual(starts[2][-2:], ("--", "--force"))
        self.assertEqual(starts[3][-2:], ("--", "--auto"))
        self.assertEqual([c[3] for c in fake.calls if c[:2] == ("pane", "rename")],
                         ["codex", "omp", "cursor", "kimi"])
        self.assertEqual(len(fake.state["trees"]), 1)
        areas = [p["rect"]["width"] * p["rect"]["height"] for p in
                 rects(fake.state["trees"]["w1:t1"], fake.state["area"])]
        self.assertLess(max(areas) - min(areas), 0.05)

    def test_four_existing_agents_become_two_rows_two_columns_without_restart(self):
        self.processes["w1:p1"] = {"foreground_processes": [{"pid": 101, "name": "codex"}]}
        for index, kind in enumerate(("omp", "cursor", "kimi"), 2):
            pane = f"w1:p{index}"
            self.panes.append({"pane_id": pane, "workspace_id": "w1", "tab_id": "w1:t1"})
            self.processes[pane] = {"foreground_processes": [{"pid": 100 + index, "name": kind}]}
        before = json.dumps(self.processes, sort_keys=True)
        fake = FakeAPI(self.panes, self.processes)
        self.assertEqual(self.run_launch(fake), 0)
        result = rects(fake.state["trees"]["w1:t1"], fake.state["area"])
        self.assertEqual({p["rect"]["x"] for p in result}, {0, 90})
        self.assertEqual({p["rect"]["y"] for p in result}, {0, 30})
        self.assertTrue(all(p["rect"]["width"] == 90 and p["rect"]["height"] == 30 for p in result))
        self.assertEqual(json.dumps(self.processes, sort_keys=True), before)
        self.assertFalse(any(c[:2] in (("agent", "start"), ("pane", "split"), ("pane", "close")) for c in fake.calls))
        self.assertTrue(all("--no-focus" in c for c in fake.calls if c[:2] == ("pane", "move")))
        moves = len([c for c in fake.calls if c[:2] == ("pane", "move")])
        self.assertEqual(self.run_launch(fake), 0)
        self.assertEqual(len([c for c in fake.calls if c[:2] == ("pane", "move")]), moves)

    def test_grid_uses_window_shape_and_equal_area_for_ragged_rows(self):
        self.assertEqual(main.grid_rows(4, 240, 60), [2, 2])
        self.assertEqual(main.grid_rows(2, 240, 60), [2])
        self.assertEqual(main.grid_rows(2, 60, 120), [1, 1])
        for count in range(1, 17):
            rows = main.grid_rows(count, 180, 60)
            self.assertEqual(sum(rows), count)
            expected = main.grid_rects(list(range(count)), rows, {"width": 180, "height": 60})
            areas = [p["width"] * p["height"] for p in expected.values()]
            self.assertAlmostEqual(max(areas), min(areas))

    def test_disabling_all_launches_still_arranges_existing_panes(self):
        self.config({"agents": []})
        for index in range(2, 5):
            self.panes.append({"pane_id": f"w1:p{index}", "workspace_id": "w1", "tab_id": "w1:t1"})
        fake = FakeAPI(self.panes, self.processes)
        self.assertEqual(self.run_launch(fake), 0)
        result = rects(fake.state["trees"]["w1:t1"], fake.state["area"])
        self.assertEqual({p["rect"]["x"] for p in result}, {0, 90})
        self.assertEqual({p["rect"]["y"] for p in result}, {0, 30})
        self.assertFalse(any(c[:2] == ("agent", "start") for c in fake.calls))

    def test_layout_failure_recovers_existing_processes(self):
        self.config({"agents": [{"kind": "codex"}]})
        self.processes["w1:p1"]["foreground_processes"] = [{"name": "codex", "pid": 101}]
        for index in range(2, 5):
            pane = f"w1:p{index}"
            self.panes.append({"pane_id": pane, "workspace_id": "w1", "tab_id": "w1:t1"})
            self.processes[pane] = {"foreground_processes": [{"name": "bash", "pid": index}]}
        count = 0

        def fail_once(args):
            nonlocal count
            count += 1
            return main.PluginError("move failure") if count == 3 else None

        before = json.dumps(self.processes, sort_keys=True)
        fake = FakeAPI(self.panes, self.processes, errors={("pane", "move"): fail_once})
        with self.assertRaises(main.PluginError):
            self.run_launch(fake)
        self.assertEqual({p["tab_id"] for p in fake.panes}, {"w1:t1"})
        self.assertEqual(len(fake.state["trees"]), 1)
        self.assertEqual(json.dumps(self.processes, sort_keys=True), before)

    def test_only_missing_enabled_cli_and_native_arguments(self):
        self.config({"agents": [{"kind": "codex", "enabled": False},
                                {"kind": "cursor", "args": ["--model", "value with spaces"]},
                                {"kind": "kimi"}]})
        self.processes["w1:p1"]["foreground_processes"] = [{"name": "kimi-code", "pid": 2}]
        fake = FakeAPI(self.panes, self.processes)
        self.assertEqual(self.run_launch(fake), 0)
        starts = [c for c in fake.calls if c[:2] == ("agent", "start")]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][4], "cursor")
        self.assertEqual(starts[0][-3:], ("--", "--model", "value with spaces"))
        self.assertIn(("pane", "rename", "w1:p1", "kimi"), fake.calls)

    def test_other_workspace_agent_does_not_suppress_start_and_name_is_unique(self):
        self.config({"agents": [{"kind": "codex"}]})
        fake = FakeAPI(self.panes, self.processes,
                       agents=[{"name": "codex", "agent": "codex", "workspace_id": "w2", "pane_id": "w2:p1"}])
        self.assertEqual(self.run_launch(fake), 0)
        self.assertEqual(next(c[2] for c in fake.calls if c[:2] == ("agent", "start")), "codex-2")

    def test_other_tab_in_workspace_is_counted(self):
        self.config({"agents": [{"kind": "codex"}]})
        self.panes.append({"pane_id": "w1:p2", "workspace_id": "w1", "tab_id": "w1:t2"})
        self.processes["w1:p2"] = {"foreground_processes": [{"name": "codex"}]}
        fake = FakeAPI(self.panes, self.processes)
        self.assertEqual(self.run_launch(fake), 0)
        self.assertFalse(any(c[:2] == ("pane", "split") for c in fake.calls))

    def test_stale_pane_label_is_not_process_evidence(self):
        self.config({"agents": [{"kind": "codex"}]})
        self.panes[0].update(label="codex", agent="codex")
        fake = FakeAPI(self.panes, self.processes)
        self.assertEqual(self.run_launch(fake), 0)
        self.assertTrue(any(c[:2] == ("agent", "start") for c in fake.calls))

    def test_pending_managed_launch_is_not_duplicated(self):
        self.config({"agents": [{"kind": "codex"}]})
        fake = FakeAPI(self.panes, self.processes, agents=[{
            "agent": "codex", "workspace_id": "w1", "pane_id": "w1:p1", "launch_pending": True}])
        self.assertEqual(self.run_launch(fake), 0)
        self.assertFalse(any(c[:2] == ("agent", "start") for c in fake.calls))

    def test_foreground_cwd_precedes_plugin_root_and_context(self):
        self.config({"agents": [{"kind": "codex"}]})
        project = self.directory / "project with spaces"
        project.mkdir()
        self.panes[0]["foreground_cwd"] = str(project)
        self.env["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"focused_pane_cwd": "/wrong"})
        fake = FakeAPI(self.panes, self.processes)
        self.assertEqual(self.run_launch(fake), 0)
        self.assertIn(str(project), next(c for c in fake.calls if c[:2] == ("pane", "split")))

    def test_failed_scan_causes_no_mutations(self):
        fake = FakeAPI(self.panes, self.processes, errors={
            ("pane", "process-info"): main.PluginError("unavailable")})
        with self.assertRaises(main.PluginError):
            self.run_launch(fake)
        self.assertFalse(any(c[:2] in (("pane", "split"), ("pane", "rename"), ("agent", "start")) for c in fake.calls))

    def test_partial_layout_failure_still_starts_created_pane(self):
        count = 0

        def fail_second_split(args):
            nonlocal count
            count += 1
            return main.PluginError("too small") if count == 2 else None

        fake = FakeAPI(self.panes, self.processes, errors={("pane", "split"): fail_second_split})
        self.assertEqual(self.run_launch(fake), 1)
        self.assertEqual([c[4] for c in fake.calls if c[:2] == ("agent", "start")], ["codex"])

    def test_blocked_start_is_not_retried_and_remaining_agents_start(self):
        fake = FakeAPI(self.panes, self.processes, errors={
            ("agent", "start"): lambda c: main.PluginError("needs confirmation", "agent_not_ready") if c[4] == "codex" else None})
        self.assertEqual(self.run_launch(fake), 0)
        self.assertEqual(len([c for c in fake.calls if c[:2] == ("agent", "start")]), 4)

    def test_timeout_is_not_retried_and_remaining_agents_start(self):
        fake = FakeAPI(self.panes, self.processes, errors={
            ("agent", "start"): lambda c: main.PluginError("timeout", "timeout") if c[4] == "codex" else None})
        self.assertEqual(self.run_launch(fake), 1)
        self.assertEqual(len([c for c in fake.calls if c[:2] == ("agent", "start")]), 4)

    def test_busy_shell_is_retried(self):
        self.config({"agents": [{"kind": "codex"}]})
        count = 0

        def busy_once(args):
            nonlocal count
            count += 1
            return main.PluginError("not ready", "agent_pane_busy") if count == 1 else None

        fake = FakeAPI(self.panes, self.processes, errors={("agent", "start"): busy_once})
        with patch.object(main.time, "sleep"):
            self.assertEqual(self.run_launch(fake), 0)
        self.assertEqual(count, 2)

    def test_duplicate_invocation_uses_same_workspace_lock(self):
        fake = FakeAPI(self.panes, self.processes)
        with main.workspace_lock(self.env, "w1") as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.run_launch(fake), 0)
            self.assertEqual(fake.calls, [])
            with main.workspace_lock(self.env, "w2") as other:
                self.assertTrue(other)

    def test_invalid_config_rejected_before_api_calls(self):
        configs = [
            {"agents": [{"kind": "codex", "enabled": "false"}]},
            {"agents": [{"kind": "codex"}, {"kind": "codex"}]},
            {"agents": [{"kind": "codex", "args": "--flag"}]},
            {"agents": [{"kind": "codex", "enabeld": False}]},
            {"agents": [], "startup_timeout_seconds": True},
        ]
        for config in configs:
            with self.subTest(config=config):
                self.config(config)
                fake = FakeAPI(self.panes, self.processes)
                with self.assertRaises(main.PluginError):
                    self.run_launch(fake)
                self.assertEqual(fake.calls, [])

    def test_init_config_preserves_existing_user_settings(self):
        self.config({"agents": []})
        main.init_config(self.env)
        self.assertEqual(main.load_config(self.env), ([], 30))

    def test_concurrent_config_creation_publishes_complete_json(self):
        def initialize(_):
            path = main.init_config(self.env)
            return json.loads(path.read_text())

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(initialize, range(32)))
        self.assertTrue(all(config == main.DEFAULT_CONFIG for config in results))
        self.assertEqual(list(main.config_path(self.env).parent.glob(".config-*")), [])

    def test_context_without_env_ids_and_missing_context(self):
        env = {"HERDR_ENV": "1", "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({
            "workspace_id": "w1", "focused_pane_id": "w1:p1"})}
        self.assertEqual(main.invocation(env)[:2], ("w1", "w1:p1"))
        for bad in ({}, {"HERDR_ENV": "1"}):
            with self.assertRaises(main.PluginError):
                main.invocation(bad)

    def test_aliases_paths_argv0_and_interpreters(self):
        for process, expected in [
            ({"name": "node", "argv": ["node", "/opt/codex/bin/codex.js"]}, "codex"),
            ({"name": "unknown", "argv0": "/opt/cursor-agent"}, "cursor-agent"),
            ({"name": "unknown", "argv0": "/opt/agent"}, "agent"),
            ({"name": "python3", "cmdline": "python3 '/a path/kimi'"}, "kimi"),
            ({"name": "C:\\bin\\CODEX.EXE"}, "codex"),
        ]:
            with self.subTest(process=process):
                self.assertIn(expected, main.process_names(process))
        self.assertNotIn("codex", main.process_names({"name": "vim", "argv": ["vim", "codex"]}))
        self.assertNotIn("codex", main.process_names({"name": "node", "argv": ["node", "-e", "codex"]}))

    def test_manifest_and_example_match_runtime(self):
        manifest = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())
        self.assertEqual(manifest["id"], main.PLUGIN_ID)
        self.assertEqual(json.loads((ROOT / "config.example.json").read_text()), main.DEFAULT_CONFIG)
        for action in manifest["actions"]:
            self.assertTrue((ROOT / action["command"][1]).is_file())
        self.assertNotIn("startup", manifest)

    def test_real_entrypoint_and_cli_transport_are_idempotent(self):
        state_path = self.directory / "fake-state.json"
        state_path.write_text(json.dumps({"panes": self.panes, "processes": self.processes,
                                         "agents": [], "calls": [], "next_pane": 10}))
        env = {**os.environ, **self.env, "FAKE_HERDR_STATE": str(state_path),
               "HERDR_BIN_PATH": str(ROOT / "tests" / "fake_herdr.py")}
        for _ in range(2):
            result = subprocess.run([sys.executable, str(ROOT / "main.py"), "start"],
                                    env=env, cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(state_path.read_text())
        self.assertEqual(len(state["panes"]), 5)
        self.assertEqual(len(state["agents"]), 4)
        self.assertEqual([p.get("label") for p in state["panes"][1:]], ["codex", "omp", "cursor", "kimi"])
        self.assertEqual(len([c for c in state["calls"] if c[:2] == ["agent", "start"]]), 4)


if __name__ == "__main__":
    unittest.main()
