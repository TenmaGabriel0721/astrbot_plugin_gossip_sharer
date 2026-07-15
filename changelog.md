# Changelog

## v1.8.1

- 保底提醒达到阈值后立即按来源会话重新计数，使主动分享提醒能够周期性出现，而不是每次加载后只触发一次。
- 默认和当前保底阈值调整为 10，并完善 WebUI 配置说明。
- 强化 `wake_qq_session_task` 工具描述，将其明确为 Bot 的主动跨会话社交能力，不必等待用户明确要求转发。
- 主动社交提示扩展到趣事、吐槽、告状、请求转达、邀请回应以及图片和文件分享，同时继续保留模型自主判断。
- 周期提醒和附件短引用目录改用当前轮临时用户内容注入，不再动态修改 `system_prompt`，提高模型提示缓存命中稳定性。
- 增加主动社交提醒注入位置配置，支持 `extra_user_content`、`user_message_before` 和 `user_message_after`，默认使用缓存友好的临时用户内容。
- 主动社交提醒增加“插件临时上下文、非用户原话、不要复述”的边界说明，降低用户轮次注入造成的误判。
- `user_message_before` 与 `user_message_after` 改用临时内容块重排，保留用户原话正常入库，同时避免内部提醒污染长期会话历史。

## v1.8.0

- 跨会话行为统一由 `wake_qq_session_task` 对外提供；`send_cross_message` 降为插件内部发送能力，不再注册为 LLM 工具。
- wake 新增 `image_refs` 与 `file_refs`，Bot 可主动决定是否发送以及选择哪些图片和文件，未选择的附件不会自动携带。
- 当前消息和引用消息附件自动生成 `image_1`、`file_1` 等短引用，并在来源 LLM 请求中提供可用附件目录。
- 选中图片会真实发送到目标 QQ，并作为一次性目标事件附件提供给目标 LLM 识别；不再写入长期历史。
- 文件支持群聊和私聊真实投递，并向目标 LLM 提供文件名、MIME 类型、大小与失败状态。
- 增加附件功能开关、远程 URL 开关、数量限制、单项大小、总大小、来源消息长度和额外安全目录配置。
- 保底分享逻辑改用 `wake_qq_session_task`，wake 成功后会清零当前来源会话计数。
- 目标任务自动携带原始来源消息，并改用更自然的“跨会话行动”提示。
- wake 附件改为目标 LLM 回复发送完成后再投递；图片仍作为一次性事件附件供目标 LLM 提前识别，文件使用独立临时快照并随目标事件清理。
- 回复后附件投递增加一次性标记，避免发送后钩子重复执行时重复发送。

## v1.7.1

- 优化 WebUI 配置展示：配置项改用简短标题，完整规则移动到 `hint`，避免长描述被界面截断。
- 目标唤醒配置提示已同步 QQ 群聊与私聊支持，并明确私聊安全开关同时作用于发送和唤醒工具。

## v1.7.0

- `wake_qq_session_task` 新增 QQ 私聊会话唤醒支持，可通过 `target_type='FriendMessage'` 委派任务。
- 私聊唤醒复用 `send_cross_message` 的安全策略：默认仅允许 `sister_qq`，开启 `enable_arbitrary_friend_targets` 后允许其他好友。
- 私聊合成事件使用目标好友 QQ 构造正确会话路由，同时在委派载荷与任务文本中保留真实请求者信息。

## v1.6.1

- 转发图片仍会正常发送到目标 QQ，但目标会话历史不再保存临时文件、网络 URL 或 base64 图片内容。
- 目标会话历史改为保存图片转发说明；需要再次识图时，可引用目标会话里的图片消息。
- 避免事件结束后临时图片被清理，失效的 `file://` 历史引用导致后续 LLM 请求失败。

## v1.6.0

- 精简 LLM 工具列表，移除旧的 `execute_qq_command` 直投指令工具和重复的 `request_group_unmute` 快捷工具。
- 移除 `enable_qq_command_execution` 与 `attempt_target_llm_trigger` 配置项；跨会话任务统一通过 `wake_qq_session_task` 唤醒目标群 LLM。
- `send_cross_message` 回归纯发送与上下文注入，不再支持发送后实验性触发目标会话 LLM。
- 扩展 `wake_qq_session_task` 工具说明，覆盖传话、打小报告、目标群回应和目标群事务处理等通用委派场景。

## v1.5.1

- `wake_qq_session_task` 改为投递目标 QQ 群合成唤醒事件，进入 AstrBot 正常 waking/process/respond pipeline。
- 目标 LLM 的最终回复会自然发送到目标群，不再要求使用 `send_message_to_user`。
- 移除目标任务提示里对 `execute_qq_command` 的强制引导；目标会话按自己的 LLM 流程和工具配置直接处理任务。

## v1.5.0

- 新增 `wake_qq_session_task` 工具，参考 AstrBot 未来任务的主动唤醒模式，把任务委派给目标 QQ 群会话 LLM 异步执行。
- 目标会话 agent 会读取目标会话上下文与工具配置；需要发送可见消息时使用 `send_message_to_user`，需要执行 QQAdmin/AstrBot 指令时使用 `execute_qq_command`。
- `execute_qq_command` 支持在被委派唤醒的目标会话内省略 `target_id`，并把 `at_self=true` 解析为原请求者 QQ。
- `request_group_unmute` 改为唤醒目标群 LLM 执行解禁任务，不再直接投递群指令。
- 新增 `enable_target_session_tasks` 配置开关，默认启用。

## v1.4.1

- 兼容新版 AstrBot tool loop 的 `ContextWrapper[AstrAgentContext]` 工具上下文，修复 `execute_qq_command` 报“无法识别当前来源平台”的问题。
- `send_cross_message`、列表查询工具也统一从工具上下文中解包真实消息事件。

## v1.4.0

- 新增 `execute_qq_command` 工具，可将 AstrBot 指令投递到指定 QQ 会话上下文执行。
- 新增 `request_group_unmute` 快捷工具，用于“去某群解禁我”，会在目标群中执行 `解禁 @请求者`。
- QQ 指令执行仅支持 OneBot/aiocqhttp；发起请求可来自 QQ 群聊或私聊。
- 群指令目标继续复用群白名单；私聊指令目标只能是请求者自己，避免伪造其他私聊身份。
- 新增 `enable_qq_command_execution` 配置开关，默认启用。
- 文档中明确 `attempt_target_llm_trigger` 只用于尝试触发目标会话 LLM，不是指令执行功能。

## v1.3.1

- `attempt_target_llm_trigger` 在 QQ OneBot/aiocqhttp 下开始实际生效：发送成功并写入目标上下文后，会投递目标会话虚拟事件，尝试立即触发目标会话 LLM。
- 虚拟事件会携带跨会话来源、目标会话、转发内容和图片附件，并跳过本插件的保底计数，避免二次触发循环。

## v1.3.0

- `send_cross_message` 支持转发到目标群聊时 @ 指定成员。
- 新增 `at_qqs`、`at_names`、`at_all` 参数；`at_qqs` 兼容列表、逗号分隔字符串和空格分隔字符串。
- 新增 `get_target_group_members` 工具，可查询白名单目标群成员，便于转发前确认要 @ 的 QQ。
- 目标会话上下文注入会记录本次转发的目标提及信息。
- 保持原有文字、图片、私聊安全策略、群白名单和软白名单联动行为不变。
