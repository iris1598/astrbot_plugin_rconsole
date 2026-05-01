import re
import os
import json
import httpx
import asyncio
import aiohttp
import time
import random
from typing import Any, Dict, List, AsyncGenerator, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from .common import delete_boring_characters, remove_files, send_forward_message, send_images_sequentially

# Constants
XHS_REQ_LINK = "https://www.xiaohongshu.com/explore/"
COMMON_HEADER = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# 数据目录和缓存目录
DATA_DIR = os.path.join(os.getcwd(), "data")
CACHE_DIR = os.path.join(DATA_DIR, "xhs_cache")

try:
    os.makedirs(CACHE_DIR, exist_ok=True)
    logger.info(f"确保小红书缓存目录存在: {CACHE_DIR}")
except Exception as e:
    logger.error(f"创建目录失败: {e}")


async def download_img(url: str, path: str, session: Optional[aiohttp.ClientSession] = None) -> str:
    if session:
        async with session.get(url) as response:
            with open(path, 'wb') as fd:
                async for chunk in response.content.iter_chunked(1024):
                    fd.write(chunk)
    else:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                with open(path, 'wb') as fd:
                    async for chunk in response.content.iter_chunked(1024):
                        fd.write(chunk)
    return path


async def download_video(url: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    filename = f"xhs_video_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
    path = os.path.join(CACHE_DIR, filename)
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            with open(path, 'wb') as fd:
                async for chunk in response.content.iter_chunked(1024):
                    fd.write(chunk)
    return path


def save_to_cache(note_id: str, data: Dict[str, Any]) -> None:
    cache_path = os.path.join(CACHE_DIR, f"{note_id}.json")
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        cache_data = {
            "timestamp": int(time.time()),
            "data": data
        }
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        logger.info(f"小红书数据已缓存: {note_id}")
    except Exception as e:
        logger.error(f"保存小红书缓存失败: {e}")


def get_from_cache(note_id: str, max_age: int = 86400) -> Optional[Dict[str, Any]]:
    cache_path = os.path.join(CACHE_DIR, f"{note_id}.json")
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
        timestamp = cache_data.get("timestamp", 0)
        if int(time.time()) - timestamp > max_age:
            return None
        return cache_data.get("data")
    except Exception as e:
        logger.error(f"读取小红书缓存失败: {e}")
        return None


def _extract_initial_state(html: str) -> dict:
    """
    从HTML中提取 window.__INITIAL_STATE__ 的JSON数据。
    先尝试正则，失败后使用括号计数兜底（支持多行JSON）。
    """
    # 方法1：正则（简单情况）
    pattern = r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>'
    match = re.search(pattern, html, re.DOTALL)
    if match:
        json_str = match.group(1)
        json_str = re.sub(r'\bundefined\b', 'null', json_str)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # 方法2：括号计数兜底
    start_marker = 'window.__INITIAL_STATE__'
    start_idx = html.find(start_marker)
    if start_idx == -1:
        raise RuntimeError("无法找到 window.__INITIAL_STATE__ 数据")

    json_start = html.find('{', start_idx)
    if json_start == -1:
        raise RuntimeError("无法找到JSON开始位置")

    script_end = html.find('</script>', start_idx)
    if script_end == -1:
        script_end = len(html)

    brace_count = 0
    json_end = json_start
    in_string = False
    escape_next = False
    in_single_quote = False

    for i in range(json_start, min(script_end, len(html))):
        char = html[i]
        if escape_next:
            escape_next = False
            continue
        if char == '\\':
            escape_next = True
            continue
        if char == '"' and not in_single_quote:
            in_string = not in_string
            continue
        if char == "'" and not in_string:
            in_single_quote = not in_single_quote
            continue
        if not in_string and not in_single_quote:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    json_end = i + 1
                    break

    if brace_count != 0:
        raise RuntimeError("无法找到完整的JSON对象")

    json_str = html[json_start:json_end]
    json_str = re.sub(r'\bundefined\b', 'null', json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON解析失败: {e}")


def _parse_note_data(data: dict, url: str = "") -> dict:
    """
    从 window.__INITIAL_STATE__ JSON 中提取笔记信息。
    同时支持移动端路径 (noteData.data.noteData) 和 PC 端路径 (note.noteDetailMap)。
    图片提取使用原版方式（只取 urlDefault）。
    """
    note_data = None
    user_data = {}

    # 移动端路径
    try:
        note_data = data["noteData"]["data"]["noteData"]
        user_data = note_data.get("user", {})
    except (KeyError, TypeError):
        pass

    # PC 端路径
    if not note_data:
        try:
            note_detail_map = data.get("note", {}).get("noteDetailMap", {})
            for detail in note_detail_map.values():
                potential = detail.get("note")
                if potential and isinstance(potential, dict):
                    note_data = potential
                    user_data = note_data.get("user", {})
                    break
        except (KeyError, TypeError):
            pass

    if not note_data:
        raise RuntimeError("无法找到笔记数据（移动端和PC端路径均失败）")

    note_type = note_data.get("type", "normal")
    title = note_data.get("title", "") or ""
    desc = note_data.get("desc", "") or ""

    # 作者：nickName 优先，再 nickname
    author_name = ""
    author_id = ""
    if user_data:
        author_name = user_data.get("nickName") or user_data.get("nickname") or ""
        author_id = user_data.get("userId", "")

    # 发布时间
    timestamp = note_data.get("time", 0)
    if timestamp:
        dt = datetime.fromtimestamp(timestamp / 1000)
        publish_time = dt.strftime("%Y-%m-%d %H:%M")
    else:
        publish_time = ""

    # 图片：原版方式，只取 urlDefault，返回 dict 列表
    image_list = []
    cover_url = ""
    for idx, img in enumerate(note_data.get('imageList', [])):
        if 'urlDefault' in img and img['urlDefault']:
            if idx == 0:
                cover_url = img['urlDefault']
            image_list.append({
                'url': img['urlDefault'],
                'width': img.get('width', 0),
                'height': img.get('height', 0)
            })

    # 视频信息
    video_info = {}
    if note_type == "video":
        vi = note_data.get("video", {})
        video_url = ""
        if vi and "media" in vi:
            h264 = vi["media"].get("stream", {}).get("h264", [])
            if h264:
                video_url = h264[0].get("masterUrl", "")
        if not video_url:
            video_url = vi.get("url", "") if isinstance(vi, dict) else ""
        if video_url.startswith("http://"):
            video_url = video_url.replace("http://", "https://", 1)
        elif video_url.startswith("//"):
            video_url = "https:" + video_url
        # 封面
        cov = ""
        if isinstance(vi, dict):
            c = vi.get("cover")
            if isinstance(c, dict):
                cov = c.get("url", "") or ""
            elif isinstance(c, str):
                cov = c
        if not cov:
            cov = vi.get("coverUrl", "") if isinstance(vi, dict) else ""
        if cov.startswith("//"):
            cov = "https:" + cov
        elif cov.startswith("http://"):
            cov = cov.replace("http://", "https://", 1)
        if cov:
            cover_url = cov
        video_info = {"url": video_url, "cover": cov}

    # 清理话题标签
    if desc:
        desc = re.sub(r'#([^#\[]+)\[话题\]#', r'#\1', desc)

    return {
        "type": note_type,
        "title": title,
        "desc": desc,
        "author_name": author_name,
        "author_id": author_id,
        "publish_time": publish_time,
        "images": image_list,
        "video": video_info,
        "cover_url": cover_url,
    }


async def process_xiaohongshu_url(event: AstrMessageEvent, xhs_ck: str, enable_forward: bool = True) -> AsyncGenerator[Any, None]:
    if not xhs_ck:
        yield event.plain_result("无法获取到小红书Cookie，请在配置中设置XHS_CK")
        return

    message_str = event.message_str.strip()
    msg_url_match = re.search(
        r"(https?:\/\/)?(?:www\.)?(xhslink\.com|xiaohongshu\.com)\/[A-Za-z\d._?%&+\-=\/#@]*",
        message_str
    )
    if not msg_url_match:
        return

    msg_url = msg_url_match.group(0)
    if not msg_url.startswith("http"):
        msg_url = "https://" + msg_url

    # 短链展开
    if "xhslink" in msg_url:
        try:
            headers = {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'cookie': xhs_ck,
                'User-Agent': COMMON_HEADER['User-Agent']
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(msg_url, headers=headers, allow_redirects=False) as resp:
                    if resp.status == 302:
                        msg_url = unquote(resp.headers.get("Location", msg_url))
                    else:
                        async with httpx.AsyncClient() as client:
                            r = await client.get(msg_url, headers=headers, follow_redirects=True)
                            msg_url = str(r.url)
        except Exception as e:
            yield event.plain_result(f"解析小红书短链失败: {str(e)}")
            return

    # 提取笔记ID
    xhs_id = None
    for pattern in [r'/explore/(\w+)', r'/discovery/item/(\w+)', r'source=note&noteId=(\w+)']:
        m = re.search(pattern, msg_url)
        if m:
            xhs_id = m.group(1)
            break
    if not xhs_id:
        yield event.plain_result("无法从链接中提取小红书ID")
        return

    # 尝试缓存
    note_data = get_from_cache(xhs_id)

    if not note_data:
        # 解析URL参数
        parsed_url = urlparse(msg_url)
        params = parse_qs(parsed_url.query)
        xsec_source = params.get('xsec_source', ['pc_feed'])[0]
        xsec_token = params.get('xsec_token', [None])[0]

        headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
            'cookie': xhs_ck,
            'User-Agent': COMMON_HEADER['User-Agent']
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f'{XHS_REQ_LINK}{xhs_id}?xsec_source={xsec_source}&xsec_token={xsec_token}',
                    headers=headers
                )
                html = response.text
        except Exception as e:
            yield event.plain_result(f"请求小红书内容失败: {str(e)}")
            return

        try:
            initial_state = _extract_initial_state(html)
        except RuntimeError as e:
            yield event.plain_result(f"解析小红书页面失败: {str(e)}（Cookie可能已失效）")
            return

        try:
            note_data = _parse_note_data(initial_state, msg_url)
            save_to_cache(xhs_id, note_data)
        except RuntimeError as e:
            yield event.plain_result(f"解析小红书笔记数据失败: {str(e)}")
            return

    if not note_data:
        yield event.plain_result("无法获取小红书内容")
        return

    # 提取字段（无互动数据）
    content_type = note_data.get("type", "")
    note_title = note_data.get("title", "") or "无标题"
    note_desc = note_data.get("desc", "")
    author_name = note_data.get("author_name", "") or "未知作者"
    publish_time = note_data.get("publish_time", "")
    cover_url = note_data.get("cover_url", "")

    # 构建简介文本（无👍💬⭐）
    info_lines = [f"识别：小红书\n标题：{note_title}"]
    if note_desc:
        info_lines.append(f"描述：{note_desc}")
    info_lines.append(f"作者：{author_name}")
    if publish_time:
        info_lines.append(f"发布时间：{publish_time}")
    info_text = "\n".join(info_lines)

    if content_type == "normal":
        # 图片帖子（原版方式：image_list 是 dict 列表）
        image_list = note_data.get("images", [])
        if not image_list:
            yield event.plain_result(info_text + "\n（未找到图片内容）")
            return

        os.makedirs(CACHE_DIR, exist_ok=True)

        # 下载图片（原版方式：item['url']）
        image_paths = []
        async with aiohttp.ClientSession() as session:
            download_tasks = []
            for index, item in enumerate(image_list):
                image_url = item.get('url', '')
                if not image_url:
                    continue
                path = os.path.join(CACHE_DIR, f"xhs_{xhs_id}_{index}.jpg")
                download_tasks.append(asyncio.create_task(
                    download_img(image_url, path, session=session)))
            if download_tasks:
                image_paths = await asyncio.gather(*download_tasks)

        if not image_paths:
            yield event.plain_result(info_text + "\n（图片下载失败）")
            return

        # 构建内容列表
        content_list = []
        intro_comps = []
        if cover_url:
            intro_comps.append(Comp.Image.fromURL(cover_url))
        intro_comps.append(Comp.Plain(info_text))
        content_list.append(intro_comps)

        for i, path in enumerate(image_paths):
            content_list.append([
                Comp.Image.fromFileSystem(path),
                Comp.Plain(f"\n第 {i+1}/{len(image_paths)} 张"),
            ])

        if enable_forward:
            yield await send_forward_message(event, content_list)
        else:
            async for result in send_images_sequentially(event, content_list, None):
                yield result

        remove_files(image_paths)

    elif content_type == "video":
        video_info = note_data.get("video", {})
        video_url = video_info.get("url", "") if isinstance(video_info, dict) else ""

        if not video_url:
            yield event.plain_result(info_text + "\n（无法获取视频链接）")
            return

        if cover_url:
            yield event.chain_result([
                Comp.Image.fromURL(cover_url),
                Comp.Plain(info_text),
            ])
        else:
            yield event.plain_result(info_text)

        try:
            video_path = await download_video(video_url)
            yield event.chain_result([Comp.Video.fromFileSystem(video_path)])
            remove_files([video_path])
        except Exception as e:
            yield event.plain_result(f"视频处理失败: {str(e)}")
    else:
        yield event.plain_result(f"不支持的内容类型: {content_type}")
