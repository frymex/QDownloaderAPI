from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from instagram_service import resolve_instagram_media
from tiktok_service import ResolveOptions, resolve_tiktok_media

app = FastAPI(title="TikTok Downloader API", version="1.0.0")
TUNNEL_SECRET = os.getenv("TUNNEL_SECRET", "change-me")
TUNNEL_TTL_SECONDS = int(os.getenv("TUNNEL_TTL_SECONDS", "900"))


class ResolveRequest(BaseModel):
    url: str = Field(..., description="TikTok post or short link")
    h265: bool = False
    audio_only: bool = False
    full_audio: bool = False
    # cobalt-compatible option names
    allowH265: bool = False
    tiktokFullAudio: bool = False
    downloadMode: Literal["auto", "audio", "mute"] = "auto"


class DownloadRequest(ResolveRequest):
    media_type: Literal["video", "audio", "photo"] = "video"
    photo_index: int = Field(1, ge=1)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _sign_payload(payload: str) -> str:
    sig = hmac.new(TUNNEL_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(sig)


def _make_tunnel_token(url: str, filename: str | None, headers: dict, media_kind: str) -> str:
    payload_obj = {
        "u": url,
        "f": filename,
        "h": headers,
        "k": media_kind,
        "exp": int(time.time()) + TUNNEL_TTL_SECONDS,
    }
    payload = _b64url_encode(json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
    sig = _sign_payload(payload)
    return f"{payload}.{sig}"


def _parse_tunnel_token(token: str) -> dict:
    try:
        payload_part, sig_part = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "tunnel.token.invalid"}) from exc

    expected_sig = _sign_payload(payload_part)
    if not hmac.compare_digest(expected_sig, sig_part):
        raise HTTPException(status_code=403, detail={"error": "tunnel.token.bad_signature"})

    try:
        payload_raw = _b64url_decode(payload_part).decode("utf-8")
        payload_obj = json.loads(payload_raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail={"error": "tunnel.token.malformed"}) from exc

    exp = int(payload_obj.get("exp", 0))
    if exp < int(time.time()):
        raise HTTPException(status_code=410, detail={"error": "tunnel.token.expired"})

    return payload_obj


def _build_outbound_headers(resolved_headers: dict, media_kind: str) -> dict:
    fetch_dest = "image" if media_kind == "photo" else ("audio" if media_kind == "audio" else "video")
    outbound_headers = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "referer": resolved_headers.get("referer", "https://www.tiktok.com/"),
        "origin": "https://www.tiktok.com",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "range": "bytes=0-",
        "sec-fetch-dest": fetch_dest,
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "cross-site",
    }
    cookie_header = resolved_headers.get("cookie")
    if cookie_header:
        outbound_headers["cookie"] = cookie_header
    return outbound_headers


async def _stream_remote(target_url: str, filename: str | None, outbound_headers: dict) -> StreamingResponse:
    client = httpx.AsyncClient(timeout=60.0)
    req = client.build_request("GET", target_url, headers=outbound_headers)
    upstream = await client.send(req, stream=True)

    if upstream.status_code == 403:
        await upstream.aclose()
        retry_headers = dict(outbound_headers)
        retry_headers["sec-fetch-site"] = "same-site"
        retry_headers["sec-fetch-mode"] = "cors"
        retry_headers["sec-fetch-dest"] = "empty"
        req = client.build_request("GET", target_url, headers=retry_headers)
        upstream = await client.send(req, stream=True)

    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail={"error": "upstream.fail", "status_code": upstream.status_code})

    media_type = upstream.headers.get("content-type", "application/octet-stream")
    response_headers = {}
    if filename:
        response_headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    async def iterator():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(iterator(), media_type=media_type, headers=response_headers)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _to_cobalt_error(error_code: str, context: dict | None = None) -> dict:
    error_obj: dict = {"code": error_code}
    if context:
        error_obj["context"] = context
    return {"status": "error", "error": error_obj}


def _normalize_resolve_error(resolved: dict) -> dict:
    error_code = resolved.get("error", "fetch.fail")
    context = resolved.get("context")
    return _to_cobalt_error(error_code, context)


def _tunnel_url(base_url: str, token: str) -> str:
    return f"{base_url}/tunnel?token={token}"


def _to_resolve_options(request: ResolveRequest) -> ResolveOptions:
    return ResolveOptions(
        h265=request.h265 or request.allowH265,
        audio_only=request.audio_only or request.downloadMode == "audio",
        full_audio=request.full_audio or request.tiktokFullAudio,
    )


async def _resolve_media(source_url: str, options: ResolveOptions) -> dict:
    host = (urlparse(source_url).netloc or "").lower()
    if any(domain in host for domain in ("instagram.com", "ddinstagram.com")):
        return await resolve_instagram_media(source_url)
    return await resolve_tiktok_media(source_url, options)


@app.post("/api/tiktok/resolve")
async def resolve(request: ResolveRequest) -> dict:
    return await _resolve_media(
        request.url,
        _to_resolve_options(request),
    )


@app.get("/")
async def cobalt_info() -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "cobalt": {
            "version": "fastapi-compat-1.0",
            "url": "",
            "startTime": str(now_ms),
            "services": ["tiktok", "instagram"],
        },
        "git": {
            "commit": "local",
            "branch": "local",
            "remote": "local",
        },
    }


@app.post("/")
async def cobalt_process(request: ResolveRequest, http_request: Request) -> dict:
    resolved = await _resolve_media(
        request.url,
        _to_resolve_options(request),
    )

    status = resolved.get("status")
    if status == "error":
        return _normalize_resolve_error(resolved)

    base_url = str(http_request.base_url).rstrip("/")
    resolved_headers = resolved.get("headers") or {}

    if status == "video":
        token = _make_tunnel_token(
            url=resolved.get("url"),
            filename=resolved.get("filename"),
            headers=resolved_headers,
            media_kind="video",
        )
        return {
            "status": "tunnel",
            "url": _tunnel_url(base_url, token),
            "filename": resolved.get("filename"),
        }

    if status == "photo":
        token = _make_tunnel_token(
            url=resolved.get("url"),
            filename=resolved.get("filename"),
            headers=resolved_headers,
            media_kind="photo",
        )
        return {
            "status": "tunnel",
            "url": _tunnel_url(base_url, token),
            "filename": resolved.get("filename"),
        }

    if status == "audio":
        token = _make_tunnel_token(
            url=resolved.get("url"),
            filename=resolved.get("filename"),
            headers=resolved_headers,
            media_kind="audio",
        )
        return {
            "status": "tunnel",
            "url": _tunnel_url(base_url, token),
            "filename": resolved.get("filename"),
        }

    if status == "picker":
        picker = []

        source_items = []
        if isinstance(resolved.get("picker"), list):
            source_items = resolved.get("picker") or []
        elif isinstance(resolved.get("photos"), list):
            source_items = resolved.get("photos") or []

        for item in source_items:
            item_url = item.get("url")
            if not item_url:
                continue
            item_type = item.get("type", "photo")
            media_kind = "video" if item_type == "video" else "photo"
            token = _make_tunnel_token(
                url=item_url,
                filename=item.get("filename"),
                headers=resolved_headers,
                media_kind=media_kind,
            )
            picker.append(
                {
                    "type": item_type,
                    "url": _tunnel_url(base_url, token),
                }
            )

        response = {
            "status": "picker",
            "picker": picker,
        }
        if resolved.get("audio"):
            audio_token = _make_tunnel_token(
                url=resolved.get("audio"),
                filename=resolved.get("audio_filename"),
                headers=resolved_headers,
                media_kind="audio",
            )
            audio_tunnel = _tunnel_url(base_url, audio_token)
            response["audio"] = audio_tunnel
            # cobalt style key
            response["audioFilename"] = resolved.get("audio_filename")
            # compatibility with custom snake_case models
            response["audio_filename"] = resolved.get("audio_filename")
        return response

    return _to_cobalt_error("fetch.fail", {"resolved_status": status})


@app.post("/session")
async def cobalt_session(cf_turnstile_response: str | None = Header(default=None)) -> dict:
    # Compatibility endpoint: returns a short-lived bearer-like token shape.
    exp = 3600
    token_source = f"{cf_turnstile_response or 'local'}:{int(time.time())}"
    token = _b64url_encode(token_source.encode("utf-8"))
    return {"token": token, "exp": exp}


@app.post("/api/tiktok/links")
async def links(request: ResolveRequest, http_request: Request) -> dict:
    resolved = await _resolve_media(
        request.url,
        _to_resolve_options(request),
    )

    status = resolved.get("status")
    if status == "error":
        return _normalize_resolve_error(resolved)

    base_url = str(http_request.base_url).rstrip("/")
    resolved_headers = resolved.get("headers") or {}

    if status == "video":
        token = _make_tunnel_token(
            url=resolved.get("url"),
            filename=resolved.get("filename"),
            headers=resolved_headers,
            media_kind="video",
        )
        return {
            "status": "ok",
            "content_type": "video",
            "links": [f"{base_url}/tunnel?token={token}"],
            "filename": resolved.get("filename"),
        }

    if status == "photo":
        token = _make_tunnel_token(
            url=resolved.get("url"),
            filename=resolved.get("filename"),
            headers=resolved_headers,
            media_kind="photo",
        )
        return {
            "status": "ok",
            "content_type": "photo",
            "links": [f"{base_url}/tunnel?token={token}"],
            "filename": resolved.get("filename"),
        }

    if status == "audio":
        token = _make_tunnel_token(
            url=resolved.get("url"),
            filename=resolved.get("filename"),
            headers=resolved_headers,
            media_kind="audio",
        )
        return {
            "status": "ok",
            "content_type": "audio",
            "links": [f"{base_url}/tunnel?token={token}"],
            "filename": resolved.get("filename"),
        }

    if status == "picker":
        links_out = []

        source_items = []
        if isinstance(resolved.get("picker"), list):
            source_items = resolved.get("picker") or []
        elif isinstance(resolved.get("photos"), list):
            source_items = resolved.get("photos") or []

        for item in source_items:
            item_url = item.get("url")
            if not item_url:
                continue
            item_type = item.get("type", "photo")
            token = _make_tunnel_token(
                url=item_url,
                filename=item.get("filename"),
                headers=resolved_headers,
                media_kind="video" if item_type == "video" else "photo",
            )
            links_out.append({"type": item_type, "url": f"{base_url}/tunnel?token={token}"})

        out = {
            "status": "ok",
            "content_type": "picker",
            "links": links_out,
        }
        if resolved.get("audio"):
            audio_token = _make_tunnel_token(
                url=resolved.get("audio"),
                filename=resolved.get("audio_filename"),
                headers=resolved_headers,
                media_kind="audio",
            )
            out["audio_link"] = f"{base_url}/tunnel?token={audio_token}"
            out["audio_filename"] = resolved.get("audio_filename")
        return out

    return {"status": "error", "error": "unsupported.resolve_status", "resolved": resolved}


@app.post("/api/tiktok/download")
async def download(request: DownloadRequest) -> StreamingResponse:
    resolved = await _resolve_media(
        request.url,
        _to_resolve_options(request),
    )

    status = resolved.get("status")
    if status == "error":
        raise HTTPException(status_code=400, detail=resolved)

    target_url: str | None = None
    filename: str | None = None

    if request.media_type == "video":
        if status != "video":
            raise HTTPException(status_code=400, detail={"error": "video.not_available", "resolved": resolved})
        target_url = resolved.get("url")
        filename = resolved.get("filename")
    elif request.media_type == "audio":
        if status == "audio":
            target_url = resolved.get("url")
            filename = resolved.get("filename")
        elif status == "picker" and resolved.get("audio"):
            target_url = resolved.get("audio")
            filename = resolved.get("audio_filename") or "tiktok_audio.m4a"
        else:
            raise HTTPException(status_code=400, detail={"error": "audio.not_available", "resolved": resolved})
    elif request.media_type == "photo":
        if status != "picker":
            raise HTTPException(status_code=400, detail={"error": "photo.not_available", "resolved": resolved})
        photos = resolved.get("photos") or []
        index = request.photo_index - 1
        if index < 0 or index >= len(photos):
            raise HTTPException(status_code=400, detail={"error": "photo.index_out_of_range", "count": len(photos)})
        target_url = photos[index].get("url")
        filename = photos[index].get("filename")

    if not target_url:
        raise HTTPException(status_code=500, detail={"error": "download.url_missing"})

    resolved_headers = resolved.get("headers") or {}
    media_kind = "photo" if request.media_type == "photo" else request.media_type
    outbound_headers = _build_outbound_headers(resolved_headers, media_kind=media_kind)
    return await _stream_remote(target_url=target_url, filename=filename, outbound_headers=outbound_headers)


@app.get("/tunnel")
async def tunnel(token: str = Query(..., description="Signed tunnel token")) -> StreamingResponse:
    payload = _parse_tunnel_token(token)
    target_url = payload.get("u")
    filename = payload.get("f")
    headers = payload.get("h") or {}
    media_kind = payload.get("k", "video")

    if not target_url:
        raise HTTPException(status_code=400, detail={"error": "tunnel.url_missing"})

    outbound_headers = _build_outbound_headers(headers, media_kind=media_kind)
    return await _stream_remote(target_url=target_url, filename=filename, outbound_headers=outbound_headers)


@app.get("/api")
async def root() -> dict[str, str]:
    return {
        "service": "fastapi-tiktok-downloader",
        "resolve_endpoint": "/api/tiktok/resolve",
        "links_endpoint": "/api/tiktok/links",
        "download_endpoint": "/api/tiktok/download",
        "tunnel_endpoint": "/tunnel?token=...",
        "cobalt_compat_post": "/",
        "cobalt_compat_get": "/",
    }
