# astrbot_plugin_gossip_sharer

基于 AstrBot 的跨 Session 消息转发插件，支持跨平台、跨群组、跨私聊投递，并可把转发内容写入目标会话上下文。

## 核心功能

- **跨会话转发**：通过 `send_cross_message` 工具向私聊或群聊发送消息。
- **私聊目标不限制**：`FriendMessage` 目标不做白名单限制。
- **群聊白名单**：`GroupMessage` 目标必须在群白名单内。
- **软白名单联动**：自动读取 `astrbot_plugin_soft_whitelist_config.json` 的 `group_whitelist`，并与本插件配置的 `group_whitelist` 合并去重。
- **保底提示**：连续多次没有主动转发后，在 LLM 请求阶段注入提示，让模型自行判断是否调用 `send_cross_message`。
- **列表查询**：支持尝试读取群列表和好友列表；平台不支持时返回降级说明。

## 配置项

在 `_conf_schema.json` 或 Web 面板中配置：

| 配置项 | 类型 | 描述 |
| :--- | :--- | :--- |
| `default_platform` | string | 默认平台 ID；不填写时使用内置默认值 `1207797855` |
| `sister_qq` | string | 姐姐的 QQ 号，作为告状和保底提示的首选私聊目标 |
| `group_whitelist` | list | 额外允许转发消息的群号列表，会与软白名单插件的群白名单合并 |
| `guarantee_threshold` | int | 连续多少次 LLM 请求未成功转发后，注入一次保底提示；小于等于 0 表示关闭 |

## 工具说明

### `send_cross_message`

向指定私聊或群聊发送消息。

- `target_type`: `FriendMessage` 或 `GroupMessage`
- `target_id`: 目标 QQ 或群号
- `content`: 消息内容
- `target_platform`: 可选，目标平台，默认使用 `default_platform`

当 `target_type` 为 `GroupMessage` 时，目标群必须在合并后的群白名单中；`FriendMessage` 不受该限制。

### `get_available_groups`

获取 Bot 当前可感知到的群聊列表，并标注哪些群在白名单中可用于转发。若平台无法读取群列表，则返回已配置的群白名单。

### `get_friend_list`

尝试获取当前 Bot 所在平台支持的好友列表。若平台未实现好友列表接口，则返回降级提示，`sister_qq` 仍可作为默认私聊目标参考。

## 自动保底逻辑

插件会在 LLM 请求阶段统计未发生主动转发的次数：

- 每次 LLM 请求计数 +1
- `send_cross_message` 成功发送后计数清零
- 达到 `guarantee_threshold` 时，把提示追加到 `system_prompt`
- 提示只引导模型考虑是否分享，不会直接替模型发送消息

## 安装

1. 将插件文件夹放入 `data/plugins/`
2. 重载插件或重启 AstrBot
3. 在 Web 面板中配置默认平台、姐姐 QQ、额外群白名单和保底阈值

## 当前实现说明

- 群白名单读取使用 `utf-8-sig`，可兼容带 BOM 的 AstrBot 配置文件
- 软白名单配置缺失或读取失败时，会继续使用本插件配置的 `group_whitelist`
- 私聊消息默认不做白名单限制
- 保底机制默认引导模型优先考虑向 `sister_qq` 分享内容
