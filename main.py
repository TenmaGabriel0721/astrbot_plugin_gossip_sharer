from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api import logger


@register("astrbot_plugin_gossip_sharer", "珈百璃", "全能消息转发与告状工具", "1.1.1")
class GossipSharer(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)

        self.config = config or {}
        self.default_platform = str(self.config.get("default_platform", "1207797855"))
        self.sister_qq = str(self.config.get("sister_qq", "1716358835"))
        self.group_whitelist = [str(x) for x in self.config.get("group_whitelist", ["984252223"])]
        self.guarantee_threshold = int(self.config.get("guarantee_threshold", 5))
        self.no_share_count = 0

        logger.info(
            f"转发告状工具 v1.1.1 已加载。姐姐: {self.sister_qq}，"
            f"白名单群数量: {len(self.group_whitelist)}，保底阈值: {self.guarantee_threshold}"
        )

    def _build_session_id(self, target_type: str, target_id: str, target_platform: str = None) -> str:
        platform = str(target_platform or self.default_platform)
        return f"{platform}:{target_type}:{str(target_id)}"

    def _validate_target(self, target_type: str, target_id: str) -> str | None:
        if target_type not in ("FriendMessage", "GroupMessage"):
            return "发送失败：target_type 只允许为 FriendMessage 或 GroupMessage。"
        if target_type == "GroupMessage" and str(target_id) not in self.group_whitelist:
            return f"发送失败：群 {target_id} 不在白名单里。"
        return None

    def _build_bridge_history_pair(self, event: AstrMessageEvent, session_id: str, content: str) -> tuple[dict, dict]:
        source_session = getattr(event, "session", None) or getattr(event, "unified_msg_origin", "未知会话")
        source_platform = getattr(event, "get_platform_id", lambda: "未知平台")()
        source_sender = (
            getattr(event, "get_sender_name", lambda: None)()
            or getattr(event, "get_sender_id", lambda: None)()
            or "未知发送者"
        )
        user_message = {
            "role": "user",
            "content": (
                f"[跨会话转入]\n"
                f"来源会话: {source_session}\n"
                f"来源平台: {source_platform}\n"
                f"来源发送者: {source_sender}\n"
                f"目标会话: {session_id}\n"
                f"转发内容:\n{content}"
            ),
        }
        assistant_message = {
            "role": "assistant",
            "content": (
                "我已收到这条来自其他会话的转述消息。"
                "后续如果当前会话有人回复，应把它理解为对上面这条转述内容的继续回应，而不是一条完全无上下文的新话题。"
            ),
        }
        return user_message, assistant_message

    async def _persist_cross_context(self, event: AstrMessageEvent, session_id: str, content: str) -> None:
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            logger.warning("当前 Context 未提供 conversation_manager，跳过目标会话上下文注入")
            return

        cid = await conv_mgr.get_curr_conversation_id(session_id)
        if not cid:
            parts = session_id.split(":", 2)
            platform_id = parts[0] if len(parts) >= 3 else None
            cid = await conv_mgr.new_conversation(session_id, platform_id=platform_id)

        user_message, assistant_message = self._build_bridge_history_pair(event, session_id, content)
        await conv_mgr.add_message_pair(cid, user_message, assistant_message)
        logger.info(f"已将跨会话转发内容写入目标上下文: session={session_id}, cid={cid}")

    async def _safe_send(
        self,
        event: AstrMessageEvent,
        target_type: str,
        target_id: str,
        content: str,
        target_platform: str = None,
    ) -> str:
        target_id = str(target_id)
        error = self._validate_target(target_type, target_id)
        if error:
            return error

        session_id = self._build_session_id(target_type, target_id, target_platform)
        chain = MessageChain().message(content)
        sent = await self.context.send_message(session_id, chain)
        if not sent:
            return f"发送失败：未找到目标平台，session={session_id}"

        try:
            await self._persist_cross_context(event, session_id, content)
        except Exception as e:
            logger.warning(f"消息已发出，但写入目标会话上下文失败: {e}")

        self.no_share_count = 0
        return session_id

    async def _call_possible_async(self, method):
        result = method()
        if hasattr(result, "__await__"):
            result = await result
        return result

    async def _try_get_group_list(self, event: AstrMessageEvent | None = None):
        candidates = []

        if event is not None:
            bot = getattr(event, "bot", None)
            if bot is not None:
                candidates.append(getattr(bot, "get_group_list", None))

        candidates.extend([
            getattr(self.context, "get_group_list", None),
            getattr(getattr(self.context, "platform", None), "get_group_list", None),
            getattr(getattr(self.context, "provider", None), "get_group_list", None),
            getattr(getattr(self.context, "adapter", None), "get_group_list", None),
            getattr(getattr(self.context, "client", None), "get_group_list", None),
        ])

        for method in candidates:
            if not callable(method):
                continue
            try:
                result = await self._call_possible_async(method)
                if result is not None:
                    return result
            except Exception as e:
                logger.debug(f"尝试获取群列表失败: {e}")

        return None

    async def _try_get_friend_list(self, event: AstrMessageEvent | None = None):
        candidates = []

        if event is not None:
            bot = getattr(event, "bot", None)
            if bot is not None:
                candidates.append(getattr(bot, "get_friend_list", None))

        candidates.extend([
            getattr(self.context, "get_friend_list", None),
            getattr(getattr(self.context, "platform", None), "get_friend_list", None),
            getattr(getattr(self.context, "provider", None), "get_friend_list", None),
            getattr(getattr(self.context, "adapter", None), "get_friend_list", None),
            getattr(getattr(self.context, "client", None), "get_friend_list", None),
        ])

        for method in candidates:
            if not callable(method):
                continue
            try:
                result = await self._call_possible_async(method)
                if result is not None:
                    return result
            except Exception as e:
                logger.debug(f"尝试获取好友列表失败: {e}")

        return None

    def _unwrap_list_data(self, data):
        if isinstance(data, dict):
            for key in ("data", "groups", "friends", "list", "result"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return data

    def _format_group_list(self, group_data) -> str:
        if not group_data:
            return ""

        group_data = self._unwrap_list_data(group_data)

        if not isinstance(group_data, list):
            return f"已获取群列表信息，但数据结构暂不支持直接展示：{type(group_data).__name__}"

        if not group_data:
            return "当前群列表为空。"

        lines = []
        whitelist_set = set(self.group_whitelist)
        for item in group_data[:50]:
            if isinstance(item, dict):
                gid = (
                    item.get("group_id")
                    or item.get("group_code")
                    or item.get("id")
                    or "未知群号"
                )
                group_name = (
                    item.get("group_name")
                    or item.get("group_remark")
                    or item.get("name")
                    or "未知群名"
                )
                status = " [白名单可转发]" if str(gid) in whitelist_set else ""
                lines.append(f"- {group_name} ({gid}){status}")
            else:
                lines.append(f"- {str(item)}")

        extra = ""
        if len(group_data) > 50:
            extra = f"\n仅展示前 50 项，共 {len(group_data)} 项。"

        return "Bot 当前可感知到的群列表：\n" + "\n".join(lines) + extra

    def _format_friend_list(self, friend_data) -> str:
        if not friend_data:
            return ""

        friend_data = self._unwrap_list_data(friend_data)

        if not isinstance(friend_data, list):
            return f"已获取好友信息，但数据结构暂不支持直接展示：{type(friend_data).__name__}"

        if not friend_data:
            return "当前好友列表为空。"

        lines = []
        for item in friend_data[:50]:
            if isinstance(item, dict):
                uid = (
                    item.get("user_id")
                    or item.get("uin")
                    or item.get("qq")
                    or item.get("id")
                    or "未知ID"
                )
                nickname = (
                    item.get("nickname")
                    or item.get("remark")
                    or item.get("card")
                    or item.get("name")
                    or "未知昵称"
                )
                mark = " [姐姐]" if str(uid) == self.sister_qq else ""
                lines.append(f"- {nickname} ({uid}){mark}")
            else:
                lines.append(f"- {str(item)}")

        extra = ""
        if len(friend_data) > 50:
            extra = f"\n仅展示前 50 项，共 {len(friend_data)} 项。"

        return "Bot 当前可感知到的好友列表：\n" + "\n".join(lines) + extra

    def _build_guarantee_prompt(self) -> str:
        return (
            f"已经连续 {self.no_share_count} 次未主动分享消息。"
            f"请你现在根据当前上下文，给姐姐打个小报告。"
            f"如果要分享，请优先调用工具 `send_cross_message`，"
            f"参数建议为 target_type='FriendMessage'、target_id='{self.sister_qq}'，"
            "并把你认为值得分享的最近内容整理后发过去。"
        )

    @filter.llm_tool("get_available_groups")
    async def get_groups(self, event: AstrMessageEvent):
        """
        获取 Bot 当前可感知到的群聊列表，并标注哪些群在白名单中可用于转发。
        """
        try:
            group_data = await self._try_get_group_list(event)
            formatted = self._format_group_list(group_data)

            whitelist_tip = (
                f"\n当前群白名单：{', '.join(self.group_whitelist)}"
                if self.group_whitelist
                else "\n当前没有任何群聊白名单。"
            )

            if formatted:
                return formatted + whitelist_tip

            if self.group_whitelist:
                return (
                    "当前平台暂未提供可读取的群列表接口。"
                    f"不过已配置的可转发群白名单为：{', '.join(self.group_whitelist)}"
                )
            return "当前平台暂未提供可读取的群列表接口，且目前没有任何群聊白名单。"
        except Exception as e:
            return f"获取群列表失败：{e}"

    @filter.llm_tool("get_friend_list")
    async def get_friend_list(self, event: AstrMessageEvent):
        """
        获取 Bot 当前可感知到的好友列表；若当前平台不支持，则返回降级说明。
        """
        try:
            friend_data = await self._try_get_friend_list(event)
            formatted = self._format_friend_list(friend_data)
            if formatted:
                return formatted
            return (
                f"当前平台暂未提供可读取的好友列表接口。"
                f"不过姐姐({self.sister_qq}) 仍可作为默认私聊目标。"
            )
        except Exception as e:
            return f"获取好友列表失败：{e}"

    @filter.llm_tool("send_cross_message")
    async def send_cross_message(
        self,
        event: AstrMessageEvent,
        target_type: str,
        target_id: str,
        content: str,
        target_platform: str = None,
    ):
        """
        【核心转发工具】向指定的私聊或群聊发送消息。

        典型使用场景：
        1. 【传话/告状给姐姐】：当你在群里发现有意思的事、离谱的瓜，或者受了委屈以及定期地分享信息给姐姐（概率10%），调用此工具发给姐姐。
           (target_type='FriendMessage', target_id='1716358835', content='姐姐姐姐，刚才群里那个人...')
        2. 【请教指示】：遇到拿不准的事，私聊请教姐姐。
        3. 【传达圣旨】：将姐姐的回复或指示转达到目标群聊中。
           (target_type='GroupMessage', target_id='目标群号', content='姐姐说了，让你们老实点！')

        Args:
            target_type (str): 消息类型。'FriendMessage' (私聊) 或 'GroupMessage' (群聊)。
            target_id (str): 接收目标的 QQ 号或群号。若填“1716358835”即为发给姐姐。
            content (str): 要发送的完整内容。
            target_platform (str): 平台 ID。默认使用配置值。
        """
        try:
            session_id = await self._safe_send(event, target_type, target_id, content, target_platform)
            if session_id.startswith("发送失败："):
                return session_id
            return f"消息已送达：{session_id}"
        except Exception as e:
            return f"发送失败：{str(e)}"

    @filter.on_decorating_result()
    async def auto_share_logic(self, event: AstrMessageEvent):
        self.no_share_count += 1

        if self.guarantee_threshold <= 0:
            return

        if self.no_share_count < self.guarantee_threshold:
            return

        self.no_share_count = 0
        prompt = self._build_guarantee_prompt()
        logger.info("已触发保底提示，提示 Bot 自主决定是否调用 send_cross_message")
        return prompt