# QDownloaderAPI

Cobalt-compatible downloader API built with FastAPI.

It currently supports:
- TikTok
- Instagram

The API is designed to be a drop-in backend for clients that expect cobalt-style responses (`tunnel`, `picker`, `error`).

## Features

- Cobalt-compatible `POST /` processing endpoint
- Signed tunnel links (`GET /tunnel?token=...`) instead of exposing raw CDN URLs
- TikTok support:
  - video posts
  - photo slideshows
  - optional H265 selection
  - optional original audio selection
- Instagram support:
  - posts/reels/tv URLs
  - share URLs
  - single media and carousel (picker) responses
- Docker-ready deployment

## API Compatibility

### Main cobalt-compatible endpoints

- `POST /` — process media URL
- `GET /` — instance info
- `GET /tunnel?token=...` — stream media via tunnel
- `POST /session` — compatibility token endpoint

### Additional utility endpoints

- `GET /health`
- `POST /api/tiktok/resolve`
- `POST /api/tiktok/links`
- `POST /api/tiktok/download`
- `GET /api`

## Installation

### 1) Local Python setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2) Run server

```bash
uvicorn main:app --host 0.0.0.0 --port 8010 --reload
```

Server will be available at:
- `http://localhost:8010`

## Docker

### Run with Docker Compose

```bash
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
docker compose up --build -d
```

### Stop

```bash
docker compose down
```

## Configuration

Environment variables:

- `TUNNEL_SECRET`  
  Secret used to sign tunnel tokens.  
  Set a long random value in production.

- `TUNNEL_TTL_SECONDS`  
  Tunnel token lifetime in seconds (default: `900`).

Example `.env`:

```env
TUNNEL_SECRET=replace-with-a-long-random-secret
TUNNEL_TTL_SECONDS=900
```

## Using It in Projects

This API is intended to be consumed like cobalt.

### Request format (`POST /`)

```json
{
  "url": "https://www.tiktok.com/@user/video/1234567890",
  "allowH265": false,
  "tiktokFullAudio": false,
  "downloadMode": "auto"
}
```

Supported cobalt-style options:
- `allowH265`
- `tiktokFullAudio`
- `downloadMode` (`auto | audio | mute`)

Also accepted:
- `h265`
- `audio_only`
- `full_audio`

### Response format

#### Tunnel response

```json
{
  "status": "tunnel",
  "url": "http://localhost:8010/tunnel?token=...",
  "filename": "tiktok_user_123.mp4"
}
```

#### Picker response

```json
{
  "status": "picker",
  "picker": [
    { "type": "photo", "url": "http://localhost:8010/tunnel?token=..." },
    { "type": "video", "url": "http://localhost:8010/tunnel?token=..." }
  ]
}
```

#### Error response

```json
{
  "status": "error",
  "error": {
    "code": "fetch.empty"
  }
}
```

## Integration Example (Python / aiohttp)

```python
import aiohttp
import asyncio

BASE_URL = "http://localhost:8010"

async def main():
    payload = {
        "url": "https://www.instagram.com/reel/xxxxxxxxxxx/",
        "downloadMode": "auto"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BASE_URL}/",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as resp:
            data = await resp.json()
            print(data)

        # If status == "tunnel", download content directly from data["url"]
        # If status == "picker", choose an item from data["picker"] and request its url

asyncio.run(main())
```

## Quick Health Check

```bash
curl http://localhost:8010/health
```

Expected:

```json
{"status":"ok"}
```

## Notes

- Tunnel URLs are temporary and signed.
- In production, always set a secure `TUNNEL_SECRET`.
- Behavior may change if upstream platforms change internal response formats.
