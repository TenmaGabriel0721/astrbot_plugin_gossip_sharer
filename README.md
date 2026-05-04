# 珈百璃的跨界传声筒 (Gossip Sharer)

✨ **让珈宝帮你跨界传话、打小报告的万能工具！**

基于 AstrBot 的跨 Session 消息转发插件，支持跨平台、跨群组、跨私聊的消息投递。

## 🌟 核心功能

- **跨界转发**：通过 `send_cross_message` 工具，珈宝可以把话带给任何人或群。
- **姐姐专属**：内置“给姐姐打小报告”快捷逻辑，受了委屈、发现好玩的秒传达。
- **安全白名单**：群聊转发受白名单保护，防止珈宝在不该说话的地方乱发。
- **保底提示**：连续多次没有主动转发后，会向 Bot 注入一条提示，提醒它按正常方式考虑是否调用 `send_cross_message` 给姐姐分享最近内容。
- **好友感知**：支持通过 `get_friend_list` 尝试读取 Bot 当前平台可用的好友列表；若平台不支持，则自动降级为说明文本。
- **环境感知**：支持查询当前可用的群聊白名单。

## ⚙️ 配置项

在 `_conf_schema.json` 或 Web 面板中配置：

| 配置项 | 类型 | 描述 |
| :--- | :--- | :--- |
| `default_platform` | string | 默认平台 ID (通常是 OneBot ID) |
| `sister_qq` | string | 姐姐的 QQ 号 (告状和保底提示的首选目标) |
| `group_whitelist` | list | 允许转发消息的群号列表 |
| `guarantee_threshold` | int | 保底阈值（连续多少次未触发主动转发后，注入一次“考虑给姐姐分享内容”的提示） |

> 当前版本统一使用 AstrBot 传入配置，不再额外读取本地 `config.json`，以避免与 Web 面板配置冲突。

## 🛠️ 工具说明

### `send_cross_message`
向指定私聊或群聊发送消息。

- **target_type**: `FriendMessage` (私聊) 或 `GroupMessage` (群聊)
- **target_id**: 目标 QQ 或群号
- **content**: 消息内容
- **target_platform**: (可选) 目标平台，默认使用 `default_platform`

> 注意：当 `target_type` 为 `GroupMessage` 时，目标群必须位于 `group_whitelist` 中。

### `get_available_groups`
获取当前允许转发消息的群聊白名单列表。

### `get_friend_list`
尝试获取当前 Bot 所在平台支持的好友列表。

- 如果平台实现了好友列表接口，则返回好友信息
- 如果平台未实现，则返回降级提示文本
- 姐姐 QQ (`sister_qq`) 始终可作为默认私聊目标参考

## 🤖 自动保底逻辑

插件会在装饰结果阶段统计“未发生主动转发”的次数：

- 每次事件经过该阶段时，计数 +1
- 只要调用 `send_cross_message` 成功发送，计数就会清零
- 当计数达到 `guarantee_threshold` 时：
  - 不会直接替 Bot 发消息
  - 而是返回一条提示文本
  - 提醒 Bot 按正常流程考虑是否调用 `send_cross_message`
  - 如果 Bot 判断当前上下文值得分享，就可以自然地给姐姐打小报告

也就是说，保底机制只负责“提醒 Bot 去考虑调用工具”，不直接代替 Bot 执行分享。

## 📦 安装

1. 将插件文件夹放入 `data/plugins/`
2. 重载插件或重启 AstrBot
3. 在 Web 面板中配置：
   - 默认平台 ID
   - 姐姐 QQ
   - 群白名单
   - 保底阈值

## 📌 当前实现说明

- 好友列表能力依赖具体平台适配器是否提供接口
- 群消息转发受白名单约束
- 私聊消息默认不做白名单限制
- 保底机制默认引导 Bot 优先考虑向 `sister_qq` 分享内容

---
*Created with ❤️ by 珈百璃*