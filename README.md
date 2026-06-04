# astrbot_plugin_gossip_sharer

基于 AstrBot 的跨 Session 消息转发插件，支持跨平台、跨群组、跨私聊投递文字与图片，并可把转发内容写入目标会话上下文。

## 核心功能

- **跨会话转发**：通过 `send_cross_message` 工具向私聊或群聊发送消息。
- **图文转发**：支持纯文字、纯图片、图文混合消息；图片可来自 HTTP/HTTPS URL、本地文件路径或 base64。
- **私聊目标安全开关**：默认只允许向 `sister_qq` 私聊转发；如确需任意私聊目标，可开启 `enable_arbitrary_friend_targets`。
- **群聊白名单**：`GroupMessage` 目标必须在群白名单内。
- **软白名单联动**：自动读取 `astrbot_plugin_soft_whitelist_config.json` 的 `group_whitelist`，并与本插件配置的 `group_whitelist` 合并去重。
- **保底提示**：按来源会话独立统计连续未转发次数，达到阈值时在 LLM 请求阶段注入一次提示，让模型自行判断是否调用 `send_cross_message`。
- **目标上下文注入**：转发成功后，把图文桥接信息写入目标会话上下文，方便目标会话后续 LLM 理解来源与内容。
- **列表查询**：支持尝试读取群列表和好友列表；平台不支持时返回降级说明。

## 配置项

在 `_conf_schema.json` 或 Web 面板中配置：

| 配置项 | 类型 | 描述 |
| :--- | :--- | :--- |
| `default_platform` | string | 默认平台 ID。留空时，调用工具必须显式传入 `target_platform`，避免误发到固定平台。 |
| `sister_qq` | string | 姐姐的 QQ 号，作为告状和保底提示的首选私聊目标。默认安全策略下，私聊只允许发给该 QQ。 |
| `group_whitelist` | list | 额外允许转发消息的群号列表，会与软白名单插件的群白名单合并。 |
| `enable_arbitrary_friend_targets` | bool | 是否允许 `FriendMessage` 发送到任意私聊目标。默认 `false`，即只允许发送给 `sister_qq`。 |
| `guarantee_threshold` | int | 连续多少次 LLM 请求未成功转发后，注入一次保底提示；按来源会话独立计数，小于等于 0 表示关闭。 |
| `attempt_target_llm_trigger` | bool | 实验选项。默认 `false`，当前版本主要保证发送与上下文持久化；开启后也仅做 best-effort，不保证跨平台触发目标会话 LLM。 |

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

至少需要提供 `content`、`image_url`、`image_path`、`image_base64` 之一。

示例：

```text
# 文字转发给姐姐
target_type='FriendMessage', target_id='<姐姐QQ>', content='姐姐姐姐，刚才群里有个瓜...'

# 转发网络图片给姐姐
target_type='FriendMessage', target_id='<姐姐QQ>', content='给你看这张图', image_url='https://example.com/a.jpg'

# 转发本地图片到白名单群
target_type='GroupMessage', target_id='<目标群号>', content='姐姐说看图', image_path='/path/to/image.jpg'

# 转发 base64 图片
target_type='FriendMessage', target_id='<姐姐QQ>', image_base64='iVBORw0KGgoAAAANSUhEUg...'
```

当 `target_type` 为 `GroupMessage` 时，目标群必须在合并后的群白名单中。

当 `target_type` 为 `FriendMessage` 时：

- 默认只允许发送给 `sister_qq`。
- 如需发送到任意私聊目标，需显式开启 `enable_arbitrary_friend_targets`。

### `get_available_groups`

获取 Bot 当前可感知到的群聊列表，并标注哪些群在白名单中可用于转发。若平台无法读取群列表，则返回已配置的群白名单。

### `get_friend_list`

尝试获取当前 Bot 所在平台支持的好友列表。若当前平台未实现好友列表接口，则返回降级提示；已配置的 `sister_qq` 仍可作为默认私聊目标参考。

## 自动保底逻辑

插件会在 LLM 请求阶段统计未发生主动转发的次数：

- 按来源会话（`event.unified_msg_origin`）独立计数。
- 每次 LLM 请求计数 +1。
- `send_cross_message` 成功发送后，只清零当前来源会话的计数。
- 达到 `guarantee_threshold` 时，把提示追加到 `system_prompt`。
- 达到阈值后只注入一次提示，不会在后续每次请求持续重复注入。
- 提示只引导模型考虑是否分享，不会直接替模型发送消息。

## 图片上下文与目标会话 LLM

`context.send_message` 只负责主动发送消息，不会天然触发目标会话的 LLM pipeline。当前插件采用更可靠的默认策略：

1. 先把文字/图片消息发送到目标会话。
2. 再把桥接说明写入目标会话上下文。
3. 图片 URL 与 base64 会尽量以 OpenAI 兼容的 `image_url` content part 写入上下文；本地图片会记录本地路径并尝试写入 `file://` 引用。
4. 目标会话后续自然触发 LLM 时，可读取这段桥接上下文。是否能真正理解图片，取决于当前 provider 是否支持多模态历史。

`attempt_target_llm_trigger` 是实验配置，默认关闭。由于模拟目标会话事件涉及平台适配器差异，当前版本不把它作为可靠默认行为；即使开启，也不会影响消息发送与上下文持久化结果。

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
