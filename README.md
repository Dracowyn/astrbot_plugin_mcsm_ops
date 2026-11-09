# MCSManager 服务器运维

通过 MCSManager 面板在 QQ 群里运维 Minecraft 服务器：多实例按群绑定，查询状态、在线玩家、TPS 与日志，执行命令、启停和管理白名单，推送崩溃告警，并提供受限的 LLM 只读工具。

适用于 AstrBot `>=4.16,<5`、OneBot v11（aiocqhttp）、MCSManager 10.x，默认按 NeoForge 服务端配置。

本插件**不做聊天互通**。需要群服互通时，请配合 [mcqq](https://github.com/kterna/astrbot_plugin_mcqq) 与[鹊桥 QueQiao](https://github.com/17TheWord/QueQiao) 使用（本插件未使用它们的代码）。

## 与现有 MCSManager 插件的区别

插件市场已有 [astrbot_plugin_mcsmanager](https://github.com/HSOS6/astrbot_plugin_mcsmanager) 和 [astrbot_plugin_mcsmanager_function](https://github.com/venti1112/astrbot_plugin_mcsmanager_function)，它们侧重**面板管理**（实例、节点、文件、面板用户、节点终端）。本插件侧重**在 QQ 群里运营 Minecraft 服务器**：

- **群与实例绑定**：群里只能看到、操作绑定的实例，管理员也不例外。
- **读懂 Minecraft 控制台**：解析在线玩家名单和 TPS，玩家在聊天里伪造不了结果。
- **崩溃告警**：意外停止、疑似崩溃后被面板自动重启、面板失联时主动推送到绑定群。
- **白名单管理**：`/mcs wl` 校验玩家名，可一次作用于本群绑定的全部实例。
- **适合公开群和游戏内**：LLM 只有只读工具，命令必须完全匹配管理员配置的白名单。

需要文件管理、面板用户管理或节点终端，请使用上面两个插件。

## 面板侧准备

1. 在 MCSManager 面板设置中开启 **API 密钥**功能。
2. 新建一个专用普通用户，只分配需要管理的实例；不要使用超级管理员的密钥。
3. 登录该用户，在「用户设置」生成 API 密钥，填入插件的 `api_key`。返回 403 通常是 API 密钥功能未开启、密钥无效或用户没有实例权限。
4. 打开每个实例的控制台页，从地址栏 URL 读取 `daemonId`（节点 ID）和 `uuid`（实例 UUID），填入对应实例的 `daemon_id`、`instance_uuid`。
5. `panel_url` 填面板地址，例如 `https://panel.example.com:23333`，不带 `/api` 和末尾斜杠。请使用 HTTPS，并直接填写最终地址（插件不跟随重定向）。

## 配置

在 WebUI 填写 `panel_url`、`api_key`，并在 `instances` 中添加至少一个实例，保存后重载插件。管理员身份以 AstrBot 的管理员配置为准。

### 全局配置

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `panel_url` / `api_key` | 空 | 面板地址与密钥 |
| `instances` | `[]` | 实例列表，见下表 |
| `allowed_platforms` | `["aiocqhttp"]` | 允许使用 `/mcs` 的平台；**不要加入 mcqq 的游戏平台** |
| `request_timeout_seconds` | `10` | 单次请求超时秒数 |
| `command_wait_seconds` | `1.5` | 发命令后等待输出的秒数，至少 `0.6` |
| `log_fetch_kb` | `64` | 每次读取的日志大小（KB） |
| `log_default_lines` / `log_max_lines` | `20` / `100` | `/mcs log` 默认行数和上限 |
| `log_mask_ip` | `true` | 日志与命令结果中隐藏 IP |
| `list_command` / `list_regex` | `list` / 见配置面板 | 在线玩家查询命令与解析正则 |
| `op_wait_seconds` | `240` | 启停操作最长等待秒数 |
| `alert_interval_seconds` | `30` | 告警轮询间隔秒数，至少 `10` |
| `platform_id` | 空 | 推送告警用的 QQ 适配器 ID；只有一个 aiocqhttp 适配器时可留空 |
| `llm_allowed_commands` | `["list", "neoforge tps", "spark tps", "spark health"]` | LLM 可执行的只读命令白名单；设为空列表即关闭 |

### 实例配置

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `alias` | 空 | 实例别名，例如 `生存`；不能含空格、不能叫 `all`，不能重复 |
| `daemon_id` / `instance_uuid` | 空 | 节点 ID 与实例 UUID |
| `groups` | `[]` | 绑定的 QQ 群号；一个群可绑定多个实例 |
| `tps_command` | `neoforge tps` | 查询 TPS 的命令，Paper 用 `tps`；留空则不查 |
| `tps_regex` | 见配置面板 | TPS 解析正则；换服务端时一起调整，并保持从行首锚定 |
| `alert_enabled` | `true` | 是否对该实例发送告警 |

### 谁能看到哪些实例

- QQ 群：只能访问绑定到本群的实例，管理员也一样。未绑定实例的群里，普通成员发 `/mcs` 不会收到回复。
- 私聊：管理员可访问全部实例，其他人不可访问。
- 游戏内（mcqq）：不能用 `/mcs`，只能通过 LLM 只读工具查询绑定了群的实例。
- 只有一个可见实例时可以省略别名；有多个时，`status`、`list` 不带别名会列出每个实例的摘要，其他指令必须指定别名。

## 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/mcs status [别名]` | 所有人 | 状态、在线人数、版本、CPU、内存、运行时长和 TPS |
| `/mcs list [别名]` | 所有人 | 在线玩家名单 |
| `/mcs log [别名] [行数] [关键词]` | 管理员 | 最近日志，可按关键词过滤，以合并转发发送 |
| `/mcs cmd [别名] <命令>` | 管理员 | 执行一条控制台命令并返回输出 |
| `/mcs start [别名]` | 管理员 | 启动实例，完成后通知 |
| `/mcs stop [别名] 确认` | 管理员 | 停止实例，在线玩家会掉线 |
| `/mcs restart [别名] 确认` | 管理员 | 重启实例，在线玩家会掉线 |
| `/mcs wl add\|remove [别名\|all] <玩家名>` | 管理员 | 添加或移除白名单，`all` 表示本群绑定的全部实例 |
| `/mcs wl list [别名]` | 管理员 | 查看白名单 |

示例：`/mcs log 生存 40 player joined`、`/mcs cmd 创造 say 服务器即将维护`、`/mcs wl add all Steve`。玩家名只能是 3–16 位字母、数字或下划线。

注意：

- `log`、`cmd` 的输出所有群成员都能看到，日志里可能有坐标、私聊等内容，建议只在私聊或管理群使用。
- `/mcs cmd 生存 stop` 会直接停服，不经过 `/mcs stop` 的确认。
- 控制台命令会出现在面板和反向代理的访问日志里，不要用 `cmd` 发送密码。
- 输出单行最多 500 字、总共最多 4000 字。日志轮转或同时有其他命令执行时，返回内容可能不完整或混入其他输出。

## 崩溃告警

插件加载后每隔 `alert_interval_seconds` 检查一次已绑定群的实例，向绑定的群推送：

- **意外停止**：服务器停了，日志里却没有正常关服的记录。
- **疑似崩溃后自动重启**：服务器在两次检查之间重启过，而且不是通过 `/mcs` 操作的。
- **面板失联**：连续 3 次读不到状态。
- **恢复**：上面几种情况恢复后各推送一次。

正常关服、通过 `/mcs` 发起的启停，以及插件加载前就已停止的服务器，都不会告警。告警只发摘要，不附带日志，详情请管理员用 `/mcs log` 查看。

## LLM 只读工具

| 工具 | 用途 |
| --- | --- |
| `mc_server_status` | 查询状态、在线人数与 TPS |
| `mc_online_players` | 查询在线玩家名单 |
| `mc_run_readonly_command` | 执行 `llm_allowed_commands` 中的命令 |

- 命令必须和白名单中的某一项完全一致（忽略大小写和多余空格），不能附加参数；需要参数就把完整命令写进白名单。
- **不要把 `spark profiler`、`op`、`stop` 等有副作用的命令加入白名单。**
- 同一实例的同一条命令 30 秒内只执行一次，期间返回缓存结果。
- 返回内容会去掉玩家聊天、进出服、成就、死亡信息等，并始终隐藏 IP。但模组自定义的聊天格式仍可能混进来，介意的话把 `llm_allowed_commands` 设为空列表；查状态和在线名单只返回解析结果，不受影响。

## 版本记录

### 0.2.1 – 0.2.3

- 安全加固：LLM 白名单命令的输出额外过滤玩家执行的命令、`/say`、成就、死亡信息和带方括号的物品名。

### 0.2.0

- 多实例与群绑定，指令支持别名。
- 新增白名单管理、崩溃告警、三个 LLM 只读工具。
- 安全修复：面板错误先脱敏再截断；LLM 返回内容过滤玩家聊天并隐藏 IP；玩家无法在聊天里伪造正常关服来压掉告警。
- **配置不兼容 0.1.x**：升级后需要在 `instances` 中重新填写实例，原来的 `daemon_id`、`instance_uuid`、`tps_command`、`tps_regex` 已移到实例配置里。

### 0.1.1

- 安全修复：在线列表与 TPS 解析从行首锚定，防止聊天伪造；输出先脱敏再截断。

### 0.1.0

- 首次发布：状态、在线玩家、TPS、日志、控制台命令、启停。

## 许可证

MIT，Copyright (c) 2026 Dracowyn。
