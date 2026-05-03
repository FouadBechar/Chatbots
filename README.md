# Chatbots API

A small Flask API that validates chat requests, forwards them to OpenRouter, and returns a simplified reply payload for your frontend.

## What changed

- Proxy-aware client IP detection for safer rate limiting behind a CDN or reverse proxy
- Optional Redis-backed rate limiting for multi-worker and multi-instance deployments
- Reusable HTTP session for upstream calls
- Better health output and clearer upstream error messages
- Basic automated tests for health, validation, proxy headers, upstream errors, and rate limiting behavior

## Quick start

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` into your environment configuration and set `API_KEY`.
4. Run the server:

```bash
python app.py
```

The API starts on `http://127.0.0.1:5000` by default.

## Environment variables

- `API_KEY`: Required OpenRouter API key
- `MODEL`: Upstream model name. Default: `openai/gpt-4o-mini`
- `ALLOWED_ORIGINS`: Comma-separated frontend origins allowed to call `/chat`
- `OPENROUTER_URL`: Upstream endpoint override
- `REQUEST_TIMEOUT_SECONDS`: Timeout for the upstream request
- `RATE_LIMIT_REQUESTS`: Requests allowed per window per client IP
- `RATE_LIMIT_WINDOW_SECONDS`: Rate-limit window length in seconds
- `REDIS_URL`: Optional Redis connection string used for shared rate limiting across instances
- `REQUIRE_BEARER_TOKEN`: Set to `true` to require `Authorization: Bearer ...` on `/chat`
- `BEARER_TOKEN`: Shared token used when bearer auth is enabled
- `LOG_LEVEL`: Flask app log level
- `PORT`: Local port when running `python app.py`

## Endpoints

### `GET /health`

Returns service health, whether the upstream API key is configured, and the current rate-limiter backend.

### `POST /chat`

Expected payload:

```json
{
  "messages": [
    { "role": "user", "content": "Hello" }
  ]
}
```

Success response:

```json
{
  "reply": "Hello from upstream",
  "request_id": "..."
}
```

## Testing

Run:

```bash
python -m unittest
```

## Deployment note

If `REDIS_URL` is not set, the app uses `InMemoryRateLimiter`, which is fine for a single process but not ideal for multi-worker or multi-instance production deployments.

If `REDIS_URL` is set and Redis is reachable, the app uses `RedisRateLimiter` so limits are shared across workers and instances. If Redis is configured but unavailable at startup, the app logs a warning and falls back to the in-memory limiter instead of failing to boot.
