# Changelog

## v1.3.0

- `send_cross_message` 支持转发到目标群聊时 @ 指定成员。
- 新增 `at_qqs`、`at_names`、`at_all` 参数；`at_qqs` 兼容列表、逗号分隔字符串和空格分隔字符串。
- 新增 `get_target_group_members` 工具，可查询白名单目标群成员，便于转发前确认要 @ 的 QQ。
- 目标会话上下文注入会记录本次转发的目标提及信息。
- 保持原有文字、图片、私聊安全策略、群白名单和软白名单联动行为不变。
