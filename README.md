# 珈百璃的跨界传声筒 (Gossip Sharer)

✨ **让珈宝帮你跨界传话、打小报告的万能工具！**

基于 AstrBot 的跨 Session 消息转发插件，支持跨平台、跨群组、跨私聊的消息投递。

## 🌟 核心功能

- **跨界转发**：通过 `send_cross_message` 工具，珈宝可以把话带给任何人或群。
- **姐姐专属**：内置“给姐姐打小报告”快捷逻辑，受了委屈、发现好玩的秒传达。
- **安全白名单**：群聊转发受白名单保护，防止珈宝在不该说话的地方乱发。
- **保底机制**：太久没找姐姐告状？珈宝会自动触发告状冲动。
- **环境感知**：支持查询当前可用的群聊白名单和好友列表。

## ⚙️ 配置项

在 `_conf_schema.json` 或 Web 面板中配置：

| 配置项 | 类型 | 描述 |
| :--- | :--- | :--- |
| `default_platform` | string | 默认平台 ID (通常是 OneBot ID) |
| `sister_qq` | string | 姐姐的 QQ 号 (告状的首选目标) |
| `group_whitelist` | list | 允许转发消息的群号列表 |
| `guarantee_threshold` | int | 保底阈值 (连续多少次不触发转发后的强制提醒) |

## 🛠️ 工具说明

### `send_cross_message`
- **target_type**: `FriendMessage` (私聊) 或 `GroupMessage` (群聊)
- **target_id**: 目标 QQ 或群号
- **content**: 消息内容
- **target_platform**: (可选) 目标平台

## 📦 安装

1. 将插件文件夹放入 `data/plugins/`。
2. 重载插件或重启 AstrBot。
3. 在 Web 面板配置姐姐的 QQ 和群白名单。

---
*Created with ❤️ by 珈百璃*