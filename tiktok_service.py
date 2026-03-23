from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

GENERIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

JSON_SCRIPT_REGEX = re.compile(
    r'<script[^>]*id="([^"]+)"[^>]*type="application/json"[^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)
SIGI_STATE_REGEX = re.compile(
    r'<script[^>]*id="SIGI_STATE"[^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)


@dataclass
class ResolveOptions:
    h265: bool = False
    audio_only: bool = False
    full_audio: bool = False


def _parse_json(raw: str) -> Any | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _decode_escaped_url(url: str) -> str:
    return url.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")


def _is_valid_detail_candidate(item_struct: Any, post_id: str) -> bool:
    if not isinstance(item_struct, dict):
        return False
    id_matches = str(item_struct.get("id")) == str(post_id)
    has_media = bool(
        item_struct.get("video", {}).get("playAddr")
        or item_struct.get("imagePost", {}).get("images")
        or item_struct.get("music", {}).get("playUrl")
    )
    has_author = bool(item_struct.get("author"))
    return id_matches or (has_media and has_author)


def _find_detail_deep(node: Any, post_id: str, visited: set[int] | None = None) -> Any | None:
    if not isinstance(node, dict):
        return None

    if visited is None:
        visited = set()

    node_id = id(node)
    if node_id in visited:
        return None
    visited.add(node_id)

    item_struct = (
        node.get("itemInfo", {}).get("itemStruct")
        or node.get("itemStruct")
    )
    if _is_valid_detail_candidate(item_struct, post_id):
        return item_struct

    item_module = node.get("ItemModule")
    if isinstance(item_module, dict):
        direct = item_module.get(str(post_id))
        if _is_valid_detail_candidate(direct, post_id):
            return direct
        for value in item_module.values():
            if _is_valid_detail_candidate(value, post_id):
                return value

    for value in node.values():
        if isinstance(value, dict):
            found = _find_detail_deep(value, post_id, visited)
            if found:
                return found
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = _find_detail_deep(item, post_id, visited)
                    if found:
                        return found
    return None


def _extract_detail_from_html(html: str, post_id: str) -> Any | None:
    parsed_payloads: list[Any] = []

    for script_id, raw_json in JSON_SCRIPT_REGEX.findall(html):
        if script_id not in ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE"):
            continue
        parsed = _parse_json(raw_json)
        if parsed:
            parsed_payloads.append(parsed)

    raw_sigi_match = SIGI_STATE_REGEX.search(html)
    if raw_sigi_match:
        parsed = _parse_json(raw_sigi_match.group(1))
        if parsed:
            parsed_payloads.append(parsed)

    for payload in parsed_payloads:
        direct = (
            payload.get("__DEFAULT_SCOPE__", {})
            .get("webapp.video-detail", {})
            .get("itemInfo", {})
            .get("itemStruct")
        )
        if _is_valid_detail_candidate(direct, post_id):
            return direct
        deep = _find_detail_deep(payload, post_id)
        if deep:
            return deep
    return None


def _extract_photo_links_from_html(html: str) -> list[str]:
    found: set[str] = set()
    targeted_patterns = [
        re.compile(r'"imageURL"\s*:\s*{[\s\S]{0,2500}?"urlList"\s*:\s*\[([\s\S]*?)\]'),
        re.compile(r'"urlList"\s*:\s*\[([\s\S]*?)\]\s*,\s*"uri"\s*:'),
    ]

    for pattern in targeted_patterns:
        for match in pattern.findall(html):
            url_matches = re.findall(r'https:\\\/\\\/[^"\'<>\\]+|https:\/\/[^"\'<>\\\s]+', match)
            for raw_url in url_matches:
                decoded = _decode_escaped_url(raw_url)
                if "tiktokcdn" not in decoded:
                    continue
                if "/tos-" not in decoded:
                    continue
                if not any(ext in decoded for ext in (".jpeg", ".jpg", ".webp")):
                    continue
                found.add(decoded)

    if found:
        return list(found)

    fallback_patterns = [
        re.compile(r'https:\\\/\\\/[^"\'<>\\]+'),
        re.compile(r'https:\/\/[^"\'<>\\\s]+'),
    ]
    for pattern in fallback_patterns:
        for raw_url in pattern.findall(html):
            decoded = _decode_escaped_url(raw_url)
            if "tiktokcdn" not in decoded:
                continue
            if "/tos-" not in decoded:
                continue
            if "~tplv" not in decoded:
                continue
            if not any(ext in decoded for ext in (".jpeg", ".jpg", ".webp")):
                continue
            found.add(decoded)

    return list(found)


def _extract_post_id(url: str) -> str | None:
    patterns = [
        re.compile(r"/video/(\d+)"),
        re.compile(r"/photo/(\d+)"),
        re.compile(r"/v/(\d+)\.html"),
    ]
    for pattern in patterns:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


async def _resolve_short_link(client: httpx.AsyncClient, url: str) -> str | None:
    response = await client.get(url, follow_redirects=False)
    location = response.headers.get("location")
    if location:
        post_id = _extract_post_id(location)
        if post_id:
            return post_id

    html = response.text
    anchor_match = re.search(r'<a href="(https://[^"]+)"', html)
    if anchor_match:
        link = anchor_match.group(1).split("?")[0]
        return _extract_post_id(link)

    return None


async def _fetch_and_extract_detail(
    client: httpx.AsyncClient, url: str, post_id: str
) -> tuple[Any | None, list[str]]:
    res = await client.get(url)
    html = res.text
    detail = _extract_detail_from_html(html, post_id)
    image_links = _extract_photo_links_from_html(html)
    return detail, image_links


async def resolve_tiktok_media(
    source_url: str, options: ResolveOptions
) -> dict[str, Any]:
    parsed = urlparse(source_url)
    if not parsed.scheme or not parsed.netloc:
        return {"status": "error", "error": "link.invalid"}

    async with httpx.AsyncClient(
        headers={"user-agent": GENERIC_UA},
        timeout=20.0,
    ) as client:
        post_id = _extract_post_id(source_url)
        host = parsed.netloc.lower()
        if not post_id and ("vt.tiktok.com" in host or "vm.tiktok.com" in host):
            post_id = await _resolve_short_link(client, source_url)

        if not post_id:
            return {"status": "error", "error": "fetch.short_link"}

        detail = None
        scraped_images: list[str] = []
        for target in (
            f"https://www.tiktok.com/@i/video/{post_id}",
            f"https://www.tiktok.com/@i/photo/{post_id}",
            f"https://m.tiktok.com/v/{post_id}.html",
            f"https://www.tiktok.com/embed/v2/{post_id}",
        ):
            parsed_detail, parsed_images = await _fetch_and_extract_detail(client, target, post_id)
            if parsed_images and not scraped_images:
                scraped_images = parsed_images
            if parsed_detail:
                detail = parsed_detail
                break

        cookie_header = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        base_headers = {"referer": "https://www.tiktok.com/"}
        if cookie_header:
            base_headers["cookie"] = cookie_header

        if not detail and not scraped_images:
            return {"status": "error", "error": "fetch.empty", "context": {"post_id": post_id}}

        if detail and detail.get("isContentClassified"):
            return {"status": "error", "error": "content.post.age"}

        filename_base = f"tiktok_{post_id}"
        author = detail.get("author") if isinstance(detail, dict) else None
        if isinstance(author, dict) and author.get("uniqueId"):
            filename_base = f"tiktok_{author['uniqueId']}_{post_id}"

        images = detail.get("imagePost", {}).get("images") if isinstance(detail, dict) else None
        video_block = detail.get("video", {}) if isinstance(detail, dict) else {}
        play_addr = video_block.get("playAddr")
        if options.h265 and isinstance(video_block.get("bitrateInfo"), list):
            for bitrate in video_block["bitrateInfo"]:
                codec = str(bitrate.get("CodecType", ""))
                if "h265" not in codec.lower():
                    continue
                url_list = bitrate.get("PlayAddr", {}).get("UrlList", [])
                if url_list:
                    play_addr = url_list[0]
                    break

        audio = play_addr
        audio_filename = f"{filename_base}_audio"
        if options.full_audio or not audio:
            music_play = detail.get("music", {}).get("playUrl") if isinstance(detail, dict) else None
            if music_play:
                audio = music_play
                audio_filename = f"{audio_filename}_original"

        if not options.audio_only and not images and play_addr:
            return {
                "status": "video",
                "url": play_addr,
                "filename": f"{filename_base}.mp4",
                "headers": base_headers,
            }

        if images:
            photo_urls: list[str] = []
            for image in images:
                image_url_block = image.get("imageURL", {}) if isinstance(image, dict) else {}
                url_list = image_url_block.get("urlList", [])
                selected = next((u for u in url_list if ".jpeg?" in u), None) or (url_list[0] if url_list else None)
                if selected:
                    photo_urls.append(selected)
            return {
                "status": "picker",
                "photos": [
                    {"type": "photo", "url": item, "filename": f"{filename_base}_photo_{idx + 1}.jpg"}
                    for idx, item in enumerate(photo_urls)
                ],
                "audio": audio,
                "audio_filename": audio_filename if audio else None,
                "headers": base_headers,
            }

        if scraped_images:
            return {
                "status": "picker",
                "photos": [
                    {"type": "photo", "url": item, "filename": f"{filename_base}_photo_{idx + 1}.jpg"}
                    for idx, item in enumerate(scraped_images[:20])
                ],
                "audio": audio,
                "audio_filename": audio_filename if audio else None,
                "headers": base_headers,
            }

        if audio:
            ext = "mp3" if "mime_type=audio_mpeg" in audio else "m4a"
            return {
                "status": "audio",
                "url": audio,
                "filename": f"{audio_filename}.{ext}",
                "headers": base_headers,
            }

        return {"status": "error", "error": "fetch.empty", "context": {"post_id": post_id}}
