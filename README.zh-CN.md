<h1 align="center">Codex App Task Channel</h1>

<p align="center">
  <a href="README.md">English</a> |
  <strong>简体中文</strong>
</p>

<p align="center">
  <strong>Codex 桌面 App 的任务创建与消息通信备用通道。</strong>
</p>

<p align="center">
  <a href="skills/codex-app-task-channel/SKILL.md">
    <img alt="Codex Skill" src="https://img.shields.io/badge/Codex-Skill-2563eb?style=flat-square">
  </a>
  <a href="https://www.python.org/">
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-172554?style=flat-square">
  </a>
  <img alt="传输方式" src="https://img.shields.io/badge/transport-App%20Server-0f8f88?style=flat-square">
  <img alt="项目状态" src="https://img.shields.io/badge/status-experimental-d97706?style=flat-square">
</p>

<p align="center">
  <a href="#为什么需要它">为什么需要它</a>
  &middot;
  <a href="#功能">功能</a>
  &middot;
  <a href="#消息投递模式">消息投递模式</a>
  &middot;
  <a href="#安装">安装</a>
  &middot;
  <a href="skills/codex-app-task-channel/SKILL.md">Skill 参考文档</a>
</p>

Codex App Task Channel 提供 `codex-app-task-channel` Skill，主要解决两个相关问题：

1. **兼容性备用通道。** 当 `create_thread`、`send_message_to_thread` 等 App 任务操作缺失，或其已展示的动态处理器拒绝实际调用时，提供替代能力。
2. **App Server 能力扩展。** 暴露内置任务工具可能未提供的底层 thread、turn 和 Session 控制。最重要的是，调用方可以为不同任务分别选择配置，而不必为所有任务修改同一套 Codex 全局默认配置。

每任务控制项包括模型、推理强度、服务等级、工作目录、上下文窗口和自动压缩阈值。例如，可以让长期运行的 Sol 监控任务使用一种上下文窗口，同时让 Luna worker 或独立任务使用另一种窗口。上下文设置会在创建、fork 任务或符合条件的 resume 边界应用，并仍受所选模型支持的最大窗口和有效窗口折算限制。

普通操作仍应优先使用内置任务工具。只有当内置工具不可用，或者无法表达所需的每任务配置时，才使用本 Skill。该仓库不会启动独立的 app-server daemon；辅助程序会连接正在运行的 Codex 桌面 App 所拥有的 Unix socket，因此任务仍会显示在 App 侧边栏中，并共享 App 的任务状态。

## 为什么需要它

该项目针对两个实际缺口：

- **可用性缺口：** 某些 Codex 版本会向模型展示 `create_thread`、`send_message_to_thread` 等任务工具，但相应动态处理器不存在或拒绝调用。
- **配置缺口：** 内置任务工具可能支持常规创建与消息通信，但不提供任务专属上下文窗口、自动压缩阈值等 Session 设置。

桌面 App Server 已经提供底层 thread/turn 生命周期和配置覆盖协议。本 Skill 将这些能力封装为带有安全保护的辅助程序，在保留 App 可见性和历史记录的同时，支持状态感知消息投递，并根据运行时遥测核验任务专属上下文配置。

相关协议和 thread/turn 方法见 [Codex App Server 官方指南](https://developers.openai.com/codex/app-server)。

## 功能

| 功能 | 说明 |
| --- | --- |
| App 可见的任务创建 | 创建并命名新的侧边栏任务，然后启动其第一个 turn |
| 保留历史的 fork | 从已完成任务的历史分支出一个单独命名的 App 任务 |
| 状态感知消息投递 | 自动 steer 正在运行的 turn，或为 idle 任务启动新 turn |
| 立即干预 | 使用 steer 将用户输入追加到当前正在运行的 turn |
| 持久化延迟投递 | 将消息加入队列，等任务进入 idle 后再启动处理 |
| 恢复已有任务 | 重新加载未加载的任务，或在 idle 任务上启动下一个带配置 turn |
| 每任务配置 | 在协议支持范围内选择模型、推理强度、服务等级、工作目录、上下文窗口和自动压缩阈值 |
| 安全替换已加载 Session | 对上下文窗口等 Session 级设置执行显式、fail-closed 的冷替换 |
| 配置回执 | 报告请求值、观测到的有效上下文窗口以及稳定的消息/turn 标识符 |
| 只读检查 | 在不发送消息的情况下检查端点健康状态、任务状态和近期用户/Agent 消息 |
| 投递来源标识 | 为跨任务消息附加模型可见的来源任务 ID |

该 Skill 不替代嵌套 subagent，不修改 Codex 全局配置，也不维护隐藏的每任务配置注册表。如果后续冷 resume 仍需要 Session 级覆盖项，必须再次显式提供。

## 消息投递模式

| 模式 | 行为 | 适用场景 |
| --- | --- | --- |
| `steer` | 将输入追加到当前 active turn | 正在运行的 Agent 需要立即看到消息 |
| `start` | 在 idle 任务上启动新 turn | 任务处于 idle，需要立即开始工作 |
| `followup` | active 时 steer，否则 start | 需要最接近普通任务消息的备用行为 |
| `queue` | 持久保存消息，任务 idle 后再投递 | 消息必须跨越 active turn 或临时断连而保留 |

`queue` 不是 steer，它不会改变当前 active turn。`steer` 无法修改已经运行中的 turn 所使用的模型或推理强度；需要更改配置时，应等待任务进入 idle，然后使用 `start`。

`resume` 不是 queue，也不是同一 turn 内的干预。它要求任务处于 idle 或未加载状态，解析 Session 配置后启动新 turn。立即控制 active 工作应使用 steer；需要持久化的稍后投递应使用 queue。

当调用方需要已完成 turn 的可验证回执，而不只是已接受的 turn id 时，可在
`send`、`create`、`fork` 或 `resume` 后添加 `--wait`。回执会包含会话消息和
`lastAgentMessage`。

## 读取任务内容

```bash
python skills/codex-app-task-channel/scripts/task_channel.py read \
  --thread THREAD_ID --turn-limit 3
```

默认输出包含最新 turn 的用户与 Agent 消息，并省略体积较大的工具 items。
只有需要完整工具级证据时才添加 `--include-items`。辅助程序通过 App Server
原生的 `thread/turns/list` 与 `itemsView: "full"` 读取内容，因此即使上层任务
摘要只返回空 turn 外壳，也可以核验实际会话内容。

## 安装

```bash
git clone https://github.com/xukp20/codex-app-task-channel.git
cd codex-app-task-channel
python -m pip install 'websockets>=14,<17'
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$PWD/skills/codex-app-task-channel" \
  "${CODEX_HOME:-$HOME/.codex}/skills/codex-app-task-channel"
```

安装后重新加载 Codex，使其发现该 Skill。使用符号链接安装时，可以通过 `git pull --ff-only` 更新。

## 验证 App 所拥有的端点

```bash
python skills/codex-app-task-channel/scripts/task_channel.py doctor
```

默认 socket 为：

```text
${CODEX_HOME:-$HOME/.codex}/app-server-control/app-server-control.sock
```

可以通过 `CODEX_APP_SERVER_SOCKET` 或 `--socket` 覆盖。当前辅助程序支持本地桌面 App 使用的 Unix socket。

操作选择、命令示例、resume 行为和冷替换要求见 [Skill 参考文档](skills/codex-app-task-channel/SKILL.md)。

## 安全边界

- 首先尝试内置任务工具。只有工具发现失败，或者一次精确调用返回处理器不可用错误后，才进入备用通道。
- 内置变更操作失败后的实际结果可能不明确。通过本通道重试前，应先读取或列出任务状态，避免延迟成功造成重复操作。
- 辅助程序不会自动批准请求，也不会扩大 sandbox 或 approval 设置。除非调用方显式选择模型、推理强度或工作目录，否则新任务继承正常 Codex 配置。
- 连接桌面 App 已有的 socket。启动单独的 app-server 可能创建不归桌面 App 管理或无法立即显示的任务。
- `followup` 使用相同的 `clientUserMessageId` 执行一次具备竞态感知的重试；它不会把失败的 steer 静默转换为排队消息。

## 仓库结构

```text
codex-app-task-channel/
├── README.md
├── README.zh-CN.md
├── LICENSE
├── requirements.txt
├── tests/
└── skills/
    └── codex-app-task-channel/
        ├── SKILL.md
        ├── agents/openai.yaml
        ├── references/app-server-protocol.md
        └── scripts/task_channel.py
```

仓库根目录 README 用于项目说明。只有 `skills/` 下的目录会链接到 Codex skills 目录。

## 验证

```bash
python /path/to/skill-creator/scripts/quick_validate.py \
  skills/codex-app-task-channel
python -m unittest discover -s tests -v
python skills/codex-app-task-channel/scripts/task_channel.py doctor
```

单元测试会覆盖 JSON-RPC 响应路由、active turn 选择、内容读取、完成回执、委派信封和消息投递模式行为，不会修改 App 状态。`doctor` 命令执行初始化后的只读 App Server 握手。

## 兼容性

这是一个基于当前 Codex App Server v2 thread/turn 协议构建的实验性备用工具。Codex 升级后应重新运行 `doctor`。如果内置任务工具可用，即使本辅助程序也通过检查，仍应优先使用内置工具。
