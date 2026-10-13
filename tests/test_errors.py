"""Error handling: LLM provider failures and database connection problems (docs/ERROR_HANDLING.md).

Failures are simulated (scripted models that raise provider errors, pools that can't connect);
no LLM calls, no real outages.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.exceptions import FallbackExceptionGroup, ModelHTTPError
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import db
from ai import agent, resilience, store
from ai.security import Principal

C1 = Principal(role="customer", user_id=9_100_031, username="c1", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455")
QUOTA_BODY = {"error": {"code": 429, "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"},
                                                 {"retryDelay": "83760s"}]}}
RPM_BODY = {"error": {"code": 429, "details": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"},
                                               {"retryDelay": "2s"}]}}


@pytest.fixture(autouse=True)
def clean():
    resilience.reset()
    now = db.query_one("SELECT NOW() AS t")["t"]
    db.execute("REPLACE INTO ai_limits (scope, messages_per_day, input_tokens_per_day, output_tokens_per_day, "
               "max_reply_tokens, requests_per_minute, max_tool_calls_per_turn, blocked, updated_at) "
               "VALUES (%s, 1000, 100000000, 10000000, 1000, 1000, 8, 0, %s)", (f"user:{C1.user_id}", now))
    yield
    resilience.reset()
    for t in ("ai_usage", "ai_usage_daily"):
        db.execute(f"DELETE FROM {t} WHERE subject = %s", (f"user:{C1.user_id}",))
    db.execute("DELETE FROM ai_limits WHERE scope = %s", (f"user:{C1.user_id}",))
    db.execute("DELETE FROM ai_conversations WHERE owner_user_id = %s", (C1.user_id,))


def failing(exc_factory, after_ok: int = 0, answer: str = "All good."):
    """A model that raises the given error (from its n-th call on), else answers."""
    calls = {"n": 0}

    async def stream(messages, info):
        calls["n"] += 1
        if calls["n"] > after_ok:
            raise exc_factory()
        yield answer

    model = FunctionModel(stream_function=stream, model_name="test-model")
    model.calls = calls
    return model


def run(model, message="hi"):
    async def go():
        turn = agent.prepare_turn(C1, message, model=model)
        events = [e async for e in turn.run()]
        return turn, events
    return asyncio.run(go())


def text(events):
    out = ""
    for e in events:
        if e["type"] == "reset":
            out = ""
        elif e["type"] == "text":
            out += e["delta"]
    return out.strip()


# ---- classifying provider errors -------------------------------------------------------------


def test_errors_are_classified_by_kind():
    assert resilience.classify(ModelHTTPError(429, "m", QUOTA_BODY)) == "quota"
    assert resilience.classify(ModelHTTPError(429, "m", RPM_BODY)) == "rate_limited"
    assert resilience.retry_after_s(ModelHTTPError(429, "m", RPM_BODY)) == 2
    assert resilience.classify(ModelHTTPError(503, "m", "overloaded")) == "provider_error"
    assert resilience.classify(ModelHTTPError(401, "m", "bad key")) == "auth"
    assert resilience.classify(ModelHTTPError(400, "m", "invalid")) == "bad_request"
    assert resilience.classify(TimeoutError()) == "timeout"
    group = FallbackExceptionGroup("all failed", [ModelHTTPError(429, "a", RPM_BODY), ModelHTTPError(429, "b", QUOTA_BODY)])
    assert resilience.classify(group) == "quota"   # the worst kind decides


def test_circuit_opens_after_repeated_failures_and_closes_on_success():
    for _ in range(resilience.FAILURES_TO_OPEN - 1):
        resilience.record_failure("provider_error")
    assert resilience.is_open() == (False, None)
    resilience.record_failure("provider_error")
    assert resilience.is_open() == (True, "provider_error")
    resilience.record_success()
    assert resilience.is_open() == (False, None)


def test_quota_pauses_the_provider_at_once_but_rechecks_within_30_minutes():
    resilience.record_failure("quota", ModelHTTPError(429, "m", QUOTA_BODY))
    is_open, why = resilience.is_open()
    assert is_open and why == "quota"
    assert resilience.state()["paused_for_s"] <= resilience.MAX_OPEN_S


# ---- a turn when the provider fails ----------------------------------------------------------


def test_provider_outage_gives_a_specific_message_and_does_not_count():
    turn, events = run(failing(lambda: ModelHTTPError(503, "m", "overloaded")))
    assert text(events).startswith(resilience.MESSAGES["provider_error"])   # plus a hybrid hint when on
    assert store.usage_today(f"user:{C1.user_id}")["requests"] == 0          # not counted against the customer
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_usage WHERE subject = %s", (f"user:{C1.user_id}",))["n"] == 1


def test_short_rate_limit_is_retried_once():
    calls = {"n": 0}

    async def stream(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModelHTTPError(429, "m", RPM_BODY)
        yield "Answered after a retry."

    started = time.monotonic()
    turn, events = run(FunctionModel(stream_function=stream))
    assert text(events) == "Answered after a retry." and calls["n"] == 2
    assert time.monotonic() - started >= 2      # waited the provider's suggested delay (2 s)
    assert store.usage_today(f"user:{C1.user_id}")["requests"] == 1


def test_no_retry_after_a_tool_has_run():
    calls = {"n": 0}

    async def stream(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {0: DeltaToolCall(name="my_account", json_args='{"view": "cart"}', tool_call_id="t1")}
            return
        raise ModelHTTPError(429, "m", RPM_BODY)

    turn, events = run(FunctionModel(stream_function=stream), "what's in my cart")
    assert text(events).startswith(resilience.MESSAGES["rate_limited"])
    assert calls["n"] == 2   # the tool call, then the failure; no repeat of the whole turn


def test_paused_provider_answers_at_once_without_calling_the_model():
    resilience.record_failure("quota", ModelHTTPError(429, "m", QUOTA_BODY))
    model = failing(lambda: AssertionError("the model must not be called while paused"))
    turn, events = run(model)
    assert text(events).startswith(resilience.MESSAGES["quota"]) and model.calls["n"] == 0
    assert turn.route["llm_skipped"] == "quota"


def test_whole_turn_timeout(monkeypatch):
    monkeypatch.setattr(agent, "TURN_TIMEOUT_S", 0.5)

    async def stream(messages, info):
        await asyncio.sleep(3)
        yield "too late"

    turn, events = run(FunctionModel(stream_function=stream))
    assert text(events) == resilience.MESSAGES["timeout"]


def test_fallback_model_answers_when_the_main_one_fails():
    main = failing(lambda: ModelHTTPError(429, "main", QUOTA_BODY))
    backup = failing(lambda: None, after_ok=10, answer="Answered by the fallback.")
    turn, events = run(FallbackModel(main, backup))
    assert text(events) == "Answered by the fallback."
    assert resilience.is_open() == (False, None)   # the turn succeeded


# ---- database --------------------------------------------------------------------------------


def test_pool_has_a_ceiling_and_reports_busy(monkeypatch):
    monkeypatch.setattr(db, "POOL_WAIT_S", 0.2)
    pool = db.Pool("MYSQL_USER", "MYSQL_PASSWORD", max_open=1)
    held = pool.acquire()
    try:
        with pytest.raises(db.DatabaseBusy):
            pool.acquire()
    finally:
        pool.release(held, healthy=True)
    pool.release(pool.acquire(), healthy=False)   # a freed slot can be used again


def test_unreachable_database_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setenv("MYSQL_PORT", "1")   # nothing listens there
    pool = db.Pool("MYSQL_USER", "MYSQL_PASSWORD", max_open=2)
    with pytest.raises(db.DatabaseUnavailable):
        pool.acquire()
    assert pool.ping() is False


def test_reads_retry_once_after_a_lost_connection(monkeypatch):
    real = db.cursor
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise db.DatabaseUnavailable("lost the database connection (2013)")
        return real()

    monkeypatch.setattr(db, "cursor", flaky)
    assert db.query_one("SELECT 1 AS one") == {"one": 1} and calls["n"] == 2


@pytest.fixture
def client():
    from api.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_api_turns_a_database_outage_into_a_friendly_503(client, monkeypatch):
    """What broke during the MySQL crash: the sign-in page's test logins (and every data page)."""
    import api.main as main

    def down(*a, **k):
        raise db.DatabaseUnavailable("MySQL is not reachable (2003)")

    monkeypatch.setattr(main, "demo_accounts", down)
    monkeypatch.setattr(main, "DEMO_LOGIN", True)
    r = client.get("/api/auth/demo-accounts", headers={"X-Request-ID": "test-req-1"})
    assert r.status_code == 503 and r.headers["Retry-After"]
    body = r.json()
    assert body == {"detail": "The store is temporarily unavailable. Please try again in a minute.",
                    "code": "db_unavailable", "request_id": "test-req-1"}
    assert r.headers["X-Request-ID"] == "test-req-1"


def test_unexpected_errors_never_show_internals(client, monkeypatch):
    import api.main as main

    def broken(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(main, "demo_accounts", broken)
    monkeypatch.setattr(main, "DEMO_LOGIN", True)
    r = client.get("/api/auth/demo-accounts")
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
    assert "secret internal detail" not in r.text and r.json()["request_id"]


def test_health_and_ready(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    r = client.get("/api/ready")
    assert r.status_code == 200 and r.json()["checks"]["database"] is True
