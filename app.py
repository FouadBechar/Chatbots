import logging
import os
import time
import uuid
from collections import defaultdict, deque
from ipaddress import ip_address

import requests
from redis import Redis
from redis.exceptions import RedisError
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix


class InMemoryRateLimiter:
    """Simple per-client rate limiter for single-process deployments."""

    def __init__(self):
        self._request_buckets = defaultdict(deque)

    def allow(self, client_id: str, now: float, limit: int, window_seconds: int) -> bool:
        bucket = self._request_buckets[client_id]

        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()

        if len(bucket) >= limit:
            return False

        bucket.append(now)
        return True


class RedisRateLimiter:
    """Shared rate limiter backed by Redis for multi-process deployments."""

    def __init__(self, client: Redis, key_prefix: str = "chatbots:rate_limit"):
        self._client = client
        self._key_prefix = key_prefix

    def _bucket_key(self, client_id: str) -> str:
        return f"{self._key_prefix}:{client_id}"

    def allow(self, client_id: str, now: float, limit: int, window_seconds: int) -> bool:
        key = self._bucket_key(client_id)
        pipeline = self._client.pipeline()
        min_score = now - window_seconds

        pipeline.zremrangebyscore(key, 0, min_score)
        pipeline.zcard(key)
        pipeline.zadd(key, {str(now): now})
        pipeline.expire(key, max(window_seconds, 1))
        _, current_count, _, _ = pipeline.execute()

        if current_count >= limit:
            self._client.zrem(key, str(now))
            return False

        return True


def get_client_ip(flask_request) -> str:
    """Return the first valid client IP, preferring trusted proxy headers."""

    forwarded_for = flask_request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        for candidate in forwarded_for.split(","):
            candidate = candidate.strip()
            try:
                ip_address(candidate)
                return candidate
            except ValueError:
                continue

    remote_addr = flask_request.remote_addr or "unknown"
    try:
        ip_address(remote_addr)
        return remote_addr
    except ValueError:
        return "unknown"


def error_response(code: str, message: str, status: int, request_id: str):
    return (
        jsonify(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": request_id,
                }
            }
        ),
        status,
    )


def validate_messages(messages):
    if not isinstance(messages, list) or not messages:
        return "'messages' must be a non-empty list."

    if len(messages) > 50:
        return "'messages' cannot contain more than 50 entries."

    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            return f"messages[{i}] must be an object."

        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"}:
            return f"messages[{i}].role must be one of: system, user, assistant."
        if not isinstance(content, str) or not content.strip():
            return f"messages[{i}].content must be a non-empty string."
        if len(content) > 8000:
            return f"messages[{i}].content exceeds 8000 characters."

    return None


def build_upstream_headers(api_key: str):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Referer": "https://fbweb.vercel.app",
        "X-Title": "GPT Chat App",
    }


def configure_rate_limiter(app) -> tuple[object, str]:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return InMemoryRateLimiter(), "InMemoryRateLimiter"

    try:
        redis_client = Redis.from_url(redis_url, decode_responses=True)
        redis_client.ping()
        return RedisRateLimiter(redis_client), "RedisRateLimiter"
    except RedisError as redis_error:
        app.logger.warning("redis_rate_limiter_unavailable error=%s", redis_error)
        return InMemoryRateLimiter(), "InMemoryRateLimiter"


def create_app(session: requests.Session | None = None, rate_limiter: InMemoryRateLimiter | None = None):
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    allowed_origins = os.environ.get(
        "ALLOWED_ORIGINS",
        "https://fouadbechar.x10.network,https://fbweb.vercel.app",
    )
    origins = [origin.strip() for origin in allowed_origins.split(",") if origin.strip()]
    CORS(app, resources={r"/chat": {"origins": origins}})

    app.config["API_KEY"] = os.environ.get("API_KEY")
    app.config["MODEL"] = os.environ.get("MODEL", "openai/gpt-4o-mini")
    app.config["OPENROUTER_URL"] = os.environ.get(
        "OPENROUTER_URL",
        "https://openrouter.ai/api/v1/chat/completions",
    )
    app.config["REQUEST_TIMEOUT_SECONDS"] = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30"))
    app.config["RATE_LIMIT_REQUESTS"] = int(os.environ.get("RATE_LIMIT_REQUESTS", "30"))
    app.config["RATE_LIMIT_WINDOW_SECONDS"] = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))
    app.config["REQUIRE_BEARER_TOKEN"] = os.environ.get("REQUIRE_BEARER_TOKEN", "false").lower() == "true"
    app.config["BEARER_TOKEN"] = os.environ.get("BEARER_TOKEN")

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app.logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

    app.extensions["http_session"] = session or requests.Session()
    if rate_limiter is not None:
        app.extensions["rate_limiter"] = rate_limiter
        app.extensions["rate_limiter_backend"] = rate_limiter.__class__.__name__
    else:
        resolved_rate_limiter, backend_name = configure_rate_limiter(app)
        app.extensions["rate_limiter"] = resolved_rate_limiter
        app.extensions["rate_limiter_backend"] = backend_name

    @app.get("/health")
    def health():
        has_api_key = bool(app.config["API_KEY"])
        return (
            jsonify(
                {
                    "status": "ok",
                    "api_key_configured": has_api_key,
                    "rate_limiter_backend": app.extensions["rate_limiter_backend"],
                }
            ),
            (200 if has_api_key else 503),
        )

    @app.route("/chat", methods=["POST"])
    def chat():
        request_id = str(uuid.uuid4())
        start = time.perf_counter()
        client_id = get_client_ip(request)

        if not app.extensions["rate_limiter"].allow(
            client_id=client_id,
            now=time.time(),
            limit=app.config["RATE_LIMIT_REQUESTS"],
            window_seconds=app.config["RATE_LIMIT_WINDOW_SECONDS"],
        ):
            return error_response(
                code="rate_limited",
                message="Too many requests. Please try again later.",
                status=429,
                request_id=request_id,
            )

        if app.config["REQUIRE_BEARER_TOKEN"]:
            auth_header = request.headers.get("Authorization", "")
            expected_token = app.config["BEARER_TOKEN"]
            if not expected_token or auth_header != f"Bearer {expected_token}":
                return error_response(
                    code="unauthorized",
                    message="Missing or invalid bearer token.",
                    status=401,
                    request_id=request_id,
                )

        api_key = app.config["API_KEY"]
        if not api_key:
            return error_response(
                code="misconfigured_server",
                message="Server API key is not configured.",
                status=500,
                request_id=request_id,
            )

        data = request.get_json(silent=True) or {}
        messages = data.get("messages")
        validation_error = validate_messages(messages)
        if validation_error:
            return error_response(
                code="invalid_request",
                message=validation_error,
                status=400,
                request_id=request_id,
            )

        payload = {"model": app.config["MODEL"], "messages": messages}

        try:
            resp = app.extensions["http_session"].post(
                app.config["OPENROUTER_URL"],
                headers=build_upstream_headers(api_key),
                json=payload,
                timeout=app.config["REQUEST_TIMEOUT_SECONDS"],
            )
            resp.raise_for_status()
            resp_json = resp.json()
            reply = resp_json["choices"][0]["message"]["content"]
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            app.logger.info(
                "chat_success request_id=%s client_ip=%s status=%s latency_ms=%s",
                request_id,
                client_id,
                resp.status_code,
                latency_ms,
            )
            return jsonify({"reply": reply, "request_id": request_id})

        except requests.exceptions.Timeout:
            return error_response(
                code="upstream_timeout",
                message="Upstream provider timed out.",
                status=504,
                request_id=request_id,
            )
        except requests.exceptions.HTTPError as http_err:
            status_code = http_err.response.status_code if http_err.response is not None else 502
            provider_message = "Upstream provider returned an HTTP error."
            try:
                upstream_error = http_err.response.json().get("error", {})
                provider_message = upstream_error.get("message", provider_message)
            except (ValueError, AttributeError):
                pass

            app.logger.warning(
                "chat_upstream_http_error request_id=%s client_ip=%s status=%s error=%s",
                request_id,
                client_id,
                status_code,
                http_err,
            )
            return error_response(
                code="upstream_http_error",
                message=provider_message,
                status=status_code,
                request_id=request_id,
            )
        except (KeyError, ValueError, TypeError) as parse_err:
            app.logger.warning(
                "chat_upstream_parse_error request_id=%s client_ip=%s error=%s",
                request_id,
                client_id,
                parse_err,
            )
            return error_response(
                code="invalid_upstream_response",
                message="Unexpected response format from upstream provider.",
                status=502,
                request_id=request_id,
            )
        except requests.exceptions.RequestException as req_err:
            app.logger.warning(
                "chat_upstream_request_error request_id=%s client_ip=%s error=%s",
                request_id,
                client_id,
                req_err,
            )
            return error_response(
                code="upstream_unavailable",
                message="Unable to reach the upstream provider.",
                status=502,
                request_id=request_id,
            )
        except Exception:
            app.logger.exception("chat_unexpected_error request_id=%s client_ip=%s", request_id, client_id)
            return error_response(
                code="internal_error",
                message="An unexpected server error occurred.",
                status=500,
                request_id=request_id,
            )

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
