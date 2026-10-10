#!/usr/bin/env python3
"""Herdr plugin entrypoint. All launch logic lives here; no external launcher."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time


PLUGIN_ID = "herdr-start-agents"
DEFAULT_CONFIG = {
    "agents": [
        {"kind": "codex", "enabled": True,
         "args": ["--dangerously-bypass-approvals-and-sandbox"]},
        {"kind": "omp", "enabled": True, "args": []},
        {"kind": "cursor", "enabled": True, "args": ["--force"]},
        {"kind": "kimi", "enabled": True, "args": ["--auto"]},
    ],
    "startup_timeout_seconds": 30,
}
ALIASES = {
    "codex": ("codex",),
    "omp": ("omp",),
    "cursor": ("cursor", "cursor-agent", "agent"),
    "kimi": ("kimi", "kimi-code"),
}
INTERPRETERS = {"node", "nodejs", "bun", "python", "python3", "deno"}


class PluginError(Exception):
    def __init__(self, message, code="plugin_error"):
        super().__init__(message)
        self.code = code


def user_dir(env, variable, category):
    if env.get(variable):
        return Path(env[variable]).expanduser()
    config_home = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config_home / "herdr" / "plugins" / category / PLUGIN_ID


def config_path(env):
    return user_dir(env, "HERDR_PLUGIN_CONFIG_DIR", "config") / "config.json"


def init_config(env):
    path = config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    # Publish a fully written file without replacing an existing user's config.
    # Concurrent init/start actions must never observe an empty JSON document.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".config-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(DEFAULT_CONFIG, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def load_config(env):
    path = init_config(env)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise PluginError(f"无法读取配置 {path}: {error}") from error
    if not isinstance(config, dict) or set(config) - {"agents", "startup_timeout_seconds"}:
        raise PluginError("配置必须为对象，且只能包含 agents、startup_timeout_seconds")
    agents = config.get("agents")
    if not isinstance(agents, list):
        raise PluginError("agents 必须是数组")
    seen = set()
    normalized = []
    for entry in agents:
        if not isinstance(entry, dict) or set(entry) - {"kind", "enabled", "args", "process_names"}:
            raise PluginError("agent 只能包含 kind、enabled、args、process_names")
        kind = entry.get("kind")
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", kind):
            raise PluginError("kind 必须为 1–24 位小写 CLI 类型名")
        if kind in seen:
            raise PluginError(f"配置中重复的 kind: {kind}")
        seen.add(kind)
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise PluginError(f"{kind}.enabled 必须为 true 或 false")
        args = entry.get("args", [])
        if not isinstance(args, list) or any(
            not isinstance(arg, str) or "\0" in arg for arg in args
        ):
            raise PluginError(f"{kind}.args 必须为不含 NUL 的字符串数组")
        names = entry.get("process_names", list(ALIASES.get(kind, (kind,))))
        if not isinstance(names, list) or not names or any(
            not isinstance(name, str) or not name or re.search(r"[\s/\\\0]", name)
            for name in names
        ):
            raise PluginError(f"{kind}.process_names 必须为非空进程名数组")
        normalized.append({"kind": kind, "enabled": enabled, "args": args,
                           "process_names": {basename(name) for name in names} | {kind}})
    timeout = config.get("startup_timeout_seconds", 30)
    if type(timeout) is not int or not 4 <= timeout <= 300:
        raise PluginError("startup_timeout_seconds 必须为 4–300 的整数")
    return normalized, timeout


class Herdr:
    def __init__(self, env):
        self.env = env
        self.binary = env.get("HERDR_BIN_PATH", "herdr")

    def call(self, *arguments, timeout=10):
        try:
            response = subprocess.run(
                [self.binary, *arguments], env=self.env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise PluginError("Herdr 请求超时；请检查 pane 后再重试", "transport_timeout") from error
        except OSError as error:
            raise PluginError(f"无法执行 Herdr: {error}") from error
        raw = response.stdout if response.returncode == 0 else response.stderr or response.stdout
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise PluginError(f"Herdr 返回非 JSON 数据: {raw.strip()[:500]}") from error
        if not isinstance(payload, dict):
            raise PluginError("Herdr 返回的 JSON 必须为对象")
        failure = payload.get("error")
        if failure:
            raise PluginError(failure.get("message", str(failure)), failure.get("code", "api_error"))
        if response.returncode:
            raise PluginError(f"Herdr 命令失败: {raw.strip()[:500]}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise PluginError("Herdr 返回缺少 result 对象")
        return result


def basename(value):
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def process_names(process):
    """Match executable positions only, never prompt text or arbitrary arguments."""
    names = {basename(process.get("name")), basename(process.get("argv0"))}
    argv = process.get("argv") or []
    if not argv and process.get("cmdline"):
        try:
            argv = shlex.split(process["cmdline"])
        except ValueError:
            argv = []
    if argv:
        executable = basename(argv[0])
        names.add(executable)
        # CLI shims commonly run as `node /path/codex` or `python3 /path/kimi`.
        if executable in INTERPRETERS and len(argv) > 1 and not argv[1].startswith("-"):
            script = basename(argv[1])
            names.add(script)
            if script.endswith((".js", ".mjs", ".cjs", ".py")):
                names.add(script.rsplit(".", 1)[0])
    return names - {""}


def invocation(env):
    if env.get("HERDR_ENV") != "1":
        raise PluginError("请在 Herdr 中调用插件动作（HERDR_ENV != 1）")
    try:
        context = json.loads(env.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    except ValueError as error:
        raise PluginError("HERDR_PLUGIN_CONTEXT_JSON 不是有效 JSON") from error
    if not isinstance(context, dict):
        raise PluginError("HERDR_PLUGIN_CONTEXT_JSON 必须为对象")
    workspace = env.get("HERDR_WORKSPACE_ID") or context.get("workspace_id")
    pane = env.get("HERDR_PANE_ID") or context.get("focused_pane_id")
    if not workspace or not pane:
        raise PluginError("缺少当前 workspace / pane，请从当前窗口的插件动作启动")
    return workspace, pane, context


@contextmanager
def workspace_lock(env, workspace):
    identity = env.get("HERDR_SOCKET_PATH") or env.get("HERDR_SESSION", "default")
    key = hashlib.sha256(f"{identity}\0{workspace}".encode()).hexdigest()
    directory = user_dir(env, "HERDR_PLUGIN_STATE_DIR", "state")
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{key}.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def list_field(result, field):
    value = result.get(field)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PluginError(f"Herdr 返回缺少有效的 {field} 数组，停止启动以避免重复")
    return value


def scan(herdr, workspace, agents):
    panes = list_field(herdr.call("pane", "list", "--workspace", workspace), "panes")
    live_agents = list_field(herdr.call("agent", "list"), "agents")
    found = {entry["kind"]: [] for entry in agents}
    # Never count an agent in a different workspace. Labels/names alone are not
    # process evidence, except for Herdr's explicitly pending managed launches.
    pending = {agent["pane_id"]: agent for agent in live_agents
               if agent.get("workspace_id") == workspace and agent.get("launch_pending")
               and agent.get("pane_id")}
    for pane in panes:
        pane_id = pane.get("pane_id")
        if not pane_id or pane.get("workspace_id") != workspace:
            raise PluginError("pane 列表上下文不完整，停止启动以避免跨窗口操作")
        info = herdr.call("pane", "process-info", "--pane", pane_id).get("process_info")
        if not isinstance(info, dict):
            raise PluginError(f"无法确认 {pane_id} 的前台进程，停止启动以避免重复")
        processes = info.get("foreground_processes", [])
        if not isinstance(processes, list) or any(not isinstance(p, dict) for p in processes):
            raise PluginError(f"{pane_id} 的前台进程数据无效")
        names = set().union(*(process_names(p) for p in processes))
        # Herdr may recognize a rewritten process title or an integration-only CLI.
        # Only consult agent labels when an actual non-shell foreground job exists.
        if any(p.get("pid") != info.get("shell_pid") for p in processes):
            names.add(basename(pane.get("agent")))
        if pane_id in pending:
            names.add(basename(pending[pane_id].get("agent")))
        for entry in agents:
            if names & entry["process_names"]:
                found[entry["kind"]].append(pane_id)
    # Current Herdr uses `name` for the registered name and `agent` for the kind;
    # reserving both also accommodates earlier releases' naming representation.
    reserved = {a.get(field) for a in live_agents for field in ("name", "agent") if a.get(field)}
    return panes, found, reserved


def unique_name(kind, reserved):
    name = kind
    suffix = 2
    while name in reserved:
        name = f"{kind}-{suffix}"
        suffix += 1
    return name


def start_agent(herdr, entry, pane, reserved, timeout):
    name = unique_name(entry["kind"], reserved)
    for attempt in range(10):
        arguments = ["agent", "start", name, "--kind", entry["kind"],
                     "--pane", pane, "--timeout", str(timeout * 1000)]
        if entry["args"]:
            arguments += ["--", *entry["args"]]
        try:
            herdr.call(*arguments, timeout=timeout + 10)
        except PluginError as error:
            if error.code == "agent_not_ready":
                reserved.add(name)
                print(f"{entry['kind']} -> {pane}: 已启动，需在 pane 中处理确认/更新界面")
                return
            if error.code == "agent_name_taken" and attempt < 9:
                reserved.add(name)
                name = unique_name(entry["kind"], reserved)
                continue
            # A busy rejection occurs before any input is sent. Only this error
            # can be retried for a new shell; timeout/input failures are ambiguous.
            if error.code == "agent_pane_busy" and attempt < 9:
                time.sleep(0.5)
                continue
            raise
        else:
            reserved.add(name)
            print(f"{entry['kind']} -> {pane} (agent: {name})")
            return


def launch(env):
    workspace, base, context = invocation(env)
    with workspace_lock(env, workspace) as acquired:
        if not acquired:
            print("当前窗口已有启动动作正在执行，跳过本次重复调用")
            return 0
        agents, timeout = load_config(env)
        enabled = [entry for entry in agents if entry["enabled"]]
        if not enabled:
            print("配置中没有启用的 CLI，无需启动")
            return 0
        herdr = Herdr(env)
        panes, found, reserved = scan(herdr, workspace, enabled)
        caller = next((p for p in panes if p["pane_id"] == base), None)
        if caller is None:
            raise PluginError("调用者 pane 已不存在或不属于当前 workspace")
        missing = []
        for entry in enabled:
            kind = entry["kind"]
            if found[kind]:
                for pane in found[kind]:
                    herdr.call("pane", "rename", pane, kind)
                print(f"跳过 {kind}: 当前窗口已在运行")
            else:
                missing.append(entry)
        if not missing:
            print("已启用的 CLI 均在当前窗口运行，无需启动")
            return 0
        cwd = (caller.get("foreground_cwd") or caller.get("cwd")
               or context.get("focused_pane_cwd") or context.get("workspace_cwd"))
        if not cwd or not Path(cwd).is_dir():
            raise PluginError(f"当前 pane 工作目录不可用: {cwd!r}")
        # Build the right column first, with equal-height rows in config order.
        created = []
        previous = base
        failed = False
        try:
            for index, entry in enumerate(missing):
                arguments = ["pane", "split", previous, "--direction",
                             "right" if index == 0 else "down", "--cwd", cwd, "--no-focus"]
                if index:
                    arguments += ["--ratio", f"{1 / (len(missing) - index + 1):.6f}"]
                pane = herdr.call(*arguments).get("pane", {}).get("pane_id")
                if not pane:
                    raise PluginError("pane split 未返回新 pane_id，停止创建")
                previous = pane
                created.append((entry, pane))
                herdr.call("pane", "rename", pane, entry["kind"])
        except PluginError as error:
            # Even if later splitting fails, launch the panes already created.
            print(f"布局未完全创建: {error}", file=sys.stderr)
            failed = True
        for entry, pane in created:
            try:
                start_agent(herdr, entry, pane, reserved, timeout)
            except PluginError as error:
                print(f"启动 {entry['kind']} 失败 ({pane}): {error}", file=sys.stderr)
                failed = True
        return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description="在当前 Herdr workspace 补齐已配置的 CLI")
    parser.add_argument("action", choices=("start", "init-config"))
    args = parser.parse_args()
    try:
        if args.action == "init-config":
            print(init_config(dict(os.environ)))
            return 0
        return launch(dict(os.environ))
    except (PluginError, OSError) as error:
        print(f"{PLUGIN_ID}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
