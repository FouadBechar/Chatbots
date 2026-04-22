import unittest

import requests

from app import InMemoryRateLimiter, create_app


class FakeResponse:
    def __init__(self, payload, status_code=200, http_error_payload=None):
        self._payload = payload
        self.status_code = status_code
        self._http_error_payload = http_error_payload or payload

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def json(self):
        return self._http_error_payload if self.status_code >= 400 else self._payload


class FakeSession:
    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        if self.exception:
            raise self.exception
        return self.response


def make_app(session=None, rate_limiter=None):
    app = create_app(session=session, rate_limiter=rate_limiter)
    app.config.update(
        TESTING=True,
        API_KEY="test-api-key",
        REQUIRE_BEARER_TOKEN=False,
    )
    return app


class AppTestCase(unittest.TestCase):
    def test_health_reports_configuration(self):
        client = make_app().test_client()

        response = client.get("/health")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["api_key_configured"])
        self.assertEqual(payload["rate_limiter_backend"], "InMemoryRateLimiter")

    def test_chat_accepts_forwarded_ip_and_returns_reply(self):
        fake_session = FakeSession(
            response=FakeResponse(
                {"choices": [{"message": {"content": "Hello from upstream"}}]},
                status_code=200,
            )
        )
        client = make_app(session=fake_session).test_client()

        response = client.post(
            "/chat",
            headers={"X-Forwarded-For": "198.51.100.8, 10.0.0.1"},
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["reply"], "Hello from upstream")
        self.assertEqual(fake_session.calls[0]["json"]["model"], "openai/gpt-4o-mini")

    def test_chat_rejects_invalid_payload(self):
        client = make_app().test_client()

        response = client.post("/chat", json={"messages": []})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_request")

    def test_chat_surfaces_upstream_http_message(self):
        fake_session = FakeSession(
            response=FakeResponse(
                payload={},
                status_code=429,
                http_error_payload={"error": {"message": "Provider limit reached"}},
            )
        )
        client = make_app(session=fake_session).test_client()

        response = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["error"]["message"], "Provider limit reached")

    def test_rate_limit_blocks_second_request_when_limit_is_one(self):
        fake_session = FakeSession(
            response=FakeResponse(
                {"choices": [{"message": {"content": "Hello from upstream"}}]},
                status_code=200,
            )
        )
        app = create_app(session=fake_session, rate_limiter=InMemoryRateLimiter())
        app.config.update(
            TESTING=True,
            API_KEY="test-api-key",
            RATE_LIMIT_REQUESTS=1,
            RATE_LIMIT_WINDOW_SECONDS=60,
        )
        client = app.test_client()

        first = client.post("/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
        second = client.post("/chat", json={"messages": [{"role": "user", "content": "Again"}]})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.get_json()["error"]["code"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
