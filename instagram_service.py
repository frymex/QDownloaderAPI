from __future__ import annotations

import json
import random
import re
import string
from typing import Any
from urllib.parse import urlparse

import httpx

GENERIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

EMBED_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-GB,en;q=0.9",
    "cache-control": "max-age=0",
    "dnt": "1",
    "priority": "u=0, i",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "upgrade-insecure-requests": "1",
    "user-agent": GENERIC_UA,
}


def _get_number_from_query(name: str, data: str) -> int | None:
    m = re.search(rf"{re.escape(name)}=(\d+)", data or "")
    if m:
        return int(m.group(1))
    return None


def _get_object_from_entries(name: str, data: str) -> dict | None:
    m = re.search(rf'\["{re.escape(name)}",.*?,({{.*?}}),\d+\]', data or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _extract_shortcode(url: str) -> tuple[str | None, bool]:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = path.split("/")
    share = False

    if len(parts) >= 2 and parts[0] in ("p", "reel", "reels", "tv"):
        return parts[1], False
    if len(parts) >= 3 and parts[0] == "share":
        return parts[-1], True
    if len(parts) >= 3 and parts[1] in ("p", "reel"):
        return parts[2], False
    return None, False


def _pick_best_video(video_versions: list[dict]) -> str | None:
    best = None
    for v in video_versions:
        if not isinstance(v, dict) or not v.get("url"):
            continue
        if not best:
            best = v
            continue
        if (v.get("width", 0) * v.get("height", 0)) > (best.get("width", 0) * best.get("height", 0)):
            best = v
    return best.get("url") if best else None


def _extract_new_post(data: dict, shortcode: str) -> dict[str, Any] | None:
    carousel = data.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        picker = []
        for idx, item in enumerate(carousel, start=1):
            image_versions = (item or {}).get("image_versions2", {}).get("candidates", [])
            if not image_versions:
                continue
            image_url = image_versions[0].get("url")
            if not image_url:
                continue

            if item.get("video_versions"):
                media_url = _pick_best_video(item["video_versions"])
                if not media_url:
                    continue
                picker.append(
                    {
                        "type": "video",
                        "url": media_url,
                        "thumb": image_url,
                        "filename": f"instagram_{shortcode}_{idx}.mp4",
                    }
                )
            else:
                picker.append(
                    {
                        "type": "photo",
                        "url": image_url,
                        "thumb": image_url,
                        "filename": f"instagram_{shortcode}_{idx}.jpg",
                    }
                )

        if picker:
            return {"status": "picker", "picker": picker}

    if data.get("video_versions"):
        video_url = _pick_best_video(data["video_versions"])
        if video_url:
            return {"status": "video", "url": video_url, "filename": f"instagram_{shortcode}.mp4"}

    image_candidates = data.get("image_versions2", {}).get("candidates", [])
    if image_candidates:
        image_url = image_candidates[0].get("url")
        if image_url:
            return {"status": "photo", "url": image_url, "filename": f"instagram_{shortcode}.jpg"}

    return None


def _extract_old_post(gql_data: dict, shortcode: str) -> dict[str, Any] | None:
    media = gql_data.get("shortcode_media") or gql_data.get("xdt_shortcode_media")
    if not isinstance(media, dict):
        return None

    sidecar = media.get("edge_sidecar_to_children")
    if isinstance(sidecar, dict):
        edges = sidecar.get("edges", [])
        picker = []
        for idx, edge in enumerate(edges, start=1):
            node = (edge or {}).get("node") or {}
            display = node.get("display_url")
            if not display:
                continue
            if node.get("is_video") and node.get("video_url"):
                picker.append(
                    {
                        "type": "video",
                        "url": node["video_url"],
                        "thumb": display,
                        "filename": f"instagram_{shortcode}_{idx}.mp4",
                    }
                )
            else:
                picker.append(
                    {
                        "type": "photo",
                        "url": display,
                        "thumb": display,
                        "filename": f"instagram_{shortcode}_{idx}.jpg",
                    }
                )
        if picker:
            return {"status": "picker", "picker": picker}

    if media.get("video_url"):
        return {"status": "video", "url": media["video_url"], "filename": f"instagram_{shortcode}.mp4"}

    if media.get("display_url"):
        return {"status": "photo", "url": media["display_url"], "filename": f"instagram_{shortcode}.jpg"}

    return None


async def _request_embed(client: httpx.AsyncClient, shortcode: str) -> dict | None:
    res = await client.get(f"https://www.instagram.com/p/{shortcode}/embed/captioned/", headers=EMBED_HEADERS)
    html = res.text
    m = re.search(r'"init",\[\],\[(.*?)\]\],', html)
    if not m:
        return None
    try:
        embed_data = json.loads(m.group(1))
        context_json = embed_data.get("contextJSON")
        return json.loads(context_json) if context_json else None
    except Exception:  # noqa: BLE001
        return None


async def _request_gql(client: httpx.AsyncClient, shortcode: str) -> dict | None:
    page = await client.get(f"https://www.instagram.com/p/{shortcode}/", headers=EMBED_HEADERS)
    html = page.text

    site_data = _get_object_from_entries("SiteData", html) or {}
    polaris_site_data = _get_object_from_entries("PolarisSiteData", html) or {}
    web_config = _get_object_from_entries("DGWWebConfig", html) or {}
    push_info = _get_object_from_entries("InstagramWebPushInfo", html) or {}
    lsd_obj = _get_object_from_entries("LSD", html) or {}
    sec_conf = _get_object_from_entries("InstagramSecurityConfig", html) or {}

    lsd = lsd_obj.get("token") or "".join(random.choices(string.ascii_letters + string.digits, k=12))
    csrf = sec_conf.get("csrf_token")
    anon_cookie_parts = [
        f"csrftoken={csrf}" if csrf else None,
        f"ig_did={polaris_site_data.get('device_id')}" if polaris_site_data.get("device_id") else None,
        "wd=1280x720",
        "dpr=2",
        f"mid={polaris_site_data.get('machine_id')}" if polaris_site_data.get("machine_id") else None,
        "ig_nrcb=1",
    ]
    anon_cookie = "; ".join([p for p in anon_cookie_parts if p])

    gql_headers = {
        **EMBED_HEADERS,
        "x-ig-app-id": web_config.get("appId", "936619743392459"),
        "x-fb-lsd": lsd,
        "x-csrftoken": csrf or "",
        "x-asbd-id": "129477",
        "x-fb-friendly-name": "PolarisPostActionLoadPostQueryQuery",
        "content-type": "application/x-www-form-urlencoded",
    }
    if anon_cookie:
        gql_headers["cookie"] = anon_cookie

    body = {
        "__d": "www",
        "__a": "1",
        "__s": "::" + "".join(random.choices(string.ascii_lowercase, k=6)),
        "__hs": site_data.get("haste_session", "20126.HYP:instagram_web_pkg.2.1...0"),
        "__req": "b",
        "__ccg": "EXCELLENT",
        "__rev": push_info.get("rollout_hash", "1019933358"),
        "__hsi": site_data.get("hsi", "7436540909012459023"),
        "__dyn": "".join(random.choices(string.ascii_letters + string.digits, k=120)),
        "__csr": "".join(random.choices(string.ascii_letters + string.digits, k=120)),
        "__user": "0",
        "__comet_req": str(_get_number_from_query("__comet_req", html) or 7),
        "av": "0",
        "dpr": "2",
        "lsd": lsd,
        "jazoest": str(_get_number_from_query("jazoest", html) or random.randint(1000, 9999)),
        "__spin_r": str(site_data.get("__spin_r", "1019933358")),
        "__spin_b": str(site_data.get("__spin_b", "trunk")),
        "__spin_t": str(site_data.get("__spin_t", int(page.elapsed.total_seconds()) if page.elapsed else 0)),
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": "PolarisPostActionLoadPostQueryQuery",
        "variables": json.dumps(
            {
                "shortcode": shortcode,
                "fetch_tagged_user_count": None,
                "hoisted_comment_id": None,
                "hoisted_reply_id": None,
            }
        ),
        "server_timestamps": "true",
        "doc_id": "8845758582119845",
    }
    req = await client.post("https://www.instagram.com/graphql/query", headers=gql_headers, data=body)
    try:
        return req.json().get("data")
    except Exception:  # noqa: BLE001
        return None


async def _resolve_share_url(client: httpx.AsyncClient, url: str) -> str | None:
    # Cobalt uses curl UA for these endpoints due to occasional HTML responses.
    res = await client.get(url, headers={"user-agent": "curl/7.88.1"}, follow_redirects=True)
    final_url = str(res.url)
    shortcode, _ = _extract_shortcode(final_url)
    if shortcode:
        return shortcode
    return None


async def resolve_instagram_media(source_url: str) -> dict[str, Any]:
    parsed = urlparse(source_url)
    if not parsed.scheme or not parsed.netloc:
        return {"status": "error", "error": "link.invalid"}

    host = parsed.netloc.lower()
    if "instagram.com" not in host and "ddinstagram.com" not in host:
        return {"status": "error", "error": "link.invalid"}

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        shortcode, is_share = _extract_shortcode(source_url)
        if is_share:
            shortcode = await _resolve_share_url(client, source_url)

        if not shortcode:
            return {"status": "error", "error": "link.unsupported"}

        data = await _request_embed(client, shortcode)
        result = _extract_new_post(data or {}, shortcode) if data else None

        if not result:
            gql_data = await _request_gql(client, shortcode)
            if gql_data:
                result = _extract_old_post(gql_data, shortcode)

        if not result:
            return {"status": "error", "error": "fetch.empty"}

        cookie_header = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        headers = {"referer": "https://www.instagram.com/"}
        if cookie_header:
            headers["cookie"] = cookie_header
        result["headers"] = headers
        return result
