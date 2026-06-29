# astrbot_plugin_gossip_sharer

基于 AstrBot 的跨 Session 消息转发插件，支持跨平台、跨群组、跨私聊投递文字与图片，并可把转发内容写入目标会话上下文。

## 核心功能

- **跨会话转发**：通过 `send_cross_message` 工具向私聊或群聊发送消息。
- **图文转发**：支持纯文字、纯图片、图文混合消息；图片可来自 HTTP/HTTPS URL、本地文件路径或 base64。
- **目标群 At**：转发到群聊时可指定目标群成员 QQ，发送消息时在目标会话内真正 @ 对方；也支持 @全体成员。
- **目标会话 LLM 唤醒**：通过 `wake_qq_session_task` 把任务作为目标 QQ 群事件投递给目标会话 LLM，走正常 AstrBot pipeline。
- **私聊目标安全开关**：默认只允许向 `sister_qq` 私聊转发；如确需任意私聊目标，可开启 `enable_arbitrary_friend_targets`。
- **群聊白名单**：`GroupMessage` 目标必须在群白名单内。
- **软白名单联动**：自动读取 `astrbot_plugin_soft_whitelist_config.json` 的 `group_whitelist`，并与本插件配置的 `group_whitelist` 合并去重。
- **保底提示**：按来源会话独立统计连续未转发次数，达到阈值时在 LLM 请求阶段注入一次提示，让模型自行判断是否调用 `send_cross_message`。
- **目标上下文注入**：转发成功后，把图文桥接信息写入目标会话上下文，方便目标会话后续 LLM 理解来源与内容。
- **列表查询**：支持尝试读取群列表、好友列表和目标群成员列表；平台不支持时返回降级说明。

## 配置项

在 `_conf_schema.json` 或 Web 面板中配置：

| 配置项 | 类型 | 描述 |
| :--- | :--- | :--- |
| `default_platform` | string | 默认平台 ID。留空时，调用工具必须显式传入 `target_platform`，避免误发到固定平台。 |
| `sister_qq` | string | 姐姐的 QQ 号，作为告状和保底提示的首选私聊目标。默认安全策略下，私聊只允许发给该 QQ。 |
| `group_whitelist` | list | 额外允许转发消息的群号列表，会与软白名单插件的群白名单合并。 |
| `enable_arbitrary_friend_targets` | bool | 是否允许 `FriendMessage` 发送到任意私聊目标。默认 `false`，即只允许发送给 `sister_qq`。 |
| `enable_target_session_tasks` | bool | 是否启用目标 QQ 会话 LLM 任务唤醒工具。默认 `true`；群目标仍必须在白名单中。开启后任务会作为目标群事件进入正常 pipeline。 |
| `guarantee_threshold` | int | 连续多少次 LLM 请求未成功转发后，注入一次保底提示；按来源会话独立计数，小于等于 0 表示关闭。 |

> 建议首次安装后手动配置 `default_platform` 与 `sister_qq`。插件不再内置固定平台 ID 或固定 QQ 号，避免复制部署时误发。

## 工具说明

### `send_cross_message`

向指定私聊或群聊发送文字、图片或图文混合消息。

- `target_type`: `FriendMessage` 或 `GroupMessage`
- `target_id`: 目标 QQ 或群号
- `content`: 可选，消息文字内容
- `target_platform`: 可选，目标平台；不传时使用 `default_platform`
- `image_url`: 可选，HTTP/HTTPS 图片链接
- `image_path`: 可选，Bot 进程本地可读的图片路径
- `image_base64`: 可选，图片 base64 内容，可带 `data:image/...;base64,` 或 `base64://` 前缀
- `at_qqs`: 可选，目标群聊中要 @ 的 QQ 号列表；也兼容逗号或空格分隔字符串
- `at_names`: 可选，与 `at_qqs` 对应的显示名；QQ 平台通常会按 QQ 号自行解析
- `at_all`: 可选，是否 @全体成员，仅 `GroupMessage` 可用

至少需要提供 `content`、`image_url`、`image_path`、`image_base64`、`at_qqs`、`at_all` 之一。

示例：

```text
# 文字转发给姐姐
target_type='FriendMessage', target_id='<姐姐QQ>', content='姐姐姐姐，刚才群里有个瓜...'

# 转发网络图片给姐姐
target_type='FriendMessage', target_id='<姐姐QQ>', content='给你看这张图', image_url='https://example.com/a.jpg'

# 转发本地图片到白名单群
target_type='GroupMessage', target_id='<目标群号>', content='姐姐说看图', image_path='/path/to/image.jpg'

# 转发到白名单群并 @ 目标群成员
target_type='GroupMessage', target_id='<目标群号>', content='有人找你', at_qqs=['123456789']

# 转发到白名单群并 @ 全体成员
target_type='GroupMessage', target_id='<目标群号>', content='集合', at_all=true

# 转发 base64 图片
target_type='FriendMessage', target_id='<姐姐QQ>', image_base64='iVBORw0KGgoAAAANSUhEUg...'
```

当 `target_type` 为 `GroupMessage` 时，目标群必须在合并后的群白名单中。

`at_qqs` 和 `at_all` 仅支持 `GroupMessage`。如果不确定目标群成员 QQ，可先调用 `get_target_group_members` 查询目标群成员，再把成员 QQ 传给 `at_qqs`。

当 `target_type` 为 `FriendMessage` 时：

- 默认只允许发送给 `sister_qq`。
- 如需发送到任意私聊目标，需显式开启 `enable_arbitrary_friend_targets`。

### `wake_qq_session_task`

把一条跨会话任务作为目标 QQ 群里的合成唤醒事件投递给目标会话 LLM。推荐用于“去某个群里做某事”这类请求，也适合传话、打小报告、转述当前会话发生的事、请目标群回应、让目标群 Bot 处理群内事务等需要目标会话自己判断和执行的场景。

- `target_id`: 目标 QQ 群号，必须在群白名单中
- `task`: 交给目标会话 LLM 执行的自然语言任务，需要包含用户原意和必要上下文
- `target_type`: 当前只开放 `GroupMessage`
- `target_platform`: 可选，目标平台；不传时使用 `default_platform`

选择边界：

- 只是单向发送一段确定内容，不需要目标群 LLM 判断或回应时，用 `send_cross_message`。
- 需要目标群 LLM 结合目标群上下文、目标群工具和目标群权限来处理时，用 `wake_qq_session_task`。
- `task` 要写清楚完整任务；跨会话信息、要转述的话、打小报告的内容、请求者希望目标群怎么处理，都应直接写进 `task`。

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
```

该工具会构造一条目标 QQ 群里的合成消息事件，消息发送者为原请求者 QQ，并 @ Bot 唤醒目标会话 LLM。后续由 AstrBot 正常 waking/process/respond 流程处理：目标 LLM 的最终回复会自然发到目标群，目标会话配置的工具也按原规则可用。

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
- `send_cross_message` 成功发送后，只清零当前来源会话的计数。
- 达到 `guarantee_threshold` 时，把提示追加到 `system_prompt`。
- 达到阈值后只注入一次提示，不会在后续每次请求持续重复注入。
- 提示只引导模型考虑是否分享，不会直接替模型发送消息。

## 图片上下文与目标会话 LLM

`context.send_message` 只负责主动发送消息，不会天然触发目标会话的 LLM pipeline。当前插件默认策略：

1. 先把文字/图片消息发送到目标会话。
2. 再把桥接说明写入目标会话上下文。
3. 图片 URL 与 base64 会尽量以 OpenAI 兼容的 `image_url` content part 写入上下文；本地图片会记录本地路径并尝试写入 `file://` 引用。
4. 目标会话后续自然触发 LLM 时，可读取这段桥接上下文。是否能真正理解图片，取决于当前 provider 是否支持多模态历史。

如果需要立即让目标群 LLM 处理一项任务，使用 `wake_qq_session_task`；`send_cross_message` 只负责发送与上下文注入。

## 安装

1. 将插件文件夹放入 `data/plugins/`。
2. 重载插件或重启 AstrBot。
3. 在 Web 面板中配置默认平台、姐姐 QQ、额外群白名单、私聊目标开关和保底阈值。

## 当前实现说明

- 群白名单读取使用 `utf-8-sig`，可兼容带 BOM 的 AstrBot 配置文件。
- 软白名单配置缺失或读取失败时，会继续使用本插件配置的 `group_whitelist`。
- 私聊消息默认仅允许发给 `sister_qq`，开启 `enable_arbitrary_friend_targets` 后才允许任意私聊目标。
- 保底机制默认引导模型优先考虑向 `sister_qq` 分享内容。
- 图片转发复用 AstrBot 的 `MessageChain.url_image`、`file_image`、`base64_image` 能力。
