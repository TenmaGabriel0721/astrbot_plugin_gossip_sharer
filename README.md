# astrbot_plugin_gossip_sharer

基于 AstrBot 的拟人化跨 Session 行动插件。Bot 可以主动进入目标 QQ 群聊或私聊，结合目标会话上下文自然说话、查询成员、At、调用工具，并自行决定是否携带图片或文件。

## 核心功能

- **统一跨会话行动**：LLM 通过 `wake_qq_session_task` 进入目标 QQ 群聊或私聊，走正常 AstrBot pipeline。
- **主动选择附件**：Bot 只有在 `image_refs` 或 `file_refs` 中明确选择时才会发送对应图片或文件。
- **回复后投递附件**：目标 LLM 先完成并发送自然回复，随后再投递选中的图片或文件，避免附件先到、文字长时间未到。
- **目标会话自主处理**：目标 LLM 可读取目标上下文，自行查询群成员、真实 At、调用群管理工具并自然回应。
- **安全附件引用**：当前消息与引用消息中的附件会获得 `image_1`、`file_1` 等短引用；生成文件只允许来自安全目录。
- **稳定图片快照**：来源 LLM 请求阶段即保存本轮图片内容，后续工具调用不再依赖容易被清理的 `media_image_*` 临时路径。
- **GIF 双轨处理**：来源和目标 LLM 只接收 GIF 第一帧 PNG，规避 Gemini 的 GIF MIME 限制；目标 QQ 仍收到原始动图。
- **私聊目标安全开关**：默认只允许向 `sister_qq` 私聊转发；如确需任意私聊目标，可开启 `enable_arbitrary_friend_targets`。
- **群聊白名单**：`GroupMessage` 目标必须在群白名单内。
- **软白名单联动**：自动读取 `astrbot_plugin_soft_whitelist_config.json` 的 `group_whitelist`，并与本插件配置的 `group_whitelist` 合并去重。
- **保底提示**：按来源会话独立统计连续未分享次数，达到阈值时提示 Bot 自主判断是否通过 wake 去姐姐私聊。
- **主动社交引导**：趣事、吐槽、告状、转达、邀请回应或附件分享都可由 Bot 自主发起，不必等待用户明确要求转发。
- **内部发送层**：原 `send_cross_message` 保留为插件内部图片、文件及故障兜底能力，不再暴露给 LLM。
- **列表查询**：支持尝试读取群列表、好友列表和目标群成员列表；平台不支持时返回降级说明。

## 配置项

在 `_conf_schema.json` 或 Web 面板中配置：

| 配置项 | 类型 | 描述 |
| :--- | :--- | :--- |
| `default_platform` | string | 默认平台 ID。留空时，调用工具必须显式传入 `target_platform`，避免误发到固定平台。 |
| `sister_qq` | string | 姐姐的 QQ 号，作为告状和保底提示的首选私聊目标。默认安全策略下，私聊只允许发给该 QQ。 |
| `group_whitelist` | list | 额外允许转发消息的群号列表，会与软白名单插件的群白名单合并。 |
| `enable_arbitrary_friend_targets` | bool | 是否允许 `FriendMessage` 发送到任意私聊目标。默认 `false`，即只允许发送给 `sister_qq`。 |
| `enable_target_session_tasks` | bool | 是否启用目标 QQ 群聊和私聊 LLM 唤醒。 |
| `enable_wake_images` | bool | 是否允许 wake 主动选择并发送图片。 |
| `enable_wake_files` | bool | 是否允许 wake 主动选择并发送文件。 |
| `allow_remote_attachment_urls` | bool | 是否允许工具参数直接使用 HTTP/HTTPS 附件 URL。 |
| `max_wake_images` | int | 单次 wake 最多处理的图片数量，`0` 表示禁止。 |
| `max_wake_files` | int | 单次 wake 最多处理的文件数量，`0` 表示禁止。 |
| `max_wake_image_mb` | int | 单张图片大小上限。 |
| `max_wake_file_mb` | int | 单个文件大小上限。 |
| `max_wake_total_mb` | int | 单次所有附件合计大小上限。 |
| `max_source_message_chars` | int | 自动提供给目标 LLM 的来源消息最大长度，`0` 表示不附带。 |
| `attachment_allowed_roots` | list | 除默认 temp/workspaces 外，允许发送本地生成文件的额外安全目录。 |
| `guarantee_threshold` | int | 每个来源会话连续多少次 LLM 请求未发起跨会话行动后提醒一次；触发后重新计数，默认 10，小于等于 0 表示关闭。 |
| `guarantee_injection_method` | string | 主动社交提醒的注入位置：`extra_user_content`（推荐）、`user_message_before` 或 `user_message_after`。 |

> 建议首次安装后手动配置 `default_platform` 与 `sister_qq`。插件不再内置固定平台 ID 或固定 QQ 号，避免复制部署时误发。

## 工具说明

### 内部发送能力

`send_cross_message` 从 v1.8.0 起不再注册为 LLM 工具。其底层发送逻辑仍由插件内部使用，负责 wake 选定附件后的真实 QQ 投递以及故障兜底。正常跨会话行为统一使用 `wake_qq_session_task`。

### `wake_qq_session_task`

把一条跨会话任务作为目标 QQ 群聊或私聊里的合成唤醒事件投递给目标会话 LLM。适合传话、打小报告、转述当前会话发生的事、请目标会话回应，以及让目标 Bot 结合对应会话上下文处理任务。

- `target_id`: 目标 QQ 群号或好友 QQ；群目标必须在群白名单中，私聊目标遵循私聊安全配置
- `task`: 交给目标会话 LLM 执行的自然语言任务，需要包含用户原意和必要上下文
- `target_type`: 支持 `GroupMessage` 和 `FriendMessage`，默认 `GroupMessage`
- `target_platform`: 可选，目标平台；不传时使用 `default_platform`
- `image_refs`: 可选，Bot 主动选择要发送的图片短引用、允许路径、URL 或 base64 引用
- `file_refs`: 可选，Bot 主动选择要发送的文件短引用、允许路径或 HTTP/HTTPS URL

选择边界：

- 所有正常跨会话行为统一使用 `wake_qq_session_task`。
- `wake_qq_session_task` 是 Bot 的主动社交能力；遇到值得告诉其他会话的内容时，可以结合人设和关系自主调用，不必等待用户明确说“转发”。
- `task` 要写清楚目标 LLM 应完成的行动；插件会自动补充来源会话、请求者和原始消息。
- 只有确实要把附件发过去时才传 `image_refs` 或 `file_refs`；未选择的附件不会自动发送。
- 当前消息和引用消息图片应使用 `image_1` 这类短引用，不要复用历史工具记录中的 `media_image_*` 临时绝对路径。
- At 和群管理不使用 wake 参数硬编码，由目标 LLM 查询目标群成员后自行调用工具。

示例：

```text
# 用户说“去群 984252223 解禁我”
target_id='984252223',
task='帮请求者解除禁言'

# 用户说“去群 984252223 禁言我 60 秒”
target_id='984252223',
task='禁言请求者 60 秒'

# 用户说“去群里说一句 xxx”
target_id='984252223',
task='向当前目标群发送：xxx'

# 用户说“去那个群打个小报告，说刚才 A 又在阴阳怪气”
target_id='984252223',
task='向当前目标群打小报告：刚才 A 又在阴阳怪气，请你根据目标群语境自然回应。'

# 用户说“去群里问问他们明天几点集合”
target_id='984252223',
task='询问当前目标群成员明天几点集合，并等待他们回应。'

# 用户说“去私聊问姐姐怎么看”
target_type='FriendMessage',
target_id='<姐姐QQ>',
task='请根据当前私聊上下文回应：你怎么看这件事？'

# 用户发了两张图，只要求把第二张给姐姐看
target_type='FriendMessage',
target_id='<姐姐QQ>',
task='把选中的图片给姐姐看，并自然询问她觉得怎么样。',
image_refs=['image_2']

# 把生成的报告交给目标群
target_id='984252223',
task='把这份报告交给当前群，并提醒大家查看。',
file_refs=['/允许目录/report.pdf']
```

群聊目标会构造一条发送者为原请求者 QQ、并 @ Bot 的合成群消息；私聊目标会使用目标好友 QQ 构造正确路由。选中的图片会先作为本次目标事件的一次性附件供 LLM 识别，但对目标 QQ 的真实图片/文件消息会等目标 LLM 回复发送完成后再投递。文件会向目标 LLM 提供名称、类型、大小和待投递状态。

当前消息及引用消息中的附件会自动获得稳定短引用，例如：

```text
image_1: 当前消息中的第一张图片
image_2: 引用消息中的图片
file_1: 当前消息中的第一个文件
```

模型也可传入工具生成的本地路径。非当前消息附件的本地路径必须位于 AstrBot `temp`、`workspaces` 或 `attachment_allowed_roots` 配置的目录中。

### `get_available_groups`

获取 Bot 当前可感知到的群聊列表，并标注哪些群在白名单中可用于转发。若平台无法读取群列表，则返回已配置的群白名单。

### `get_friend_list`

尝试获取当前 Bot 所在平台支持的好友列表。若当前平台未实现好友列表接口，则返回降级提示；已配置的 `sister_qq` 仍可作为默认私聊目标参考。

### `get_target_group_members`

获取指定白名单群的成员列表，用于转发前确认目标会话里应该 @ 谁。

- `target_id`: 目标群号，必须在群白名单中
- `target_platform`: 可选，目标平台；不传时使用 `default_platform`
- `keyword`: 可选，按 QQ、群名片或昵称过滤
- `limit`: 可选，最多展示多少名成员，默认 50，最大 200

## 自动保底逻辑

插件会在 LLM 请求阶段统计未发生主动转发的次数：

- 按来源会话（`event.unified_msg_origin`）独立计数。
- 每次 LLM 请求计数 +1。
- `wake_qq_session_task` 成功投递后，只清零当前来源会话的计数。
- 达到 `guarantee_threshold` 时，把一次性提醒追加到当前轮用户内容，不修改 `system_prompt`，避免破坏模型的系统提示缓存命中。
- 达到阈值后立即将当前会话计数归零，进入下一提醒周期。
- 默认阈值为 10；例如持续没有 wake 时，会在第 10、20、30 次请求分别重新提醒。
- 提示只引导模型考虑是否分享，不会直接替模型发送消息。
- 提醒注入位置可选择：`extra_user_content` 作为不落历史的临时用户内容；`user_message_before` 在本轮请求中放在用户原话前；`user_message_after` 在本轮请求中放在用户原话后。三种方式都不会把提醒写入长期历史。
- `extra_user_content` 最利于保持前缀缓存和用户原话完整，因此作为默认推荐。
- 附件短引用目录同样使用当前轮临时用户内容注入，不写入长期历史，也不动态修改系统提示。

## 附件处理与安全

1. 插件只处理 Bot 在 `image_refs`、`file_refs` 中主动选择的附件。
2. 图片在来源 LLM 请求阶段就转成一次性内存 base64 快照，先附加到目标合成事件供本次 LLM 识别，再在目标回复发送完成后真实发送到目标 QQ。
3. 文件会复制为本次目标事件专用的临时快照，并在目标回复发送完成后真实发送；第一版只向目标 LLM 提供文件元数据，不自动解析 PDF、Office 或压缩包正文。
4. 图片 base64、文件内容和临时路径都不会写入长期会话历史。
5. 当前消息与引用消息中的附件视为可信来源；其他本地路径必须通过安全目录校验。
6. 个别附件准备失败不会阻止文字任务，目标 LLM 会收到准确的待投递清单和准备失败状态；回复后的平台投递结果记录在日志中。
7. 回复后的附件发送带一次性标记，同一目标事件即使重复触发发送后钩子也不会重复投递。
8. 纯图片任务即使目标 Agent 最终回复为空，也会由 Agent 完成钩子直接投递附件，不依赖可能被跳过的普通消息发送阶段。
9. 目标 LLM 会被明确告知附件已由插件锁定，不应搜索、替换或调用其他发送工具重复投递。
10. GIF 会保留原始 base64 用于 QQ 投递，同时生成第一帧 PNG 作为来源和目标 LLM 的一次性识别输入；首帧转换失败时仍发送原 GIF，但不把不兼容的 GIF 交给 LLM。

## 安装

1. 将插件文件夹放入 `data/plugins/`。
2. 重载插件或重启 AstrBot。
3. 在 Web 面板中配置目标安全策略、附件开关、大小限制和额外允许目录。

## 当前实现说明

- 群白名单读取使用 `utf-8-sig`，可兼容带 BOM 的 AstrBot 配置文件。
- 软白名单配置缺失或读取失败时，会继续使用本插件配置的 `group_whitelist`。
- 私聊消息默认仅允许发给 `sister_qq`，开启 `enable_arbitrary_friend_targets` 后才允许任意私聊目标。
- 保底机制默认引导模型优先考虑向 `sister_qq` 分享内容。
- `send_cross_message` 保留为内部方法，不再出现在 LLM 工具列表中。
- 插件不加入跨会话跳数、循环追踪或长期 relay 状态机制。
