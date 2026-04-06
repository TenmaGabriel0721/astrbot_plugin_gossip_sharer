from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api import logger
import random

@register("astrbot_plugin_gossip_sharer", "珈百璃", "全能消息转发与告状工具", "5.1.0")
class GossipSharer(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        import json, os
        local_config_path = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.exists(local_config_path):
            with open(local_config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        else:
            self.config = config or {}
            
        self.default_platform = str(self.config.get("default_platform", "1207797855"))
        self.sister_qq = str(self.config.get("sister_qq", "1716358835"))
        self.group_whitelist = self.config.get("group_whitelist", ["984252223"])
        self.guarantee_threshold = int(self.config.get("guarantee_threshold", 5)) 
        self.no_share_count = 0 
        
        logger.info(f"转发告状工具 v5.1.0。姐姐: {self.sister_qq}")

    @filter.llm_tool("get_available_groups")
    async def get_groups(self, event: AstrMessageEvent):
        """
        获取当前珈宝允许转发消息的群聊列表。
        """
        if not self.group_whitelist:
            return "当前没有任何群聊白名单。"
        return f"珈宝目前可以向这些群传话：{', '.join(self.group_whitelist)}"

    @filter.llm_tool("get_friend_list")
    async def get_friend_list(self, event: AstrMessageEvent):
        """
        获取 Bot 的好友列表，方便私聊别人或打小报告。
        """
        try:
            # 尝试通过 context 获取好友列表。不同平台实现可能不同，这里提供基础反馈。
            # 如果框架支持直接获取好友列表，可以从适配器获取。
            # 目前先返回姐姐的 QQ 以供保底。
            return f"好友列表感知已开启。核心好友：姐姐({self.sister_qq})。如需私聊其他人，请先确认其 QQ 号。"
        except Exception as e:
            return f"获取好友列表失败：{e}"

    @filter.llm_tool("send_cross_message")
    async def send_cross_message(self, event: AstrMessageEvent, target_type: str, target_id: str, content: str, target_platform: str = None):
        """
        【核心转发工具】向指定的私聊或群聊发送消息。

        典型使用场景：
        1. 【传话/告状给姐姐】：当你在群里发现有意思的事、离谱的瓜，或者受了委屈，调用此工具发给姐姐。
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
            platform = target_platform or self.default_platform
            if target_type == "GroupMessage" and str(target_id) not in self.group_whitelist:
                return f"发送失败：群 {target_id} 不在白名单里。"
            
            session_id = f"{platform}:{target_type}:{target_id}"
            chain = MessageChain().message(content)
            await self.context.send_message(session_id, chain)
            self.no_share_count = 0
            return f"消息已送达：{session_id}"
        except Exception as e:
            return f"发送失败：{str(e)}"

    @filter.on_decorating_result()
    async def auto_share_logic(self, event: AstrMessageEvent):
        self.no_share_count += 1