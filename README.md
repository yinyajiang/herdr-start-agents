# herdr-start-agents

在当前 Herdr 窗口中一键补齐 Codex、OMP、Cursor、Kimi CLI。插件内部实现检测、布局和启动，不调用 `launch-agents` 或其他外部启动脚本。

需要 **Herdr >= 0.9.0、Python >= 3.10**，支持 Linux / WSL 和 macOS。要启动的 CLI 需已在 Herdr 服务端的 PATH 中安装。

## 安装与使用

本地开发安装：

```sh
herdr plugin link /absolute/path/to/herdr-start-agents
```

代码发布到 GitHub 后，可以安装：

```sh
herdr plugin install yinyajiang/herdr-start-agents
```

在 Herdr 的动作面板中选择 **启动当前窗口缺失的 CLI**，或者在当前 Herdr pane 内运行：

```sh
herdr plugin action invoke herdr-start-agents.start
```

每次调用会：

1. 读取配置，扫描**当前 workspace 的所有 tab / pane**。这里的“窗口”与参考脚本一致，指 workspace；其他 workspace 中的同类 CLI 不会阻止当前窗口启动。
2. 根据前台进程、进程入口和 Herdr 的待启动状态判断 CLI 是否存在。支持 `cursor-agent` / `agent`、`kimi-code`，以及 Node / Python 的 CLI 入口。仅有 `codex` 等 pane 标题、历史输出或命令参数不算已启动。
3. 跳过当前窗口已有的 CLI，将识别到的 pane 重命名为对应 `kind`。
4. 为缺失的 CLI 在当前 tab 中创建 pane，优先沿较长方向对半分割较大的 pane。所有新 pane 继承调用者 pane 的工作目录。
5. 将新 pane 重命名为 `codex`、`omp`、`cursor`、`kimi` 等，通过 Herdr 的 `agent start` 启动。注册的 agent 名若与其他窗口冲突，使用 `codex-2` 等后缀；pane 名始终是 CLI 类型名。

同一 session / workspace 的启动动作互斥，重复触发不会同时创建两批 pane。配置在每次调用时重新读取，不需要重启 Herdr。插件安装后通过动作启动，不会在服务启动或切换窗口时自动执行。

## 启动时的网格布局

每次执行启动动作都会重新检查**当前 tab 中所有 pane**的布局，包括已经运行的 CLI 和普通 shell。CLI 已全部启动时也会调整布局，其他 tab 的 pane 保持在原 tab。

- 4 个 pane 固定排列为 **2 行 × 2 列**，9 个为 3 行 × 3 列。
- 其他数量根据窗口宽高选择紧凑的行列数；宽窗口优先并排，窄长窗口优先上下排列。
- 行内等宽，行高按该行的 pane 数分配，使每个 pane 尽量占相同面积，允许终端边框和整数单元格带来的少量误差。
- 普通 shell 也计入总数：4 个 CLI 加 1 个 shell 是 5 个 pane，而不是 4 个。
- 保留 pane ID、已有进程和调用者焦点。布局已经符合网格时不重复移动。

Herdr 0.9.1 的布局重建接口会重建终端，因此插件通过一个短暂的后台 tab 中转现有 pane，再移动回原 tab；最后一个 pane 移出时，临时 tab 自动消失。移动失败时会尝试将中转中的 pane 移回；恢复失败会在日志中指出保留进程的临时 tab，不关闭终端。

## 配置

首次启动会生成默认配置，也可提前运行：

```sh
herdr plugin action invoke herdr-start-agents.init-config
herdr plugin config-dir herdr-start-agents
```

编辑上面命令输出目录中的 `config.json`。插件使用 Herdr 注入的 `HERDR_PLUGIN_CONFIG_DIR`，配置保存在源码目录之外，重复生成配置不会覆盖已有内容。

默认配置见 [config.example.json](config.example.json)：

```json
{
  "agents": [
    {"kind": "codex", "enabled": true, "args": ["--dangerously-bypass-approvals-and-sandbox"]},
    {"kind": "omp", "enabled": true, "args": []},
    {"kind": "cursor", "enabled": true, "args": ["--force"]},
    {"kind": "kimi", "enabled": true, "args": ["--auto"]}
  ],
  "startup_timeout_seconds": 30
}
```

默认启动参数为 `codex --dangerously-bypass-approvals-and-sandbox`、`kimi --auto`、Cursor `agent --force`（Herdr 使用等价入口 `cursor-agent --force`）。OMP 不追加参数。这些参数分别启用各 CLI 的自动执行 / 跳过确认模式，可通过 `args` 调整。

默认值仅用于新建配置，不覆盖已有用户配置；已经安装的用户需要在自己的 `config.json` 中更新对应 `args`。

- `enabled`：是否补齐该 CLI，默认 `true`。设为 `false` 不会关闭已经运行的 CLI。
- `agents` 数组顺序：CLI 启动顺序。也可以删掉不需要的条目；空数组不启动任何 CLI，但仍检查和调整当前 tab 布局。
- `args`：传给 CLI 的原生参数数组，例如 `["--model", "你的模型"]`。每项是一个参数，不做 shell 插值。
- `startup_timeout_seconds`：单个 CLI 的启动等待时间，默认 30，范围 4–300 秒。
- `kind`：Herdr 支持的 agent 类型。除了四个默认 CLI，也可添加 `claude`、`gemini` 等；可用类型以本机 `herdr agent` 帮助为准。
- `process_names`：可选进程名别名数组。默认四类已内置别名，添加其他 CLI 时可按需要指定，例如 `{"kind":"claude","process_names":["claude"],"enabled":true}`。

新 pane 的工作目录来自调用者 pane 的前台进程目录 / pane 目录，必要时使用 Herdr 插件上下文中的目录；不会使用插件源码所在目录。

## 快捷键

可以在 Herdr 的 `config.toml` 中增加：

```toml
[keys]
settings = "prefix+shift+s"

[[keys.command]]
key = "prefix+s"
type = "plugin_action"
command = "herdr-start-agents.start"
description = "启动缺失 CLI"
```

`prefix+s` 默认用于设置，上面的配置将设置入口移到 `prefix+shift+s`。若已经存在 `[keys]`，在原段落中修改 `settings`，不要重复添加段落。重新加载 Herdr 配置后，默认前缀下先按 `Ctrl+B`、松开再按 `S` 即可补齐 CLI 并调整网格布局。插件入口、环境变量和快捷键格式遵循 [Herdr 插件文档](https://herdr.ink/docs/plugins)。

## 失败与日志

- 无法读取 pane / 前台进程时停止创建，避免把“检测失败”当成“尚未运行”。
- 新 shell 尚未就绪，只对 Herdr 明确拒绝执行的 `agent_pane_busy` 做有限重试。
- CLI 已启动但停在信任确认 / 更新界面时，记录提示并继续其他 CLI；到对应 pane 手动处理。
- 启动超时或其他不确定失败不会重发启动命令。已经创建的 pane 会保留，其他 CLI 继续启动；检查该 pane 的实际状态后再执行动作。失败留下的空 pane 可手动关闭。
- 任何布局或启动失败，动作以非零状态退出。Herdr 的插件日志保存具体输出：

```sh
herdr plugin log list --plugin herdr-start-agents
```

## 开发与验证

运行测试需要 Python >= 3.11（测试用标准库 `tomllib` 校验 manifest；插件运行只需要 3.10）：

```sh
python3 -m unittest discover -s tests -v
```

测试覆盖配置开关、当前窗口去重、其他窗口 / tab、CLI 别名、工作目录、pane 标签、网格等面积布局、已有 CLI 的 2×2 重排、移动失败恢复、互斥调用、确认界面、超时以及部分失败。入口集成测试使用有状态的模拟 Herdr CLI，连续执行两次验证幂等性，不访问实际 Herdr session。

可选的真实 Herdr 验证会创建并清理专用临时 headless session，检查 2×2 / 五个 pane 的几何布局、pane ID、shell PID、焦点与重复调用，不访问用户 session：

```sh
python3 tests/native_layout_check.py
```
