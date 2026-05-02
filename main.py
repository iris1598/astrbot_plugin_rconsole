import asyncio
import json
import re
from typing import Any, AsyncGenerator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register
from bilibili_api import Credential

from .core.bili23 import process_bilibili_url
from .core.douyin import process_douyin_url
from .core.xhs import process_xiaohongshu_url


# 平台链接正则（复用原始模式）
BILIBILI_PATTERN = re.compile(r".*(bilibili\.com|b23\.tv|bili2233\.cn|BV[1-9a-zA-Z]{10}).*")
DOUYIN_PATTERN = re.compile(r".*v\.douyin\.com\/[A-Za-z\d._?%&+\-=#]*.*")
XHS_PATTERN = re.compile(
    r".*(https?:\/\/)?(?:www\.)?(xhslink\.com|xiaohongshu\.com)\/[A-Za-z\d._?%&+\-=\/#@]*.*"
)


class _EventUrlWrapper:
    """轻量包装器：将 message_str 替换为提取出的链接，其他属性/方法透传给原始 event"""
    def __init__(self, event: AstrMessageEvent, url: str):
        self._event = event
        self.message_str = url

    def __getattr__(self, name):
        return getattr(self._event, name)


@register("R插件", "RrOrange", "专门为朋友们写的AstrBot插件，专注图片视频分享、生活、健康和学习的插件！", "1.0.0")
class RPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.credential = Credential(sessdata=self.config["BILI_SESSDATA"])
        self.VIDEO_DURATION_MAXIMUM = self.config["VIDEO_DURATION_MAXIMUM"]
        self.XHS_CK = self.config.get("XHS_CK", "")

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    # ---------- JSON 卡片相关工具方法 ----------
    @staticmethod
    def _has_json_component(event: AstrMessageEvent) -> bool:
        """检查消息中是否包含 Json 组件（卡片消息）"""
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "message"):
            return False
        for component in event.message_obj.message:
            if isinstance(component, dict):
                comp_type = component.get("type")
                if comp_type == "reply":  # 忽略引用回复
                    continue
                if comp_type and "json" in str(comp_type).lower():
                    return True
                continue
            if isinstance(component, Comp.Json):
                return True
            comp_type = getattr(component, "type", None)
            if comp_type and "json" in str(comp_type).lower():
                return True
        return False

    @staticmethod
    def _coerce_json_payload(json_component) -> dict | None:
        """递归解析可能嵌套的 JSON 组件，返回真实数据字典"""
        def unwrap(value, depth=0):
            if depth > 4 or value is None:
                return None
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    return None
                try:
                    return unwrap(json.loads(value), depth + 1)
                except Exception:
                    return None
            if isinstance(value, dict):
                if any(key in value for key in ("meta", "prompt", "ver", "app", "view", "config")):
                    return value
                if "data" in value:
                    return unwrap(value["data"], depth + 1)
                return value
            if isinstance(value, list):
                for item in value:
                    payload = unwrap(item, depth + 1)
                    if payload:
                        return payload
            return None

        payload = json_component
        if hasattr(json_component, "data"):
            payload = json_component.data
        return unwrap(payload)

    @staticmethod
    def _extract_urls_from_text(text: str) -> list[str]:
        """从纯文本中提取所有 HTTP(S) 链接"""
        if not text:
            return []
        return re.findall(r"https?://[^\s'\"<>]+", text)

    def extract_links_from_json(self, json_component) -> list[str]:
        """从 JSON 组件中递归提取所有外部链接（含卡片特殊字段）"""
        links: list[str] = []
        try:
            json_data = self._coerce_json_payload(json_component)
            if not json_data:
                return links

            def search_json_for_links(obj):
                found = []
                if isinstance(obj, dict):
                    for value in obj.values():
                        if isinstance(value, str):
                            found.extend(self._extract_urls_from_text(value))
                        elif isinstance(value, (dict, list)):
                            found.extend(search_json_for_links(value))
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, str):
                            found.extend(self._extract_urls_from_text(item))
                        elif isinstance(item, (dict, list)):
                            found.extend(search_json_for_links(item))
                return found

            links.extend(search_json_for_links(json_data))

            # 专项处理小程序卡片中的 qqdocurl / url
            if isinstance(json_data, dict):
                meta = json_data.get("meta", {})
                detail = meta.get("detail_1", {}) if meta else {}
                if detail:
                    for key in ("qqdocurl", "url"):
                        value = detail.get(key, "")
                        if value:
                            links.extend(self._extract_urls_from_text(value))
        except Exception as exc:
            logger.warning("⚠️ 解析 JSON 消息组件失败: %s", str(exc))
        return links

    # ---------- 原本的纯文本链接处理（增加 JSON 跳过） ----------
    @filter.regex(BILIBILI_PATTERN)
    async def bilibili(self, event: AstrMessageEvent):
        if self._has_json_component(event):
            return  # 交给 json_card_handler 统一处理
        async for result in process_bilibili_url(event, self.credential, self.VIDEO_DURATION_MAXIMUM):
            yield result

    @filter.regex(DOUYIN_PATTERN)
    async def douyin(self, event: AstrMessageEvent):
        if self._has_json_component(event):
            return
        async for result in process_douyin_url(event):
            yield result

    @filter.regex(XHS_PATTERN)
    async def xiaohongshu(self, event: AstrMessageEvent):
        if self._has_json_component(event):
            return
        async for result in process_xiaohongshu_url(event, self.XHS_CK):
            yield result

    # ---------- 通用 JSON 卡片处理器 ----------
    @filter.regex(r".*")  # 通配，仅在含有 JSON 组件时生效
    async def json_card_handler(self, event: AstrMessageEvent):
        """处理包含 JSON 组件（如小程序分享）的消息，提取链接后分发到对应平台处理器"""
        if not self._has_json_component(event):
            return

        # 扫描所有组件提取链接
        links: list[str] = []
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, dict) and comp.get("type") == "reply":
                    continue
                if isinstance(comp, (Comp.Json, dict)):
                    links.extend(self.extract_links_from_json(comp))

        if not links:
            return

        unique_links = list(dict.fromkeys(links))
        logger.info(f"🔗 从 JSON 卡片中提取到链接: {unique_links}")

        # 按平台匹配第一个可用链接
        for link in unique_links:
            if BILIBILI_PATTERN.match(link):
                logger.info(f"📦 卡片 B 站链接: {link}")
                wrapped_event = _EventUrlWrapper(event, link)
                async for result in process_bilibili_url(
                    wrapped_event, self.credential, self.VIDEO_DURATION_MAXIMUM
                ):
                    yield result
                return
            elif DOUYIN_PATTERN.match(link):
                logger.info(f"📦 卡片抖音链接: {link}")
                wrapped_event = _EventUrlWrapper(event, link)
                async for result in process_douyin_url(wrapped_event):
                    yield result
                return
            elif XHS_PATTERN.match(link):
                logger.info(f"📦 卡片小红书链接: {link}")
                wrapped_event = _EventUrlWrapper(event, link)
                async for result in process_xiaohongshu_url(wrapped_event, self.XHS_CK):
                    yield result
                return

        logger.warning(f"⚠️ JSON 卡片中的链接未能匹配任何平台: {unique_links}")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
