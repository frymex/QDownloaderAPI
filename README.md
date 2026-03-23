# QDownloaderAPI

FastAPI-сервис с cobalt-совместимым API для загрузки контента из TikTok и Instagram.

Поддержка:
- `POST /` в формате cobalt (`tunnel | picker | error`)
- `GET /` (информация об инстансе)
- `GET /tunnel?token=...` (проксирующий туннель)
- `POST /session` (совместимый endpoint)

Дополнительно:
- `POST /api/tiktok/resolve`
- `POST /api/tiktok/links`
- `POST /api/tiktok/download`
- `GET /health`

## Почему это совместимо с вашим клиентом

Ваш `QDownloader.process_url(...)` отправляет запрос в `POST /` и ожидает:
- `status = "tunnel"` или `status = "redirect"` с полями `url`, `filename`
- `status = "picker"` с `picker[]`
- `status = "error"` с `error.code`

Этот сервис возвращает именно такой формат.

## Структура проекта

```text
.
├─ main.py
├─ tiktok_service.py
├─ instagram_service.py
├─ requirements.txt
├─ Dockerfile
├─ docker-compose.yml
├─ .env.example
└─ README.md
```

## Локальный запуск

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8010 --reload
```

## Docker запуск

```bash
cp .env.example .env
docker compose up --build -d
```

Проверка:

```bash
curl http://localhost:8010/health
```

## Минимальный пример запроса (cobalt-совместимый)

```bash
curl -X POST "http://localhost:8010/" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://www.tiktok.com/@user/video/123\"}"
```

Типичный ответ:

```json
{
  "status": "tunnel",
  "url": "http://localhost:8010/tunnel?token=...",
  "filename": "tiktok_user_123.mp4"
}
```

Для карусели:

```json
{
  "status": "picker",
  "picker": [
    { "type": "photo", "url": "http://localhost:8010/tunnel?token=..." },
    { "type": "video", "url": "http://localhost:8010/tunnel?token=..." }
  ]
}
```

## Переменные окружения

- `TUNNEL_SECRET` — секрет подписи токена для `/tunnel`
- `TUNNEL_TTL_SECONDS` — TTL токена (по умолчанию `900`)

Для production обязательно задайте уникальный `TUNNEL_SECRET`.

## Совместимость параметров POST `/`

Поддерживаются cobalt-имена опций:
- `allowH265`
- `tiktokFullAudio`
- `downloadMode` (`auto | audio | mute`)

А также внутренние:
- `h265`
- `audio_only`
- `full_audio`

## Лицензия и источники

Логика сервиса основана на подходах проекта cobalt:
- [cobalt](https://github.com/frymex/cobalt)

При публичном использовании изменений учитывайте условия лицензии исходного проекта.
