"""
抖音解析模块 - 无Cookie解析方式
基于 iesdouyin.com 分享页面的 window._ROUTER_DATA 提取视频/图集信息
"""
import asyncio
import json
import os
import random
import re
import time
from typing import AsyncGenerator, Optional

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .common import send_forward_message

# 移动端 Android Chrome UA —— 无需 Cookie 即可获取 SSR 数据
DOUYIN_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/116.0.0.0 Mobile Safari/537.36"
)

DOUYIN_HEADERS = {
    "User-Agent": DOUYIN_USER_AGENT,
    "Referer": "https://www.douyin.com/?is_from_mobile_home=1&recommend=1",
    "Accept-Encoding": "gzip, deflate",
}

# 临时目录设置
DATA_DIR = os.path.join(os.getcwd(), "data", "plugin_data", "astrbot_plugin_rconsole")
CACHE_DIR = os.path.join(DATA_DIR, "douyin_cache")

try:
    os.makedirs(CACHE_DIR, exist_ok=True)
except Exception as e:
    logger.error(f"创建缓存目录失败: {e}")


# ── 核心：从 HTML 中提取 window._ROUTER_DATA ──

def _extract_router_data(text: str) -> Optional[str]:
    """从 HTML 中提取 `window._ROUTER_DATA = {...}` 的 JSON 字符串。"""
    start_flag = "window._ROUTER_DATA = "
    start_idx = text.find(start_flag)
    if start_idx == -1:
        return None
    brace_start = text.find("{", start_idx)
    if brace_start == -1:
        return None
    idx = brace_start
    stack = []
    while idx < len(text):
        if text[idx] == "{":
            stack.append("{")
        elif text[idx] == "}":
            stack.pop()
            if not stack:
                return text[brace_start:idx + 1]
        idx += 1
    return None


# ── 网络请求 ──

async def _get_redirect_url(url: str) -> Optional[str]:
    """获取抖音短链接重定向后的真实 URL。"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=DOUYIN_HEADERS, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                return str(resp.url)
    except Exception as e:
        logger.error(f"获取重定向 URL 失败: {e}")
        return None


async def _fetch_douyin_info(item_id: str, is_note: bool = False) -> Optional[dict]:
    """
    通过 iesdouyin.com 分享页面，无 Cookie 获取抖音视频/图集信息。
    
    返回 dict:
        title       - 标题/描述
        author      - 作者昵称
        cover_url   - 封面图 URL（视频）
        video_url   - 无水印视频地址（视频）
        image_urls  - 图片 URL 列表（图集）
        is_gallery  - 是否为图集
    """
    if is_note:
        url = f"https://www.iesdouyin.com/share/note/{item_id}/"
    else:
        url = f"https://www.iesdouyin.com/share/video/{item_id}/"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=DOUYIN_HEADERS, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status >= 400:
                    logger.error(f"请求 iesdouyin 失败，状态码: {resp.status}")
                    return None
                html = await resp.text()

        json_str = _extract_router_data(html)
        if not json_str:
            logger.error("未能从 HTML 中提取到 _ROUTER_DATA")
            return None

        # 处理转义的 Unicode 和正斜杠
        json_str = json_str.replace("\\u002F", "/").replace("\\/", "/")
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"_ROUTER_DATA JSON 解析失败: {e}")
            return None

        # 在 loaderData 中寻找内容数据
        loader_data = data.get("loaderData", {})
        item_info = None
        for val in loader_data.values():
            if not isinstance(val, dict):
                continue
            for key in ("videoInfoRes", "noteDetailRes"):
                info = val.get(key)
                if info and info.get("item_list"):
                    item_info = info["item_list"][0]
                    break
            if item_info:
                break

        if not item_info:
            logger.error("_ROUTER_DATA 中未找到 item_list 数据")
            return None

        # 基本信息
        desc = item_info.get("desc", "无标题")
        author_info = item_info.get("author", {})
        nickname = author_info.get("nickname", "未知作者")

        # 封面图
        video_obj = item_info.get("video", {})
        cover_url = (
            video_obj.get("cover", {}).get("url_list", [None])[0]
            if video_obj else None
        )

        # 图集图片
        images = item_info.get("images") or []
        image_urls = []
        for img in images:
            url_list = img.get("url_list", [])
            if url_list:
                # 优先用 url_list[1]（较大尺寸），回退 url_list[0]
                image_urls.append(url_list[1] if len(url_list) > 1 else url_list[0])

        # 视频地址
        video_url = None
        if not images:
            play_addr = video_obj.get("play_addr", {})
            uri = play_addr.get("uri")
            if uri and isinstance(uri, str):
                if uri.startswith("https://"):
                    video_url = uri
                else:
                    video_url = f"https://www.douyin.com/aweme/v1/play/?video_id={uri}"

        return {
            "title": desc,
            "author": nickname,
            "cover_url": cover_url,
            "video_url": video_url,
            "image_urls": image_urls,
            "is_gallery": bool(image_urls),
        }

    except Exception as e:
        logger.error(f"获取抖音信息异常: {e}")
        return None


async def _try_fetch(item_id: str, prefer_note: bool = False) -> Optional[dict]:
    """尝试获取信息，如果 prefer_note 模式失败则回退到另一种模式。"""
    result = await _fetch_douyin_info(item_id, is_note=prefer_note)
    if result:
        return result
    # 回退尝试另一种类型
    return await _fetch_douyin_info(item_id, is_note=not prefer_note)


# ── 下载图片（图集） ──

async def _download_image(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """下载图片到缓存目录并返回本地路径。"""
    try:
        filename = f"img_{int(time.time())}_{random.randint(1000, 9999)}.jpg"
        filepath = os.path.join(CACHE_DIR, filename)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            with open(filepath, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024):
                    f.write(chunk)
        return filepath
    except Exception as e:
        logger.error(f"下载图片异常: {e}")
        return None


# ── 主入口 ──

async def process_douyin_url(event: AstrMessageEvent) -> AsyncGenerator:
    """
    处理抖音链接（无 Cookie 方式）。
    
    保留原始的消息回复格式：
      - 视频：封面图 + "识别：抖音\\n作者：...\\n标题：..." → 视频
      - 图集："识别：抖音\\n作者：...\\n标题：..." → 合并转发图片
    """
    msg: str = event.message_str.strip()
    logger.info(f"处理抖音链接: {msg}")

    # 匹配短链接
    reg = r"(http:|https:)\/\/v\.douyin\.com\/[A-Za-z\d._?%&+\-=#]*"
    douyin_match = re.search(reg, msg, re.I)
    if not douyin_match:
        yield event.plain_result("无法识别抖音链接")
        return

    douyin_url = douyin_match.group(0)

    try:
        # 解析短链接获取真实 URL
        real_url = await _get_redirect_url(douyin_url)
        if not real_url:
            yield event.plain_result("抖音短链接解析失败")
            return

        logger.debug(f"重定向后的 URL: {real_url}")

        # 判断是否为图集/笔记类型
        is_note_url = bool(re.search(r"/(note|slides)/", real_url))

        # 提取视频/笔记 ID
        id_match = re.search(r"/(?:video|note|slides)/(\d+)", real_url)
        if not id_match:
            yield event.plain_result("无法提取抖音视频/笔记 ID")
            return

        item_id = id_match.group(1)
        info = await _try_fetch(item_id, prefer_note=is_note_url)
        if not info:
            yield event.plain_result("抖音解析失败，无法获取内容详情")
            return

        author = info["author"]
        title = info["title"]
        cover_url = info["cover_url"]
        video_url = info["video_url"]
        image_urls = info["image_urls"]

        if info["is_gallery"]:
            # ── 图集（同原始回复格式） ──
            yield event.chain_result([
                Comp.Plain(f"识别：抖音\n作者：{author}\n标题：{title}")
            ])

            if image_urls:
                content_list = []
                content_list.append([
                    Comp.Plain(f"抖音 | {title}\n作者: {author}\n\n图集共 {len(image_urls)} 张图片")
                ])

                # 下载图片
                downloaded = []
                try:
                    async with aiohttp.ClientSession() as session:
                        tasks = []
                        for img_url in image_urls:
                            if img_url:
                                tasks.append(_download_image(img_url, session))
                        if tasks:
                            downloaded = await asyncio.gather(*tasks)
                except Exception as e:
                    logger.error(f"下载图集图片失败: {e}")

                for i, path in enumerate(downloaded):
                    if path:
                        content_list.append([
                            Comp.Image.fromFileSystem(path),
                            Comp.Plain(f"\n第 {i+1}/{len(downloaded)} 张"),
                        ])

                if content_list:
                    try:
                        yield await send_forward_message(event, content_list)
                    except Exception as e:
                        logger.error(f"发送合并转发消息失败: {e}")
                        yield event.plain_result("合并转发失败，单独发送图片...")
                        for i, path in enumerate(downloaded):
                            if path:
                                try:
                                    yield event.chain_result([
                                        Comp.Image.fromFileSystem(path),
                                        Comp.Plain(f"\n第 {i+1}/{len(downloaded)} 张"),
                                    ])
                                except Exception as e2:
                                    logger.error(f"发送单张图片失败: {e2}")

                # 清理临时文件
                for path in downloaded:
                    if path and os.path.exists(path):
                        try:
                            os.remove(path)
                        except Exception as e:
                            logger.error(f"删除临时文件失败: {e}")
            else:
                yield event.plain_result("无法获取图集图片")
        else:
            # ── 视频（同原始回复格式） ──
            if not video_url:
                yield event.plain_result("无法获取视频播放地址")
                return

            if cover_url:
                yield event.chain_result([
                    Comp.Image.fromURL(cover_url),
                    Comp.Plain(f"识别：抖音\n作者：{author}\n标题：{title}"),
                ])
            else:
                yield event.chain_result([
                    Comp.Plain(f"识别：抖音\n作者：{author}\n标题：{title}"),
                ])

            yield event.chain_result([Comp.Video.fromURL(video_url)])

    except Exception as e:
        logger.error(f"处理抖音链接失败: {e}")
        yield event.plain_result(f"处理抖音链接失败: {e}")
