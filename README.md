# astrbot_plugin_gossip_sharer

基于 AstrBot 的跨会话协作插件。Bot 可以进入其他 QQ 群聊或私聊读取当地上下文、查证信息、找回原图或文件，再把实际结果送回发起会话，也可以在目标会话传话、分享和执行操作。

## 核心功能

- **统一跨会话行动**：LLM 通过 `wake_qq_session_task` 进入目标 QQ 群聊或私聊，走正常 AstrBot pipeline。
- **主动取回信息**：当前会话没有保存另一群的图片，或需要知道另一会话发生了什么时，工具描述会引导 Bot 主动委派查证，不必由用户提醒它唤醒目标会话。
- **固定回传路线**：`return_to_source=true` 时，目标是取材与执行位置，首次请求会话是最终收件人。嵌套委派不会把回传地址变成中间群聊。
- **真实原消息读取**：`get_qq_session_messages` 读取 OneBot 原消息，生成带会话、消息 ID 和时间来源的附件引用；可按原消息 ID 找回较早附件。
- **直接发送结果**：`reply_qq_session_task` 发送实际结果到固定来源；普通会话中可直接把查到的原图或文件发送到当前会话。
- **主动选择附件**：Bot 只有在 `image_refs` 或 `file_refs` 中明确选择时才会发送对应图片或文件。
- **原生合并聊天记录**：Bot 可选择已有 `forward_1` 合并记录，或把 `message_1`、`message_2` 等零散消息整理成一张 QQ 可展开的合并转发卡片。
- **两种输出方式**：分享任务在目标回复后投递选中附件；回传任务把选中附件作为资料，实际输出和工具生成的图片／文件返回来源，不公开到取材群。
- **目标会话自主处理**：目标 LLM 可读取目标上下文，自行查询群成员、真实 At、调用群管理工具并自然回应。
- **安全附件引用**：当前消息与引用消息中的附件会获得 `image_1`、`file_1` 等短引用；生成文件只允许来自安全目录。
- **稳定图片快照**：来源 LLM 请求阶段即保存本轮图片内容，后续工具调用不再依赖容易被清理的 `media_image_*` 临时路径。
- **GIF 双轨处理**：来源和目标 LLM 只接收 GIF 第一帧 PNG，规避 Gemini 的 GIF MIME 限制；目标 QQ 仍收到原始动图。
- **原图作为文件**：`file_refs` 可选择 `image_N`，以 QQ 文件发送原始图片字节，而非识图首帧。
- **稳定引用与重试**：并行读取不会覆盖附件编号；相同回传选择仅重试尚未成功的附件，保留原文件名和 MIME 类型。
- **私聊目标安全开关**：默认只允许向对应平台的姐姐（`sister_qq` / `official_sister_openid`）私聊转发；如确需任意私聊目标，可开启 `enable_arbitrary_friend_targets`。
- **群聊白名单**：OneBot 和 QQ 官方机器人的群白名单均为留空允许所有群，填写后只允许白名单内的群。
- **软白名单联动**：本插件 OneBot `group_whitelist` 留空时允许所有群；填写后读取 `astrbot_plugin_soft_whitelist_config.json` 的 `group_whitelist`，与本插件配置合并去重，两个来源中的群均可作为目标。
- **保底提示**：按来源会话独立统计连续未分享次数，达到阈值时提示 Bot 自主判断是否通过 wake 去姐姐私聊。
- **主动社交引导**：趣事、吐槽、告状、转达、邀请回应或附件分享都可由 Bot 自主发起，不必等待用户明确要求转发。
- **内部发送层**：原 `send_cross_message` 保留为插件内部图片、文件及故障兜底能力，不再暴露给 LLM。
- **列表查询**：支持尝试读取群列表、好友列表和目标群成员列表；平台不支持时返回降级说明。

## 配置项

在 `_conf_schema.json` 或 Web 面板中配置：

| 配置项 | 类型 | 描述 |
| :--- | :--- | :--- |
| `default_platform` | string | 可留空，默认跟随当前会话所在平台。填写后仅在与来源会话同一类适配器时生效，用于投递到同类的另一个平台实例。 |
| `sister_title` | string | 默认私聊对象的称呼，出现在保底提醒和好友列表中，默认“姐姐”。下文的“姐姐”均指这个称呼。 |
| `sister_qq` | string | OneBot 平台姐姐的 QQ 号，作为告状和保底提示的首选私聊目标。默认安全策略下，私聊只允许发给该 QQ。 |
| `group_whitelist` | list | OneBot 群白名单。留空允许所有群；填写后与软白名单合并，仅允许合并列表中的群。 |
| `official_sister_openid` | string | QQ 官方机器人平台姐姐的 `user_openid`，作用同 `sister_qq`。 |
| `official_group_whitelist` | list | QQ 官方机器人群白名单。留空允许所有群；填写后仅允许列出的 `group_openid`，与 OneBot 群白名单相互独立。 |
| `enable_arbitrary_friend_targets` | bool | 是否允许 `FriendMessage` 发送到任意私聊目标。默认 `false`，即只允许发送给对应平台的姐姐。 |
| `enable_target_session_tasks` | bool | 是否启用目标 QQ 群聊和私聊 LLM 唤醒。 |
| `enable_wake_images` | bool | 是否允许 wake 主动选择并发送图片。 |
| `enable_wake_files` | bool | 是否允许 wake 主动选择并发送文件。 |
| `enable_wake_forwards` | bool | 是否允许 wake 发送或整理 QQ 原生合并聊天记录。 |
| `allow_remote_attachment_urls` | bool | 是否允许工具参数直接使用 HTTP/HTTPS 附件 URL。 |
| `max_wake_images` | int | 单次 wake 最多处理的图片数量，`0` 表示禁止。 |
| `max_wake_files` | int | 单次 wake 最多处理的文件数量，`0` 表示禁止。 |
| `max_wake_forwards` | int | 单次 wake 最多处理的已有合并记录/零散消息来源数量，`0` 表示禁止，默认 10。多个来源会按顺序合并为一张卡片。 |
| `max_forward_nodes` | int | 单张合并聊天记录的节点上限，超过时该记录不发送。 |
| `max_wake_image_mb` | int | 单张图片大小上限。 |
| `max_wake_file_mb` | int | 单个文件大小上限。 |
| `max_wake_total_mb` | int | 单次所有附件合计大小上限。 |
| `max_source_message_chars` | int | 自动提供给目标 LLM 的来源消息最大长度，`0` 表示不附带。 |
| `attachment_allowed_roots` | list | 除默认 temp/workspaces 外，允许发送本地生成文件的额外安全目录。 |
| `guarantee_threshold` | int | 每个来源会话连续多少次 LLM 请求未发起跨会话行动后提醒一次；触发后重新计数，默认 10，小于等于 0 表示关闭。 |
| `guarantee_injection_method` | string | 主动社交提醒的注入位置：`extra_user_content`（推荐）、`user_message_before` 或 `user_message_after`。 |

> 建议首次安装后按实际使用的平台配置姐姐和群白名单。插件不内置固定平台 ID 或固定 QQ 号，避免复制部署时误发。

## 平台支持

插件同时支持 QQ OneBot(`aiocqhttp`) 与 QQ 官方机器人(`qq_official` / `qq_official_webhook`)。两类适配器可以在同一个 AstrBot 里同时启用，插件按来源会话所在平台各管各的：

- 唤醒目标默认就是来源会话所在的平台实例；显式指定的 `target_platform` 或 `default_platform` 必须与来源属于同一类适配器，OneBot 与 QQ 官方机器人之间不互相投递。
- 白名单和默认私聊对象分开配置：OneBot 使用 `group_whitelist`、`sister_qq`（QQ 号），QQ 官方机器人使用 `official_group_whitelist`、`official_sister_openid`（openid）。
- 合并聊天记录捕获、OneBot 接口调用只作用于 OneBot 平台；QQ 官方机器人的会话记录只作用于官方平台。
- 保底提醒按平台提示对应的姐姐。

### QQ 官方机器人说明

- 目标 ID 均为 openid：群用 `group_openid`，私聊用 `user_openid`。可在对应会话中发送 `/sid` 查看，或让 Bot 调用 `get_available_groups` / `get_friend_list`。
- 官方接口不提供群列表、好友列表、群成员列表和原始消息历史接口。`get_available_groups`、`get_friend_list` 会读取 AstrBot「对话」页面对应的已保存会话，合并白名单、默认私聊对象和本次运行中收到过消息的群与私聊；重启后仍能发现已有会话的对象。名称优先保留运行期已知群名或昵称，缺失时使用会话备注或自动名称，群名还可通过 `GET /v2/groups/{group_openid}/info` 补全。
- 历史会话按当前机器人平台 ID 筛选；同一群的多个对话和成员独立会话会合并为一个 `group_openid`。对话标题不作为群名或昵称，数字频道 ID 不作为 QQ 群或单聊 OpenID。当前 AstrBot 支持只读取会话元数据；旧版本缺少轻量接口时使用其会话列表接口。
- 这些记录是“已知群聊／私聊对象”，不等于完整好友或群列表；没有已保存对话的对象可能缺失。`official_group_whitelist` 留空时允许所有群，填写后群目标须在名单中；私聊仍遵循 `official_sister_openid` 和 `enable_arbitrary_friend_targets`。历史更新时间不代表本次运行收到过消息，也不保证当前能够发送。`get_target_group_members` 和 `get_qq_session_messages` 在官方会话中不提供给 LLM。
- 跨会话消息一律走主动推送，能否送达取决于平台频控和对方设置。普通分享模式的 `wake_qq_session_task` 会等待目标首次回复发出或发送失败，再把结果及 QQ 返回的原因（如对方没添加机器人、关闭了主动消息）交给 Bot。`return_to_source=true` 的回传任务异步入队，由目标通过 `reply_qq_session_task` 将结果送回固定来源；入队成功不代表结果已经发送。
- 不支持合并聊天记录，传入 `forward_refs` 会直接返回失败；图片和文件仍会投递，但群文件能否发出取决于平台是否开放该能力。
- 私聊目标开启流式输出时，回复依赖平台是否接受无 `msg_id` 的流式消息，建议对官方平台关闭流式输出。
- 只支持 QQ 群和单聊，不支持频道。

## 工具说明

### 内部发送能力

`send_cross_message` 从 v1.8.0 起不再注册为 LLM 工具。委派目标会话用 `wake_qq_session_task`，回传实际结果或发送到当前会话用 `reply_qq_session_task`。合并记录由内部 OneBot 投递层负责。

### `wake_qq_session_task`

委派目标会话里的 Bot 读取它的上下文、调用工具和处理任务。当前会话缺少另一会话的信息时，Bot 应主动想到这条路径；例如“那个群的原图发我看看”“查查他们刚才讨论了什么”。

`target_id` 是执行位置，`return_to_source` 决定结果去向：

- `true`：在目标会话查证，把实际文字、原图或文件送回首次请求会话。目标事件的流式输出关闭，直接工具输出也按固定路线回传。
- `false`：在目标会话自然说话、分享已选附件或执行操作。适合传话、告状、邀请回应和群内事务。

- `target_id`: 目标 QQ 群号或好友 QQ，QQ 官方机器人平台下为 `group_openid` / `user_openid`；群白名单留空时允许所有群，填写后群目标须在名单中，私聊目标遵循私聊安全配置
- `task`: 交给目标会话 LLM 执行的自然语言任务，需要包含用户原意和必要上下文
- `target_type`: 支持 `GroupMessage` 和 `FriendMessage`，默认 `GroupMessage`
- `target_platform`: 可选，目标平台，必须与当前会话属于同一类适配器；不传时使用当前会话所在平台
- `image_refs`: 可选，Bot 主动选择要发送的图片短引用、允许路径、URL 或 base64 引用
- `file_refs`: 可选，Bot 主动选择要发送的文件短引用、允许路径或 HTTP/HTTPS URL
- `forward_refs`: 可选，仅 OneBot 平台可用，选择当前提示中列出的 `forward_1`、`message_1` 等来源。多个来源会按顺序整理成一张原生 QQ 合并聊天记录。
- `return_to_source`: 可选，默认 `false`。需要信息、附件或结果回到发起会话时设为 `true`；嵌套委派继承首次回传地址。

选择边界：

- 信息不足时可主动取回信息，遇到值得分享的内容时可主动社交，不必等用户点名工具。
- `task` 要写清楚目标 LLM 应完成的行动；插件会自动补充来源会话、请求者和原始消息。
- 附件需明确选择。分享模式自动投递到目标；回传模式仅作为任务资料，目标通过 `reply_qq_session_task` 选择实际输出。
- 当前消息和引用消息图片应使用 `image_1` 这类短引用，不要复用历史工具记录中的 `media_image_*` 临时绝对路径。
- 历史中的图片路径失效时，通过 `get_qq_session_messages` 读取原消息，重新取得可靠附件引用；不会猜测某张最新图片就是所需原图。
- 当前消息、防抖窗口内的零散消息和引用消息会获得 `message_1`、`message_2` 等短引用；已有 QQ 合并转发会获得 `forward_1` 等短引用。只有显式传入 `forward_refs` 才会发送，原消息不会自动投递。
- `forward_refs=['forward_1']` 会尽量按原消息节点转发，目标 QQ 中可展开查看原聊天记录；选择多个已有记录或 `forward_refs=['message_1', 'message_2']` 时，会重新整理成一张带稳定标题和预览的新卡片。
- 从群聊整理出的合并记录即使最终私聊发送，也会显式标记为“群聊的聊天记录”，避免 QQ/NapCat 根据投递目标误显示成两个人的私聊记录。
- 构造型记录会生成来源标题、QQ 原生摘要和前几条消息预览，保留原消息时间。
- At 和群管理不使用 wake 参数硬编码，由目标 LLM 查询目标群成员后自行调用工具。
- 不能重复唤醒当前会话，或通过再次唤醒收件人代替回传。任务已提交仅代表入队，不能据此声称找到了原图或发送成功。

示例：

```text
# 私聊用户说“那个群的九宫格原图发我看看”
target_type='GroupMessage',
target_id='<来源群号>',
return_to_source=true,
task='根据当前群上下文定位那张九宫格原图，用 get_qq_session_messages 核实原消息，通过 reply_qq_session_task 回传原图。'

# 用户想了解另一个群已有的讨论
target_type='GroupMessage',
target_id='<来源群号>',
return_to_source=true,
task='查证群里关于这件事的近期讨论，把结论和关键依据回传发起会话。'

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

# 用户说“去那个群看看他们定了几点集合”
target_id='984252223',
return_to_source=true,
task='根据群内已有讨论查证集合时间，并回传确认依据；没有讨论过就如实说明。'

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

# 把已有合并记录原样转到目标群
target_id='984252223',
task='把这条聊天记录转到目标群，并自然说明这是请求者转来的内容。',
forward_refs=['forward_1']

# 把防抖窗口内的两条零散消息整理成一张可展开记录
target_id='984252223',
task='把刚才整理好的聊天记录发到目标群。',
forward_refs=['message_1', 'message_2']

```

群聊目标构造合成群消息；私聊目标使用目标好友构造路由，同时保留真实请求者和首次回传地址。图片提供一次性识图副本，附件目录保存原始字节和文件快照。分享模式在目标回复后发送；回传模式的结果送回来源。

当前消息及引用消息中的附件会自动获得稳定短引用，例如：

```text
image_1: 当前消息中的第一张图片
image_2: 引用消息中的图片
file_1: 当前消息中的第一个文件
forward_1: 当前消息中的已有合并聊天记录
message_1: 防抖窗口内第一条可作为合并节点的零散消息
message_2: 防抖窗口内第二条可作为合并节点的零散消息
```

模型也可传入工具生成的本地路径。非当前消息附件的本地路径必须位于 AstrBot `temp`、`workspaces` 或 `attachment_allowed_roots` 配置的目录中。

### `get_qq_session_messages`

读取 OneBot 真实群聊／私聊消息，返回消息 ID、发送者、时间、文字和原始附件短引用。默认读取当前实际会话；指定其他会话时遵守白名单、私聊策略和平台范围。

- `target_id`、`target_type`、`target_platform`：可选的读取目标，默认当前会话，独立于 `default_platform`。
- `limit`：默认 20，限制为 1～50 条。
- `before_message_id`：可选的原消息 ID；精确读取该消息并回溯较早记录。分页失败仍返回已核实的该条原消息，不替换成无关的新图片。

文件按平台文件 ID 补取下载地址并保留原文件名；远程 NapCat 的本机路径不可访问时，可使用平台提供的原图 URL 或原始 base64。QQ 官方机器人没有该历史接口。

### `reply_qq_session_task`

直接发送实际结果，收件地址由插件固定，参数不接受群号或好友号。委派任务回到首次请求会话；普通事件发到当前会话。无需再次唤醒收件人。

- `content`：可选文字，在选中的附件发送成功后发送。
- `image_refs`、`file_refs`、`forward_refs`：明确选择实际附件，参数意义与 wake 相同。`file_refs=['image_1']` 可把原图作为文件发送。

```text
# 已读取到原消息中的 image_1，回传原图
image_refs=['image_1']

# 以文件形式回传原动图，保留原始字节
file_refs=['image_1']
```

相同选择重复调用不会重复发送已成功的附件。发送失败返回分项结果，准备失败不会发送文字冒充附件；发送成功说明发送调用完成。

### `get_available_groups`

获取 Bot 当前可感知到的群聊列表，并标注可用于转发的群。群白名单留空时显示“允许所有群”，已知群均标为可转发；填写后只标记名单内的群。QQ 官方机器人使用当前平台的 AstrBot 历史会话、运行期记录和群白名单作为替代；其他平台无法读取群列表时返回当前群白名单策略。

### `get_friend_list`

尝试获取当前 Bot 所在平台支持的好友列表。若当前平台未实现好友列表接口，则返回降级提示；已配置的 `sister_qq` 仍可作为默认私聊目标参考。QQ 官方机器人平台返回当前平台的 AstrBot 历史私聊会话、默认私聊对象和本次运行中私聊过的用户。

### `get_target_group_members`

获取允许作为目标的群成员列表，用于转发前确认目标会话里应该 @ 谁。QQ 官方机器人平台不支持。

- `target_id`: 目标群号；群白名单留空时允许所有群，填写后须在名单中
- `target_platform`: 可选，目标平台，必须与当前会话属于同一类适配器；不传时使用当前会话所在平台
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

1. 插件处理 Bot 在 `image_refs`、`file_refs`、`forward_refs` 中明确选择的内容。回传模式的输入附件不会自动发到执行会话。
2. 当前图片在来源请求阶段保存一次性内存快照；历史附件通过平台原消息恢复，避免依赖失效路径。真实发送与识图副本分离。
3. 文件复制为独立临时快照，发送完成后清理。目标 LLM 得到文件元数据和可选引用，不自动解析 PDF、Office 或压缩包正文。
4. 图片 base64、文件内容和临时路径都不会写入长期会话历史。
5. 当前、引用和已核实平台原消息的附件保留来源；其他本地路径必须通过安全目录校验。
6. 任一明确选择的附件准备失败时，本次提交或回传失败，不降级为仅发送文字。实际发送按项记录结果，部分成功时只重试失败附件。
7. 回复后的附件发送带一次性标记，同一目标事件即使重复触发发送后钩子也不会重复投递。
8. 分享任务的纯附件空回复由 Agent 完成钩子投递；回传任务由专用工具或最终输出路由发送到来源。
9. 已选附件不应被搜索结果或无关新图片替换；取回历史原图时应核实原消息并选择其引用。
10. GIF 会保留原始 base64 用于 QQ 投递，同时生成第一帧 PNG 作为来源和目标 LLM 的一次性识别输入；首帧转换失败时仍发送原 GIF，但不把不兼容的 GIF 交给 LLM。
11. 已有合并记录优先使用 QQ 原消息节点 ID 投递；失败时改用节点快照重建。零散消息引用按顺序整理为 OneBot 合并转发。

## 安装

1. 将插件文件夹放入 `data/plugins/`。
2. 重载插件或重启 AstrBot。
3. 在 Web 面板中配置目标安全策略、附件开关、大小限制和额外允许目录。

## 当前实现说明

- 群白名单读取使用 `utf-8-sig`，可兼容带 BOM 的 AstrBot 配置文件。
- 本插件 `group_whitelist` 留空时允许所有 OneBot 群；填写后若软白名单配置缺失或读取失败，则使用本插件配置的群号。
- 私聊消息默认仅允许发给对应平台的姐姐，开启 `enable_arbitrary_friend_targets` 后才允许任意私聊目标。
- 保底机制默认引导模型优先考虑向当前平台的姐姐分享内容。
- `send_cross_message` 保留为内部方法，不再出现在 LLM 工具列表中。
- 插件不加入跨会话跳数、循环追踪或长期 relay 状态机制。
