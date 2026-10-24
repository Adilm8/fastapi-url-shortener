from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

import app.main as main_module
from tests.test_main import client, engine, TestingSessionLocal, ADMIN_KEY, ADMIN_HEADERS, auth
from app.config import settings
from app.database import Base
from app.models import URLItem
from app.redis_client import clear_all_redis_keys
from app.utils import get_client_ip, escape_like
from app.qr import generate_qr_matrix


@pytest.fixture(autouse=True)
def setup_and_teardown_hardening_db(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", ADMIN_KEY)
    Base.metadata.create_all(bind=engine)
    clear_all_redis_keys()
    yield
    Base.metadata.drop_all(bind=engine)
    clear_all_redis_keys()


def create(**payload):
    payload.setdefault("long_url", "https://example.com/target")
    res = client.post("/api/v1/urls", json=payload)
    assert res.status_code == 201, res.text
    return res


# Ownership & access control

def test_create_returns_manage_token_once():
    """The manage token is returned at creation and never exposed by read endpoints."""
    res = create(custom_code="tok-once")
    assert len(res.json()["manage_token"]) >= 32

    listed = client.get("/api/v1/urls?codes=tok-once").json()
    assert listed["items"][0]["manage_token"] is None


@pytest.mark.parametrize("method,body", [("patch", {"is_active": False}), ("delete", None)])
def test_manage_endpoints_require_token(method, body):
    """PATCH/DELETE without credentials is 401; with someone else's token it is 403."""
    create(custom_code="guarded")
    other = create(custom_code="other-link")
    kwargs = {"json": body} if body else {}

    assert getattr(client, method)("/api/v1/urls/guarded", **kwargs).status_code == 401
    forbidden = getattr(client, method)("/api/v1/urls/guarded", headers=auth(other), **kwargs)
    assert forbidden.status_code == 403
    # The link is untouched
    assert client.get("/guarded", follow_redirects=False).status_code == 307


def test_admin_key_manages_any_link():
    create(custom_code="admin-mng")
    res = client.patch("/api/v1/urls/admin-mng", json={"is_active": False}, headers=ADMIN_HEADERS)
    assert res.status_code == 200
    assert client.delete("/api/v1/urls/admin-mng", headers={"X-API-Key": ADMIN_KEY}).status_code == 204


def test_legacy_link_without_token_is_admin_only():
    """Links created before tokens existed cannot be claimed with an arbitrary token."""
    db = TestingSessionLocal()
    db.add(URLItem(short_code="legacy1", long_url="https://legacy.example.com/"))
    db.commit()
    db.close()

    res = client.patch("/api/v1/urls/legacy1", json={"is_active": False}, headers={"Authorization": "Bearer guess"})
    assert res.status_code == 403
    assert client.patch("/api/v1/urls/legacy1", json={"is_active": False}, headers=ADMIN_HEADERS).status_code == 200


def test_admin_disabled_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    create(custom_code="no-admin")
    assert client.get("/api/v1/urls", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.delete("/api/v1/urls/no-admin", headers={"X-API-Key": ""}).status_code == 401


def test_listing_all_links_requires_admin():
    create(custom_code="private-1")
    assert client.get("/api/v1/urls").status_code == 401
    assert client.get("/api/v1/urls", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/urls", headers=ADMIN_HEADERS).json()["total"] == 1


def test_listing_by_codes_returns_only_requested_links():
    create(custom_code="mine-1")
    create(custom_code="mine-2", long_url="https://example.com/two")
    create(custom_code="theirs", long_url="https://example.com/three")

    res = client.get("/api/v1/urls?codes=mine-1,mine-2,does-not-exist")
    assert res.status_code == 200
    assert sorted(i["short_code"] for i in res.json()["items"]) == ["mine-1", "mine-2"]

    too_many = ",".join(f"c{i}" for i in range(101))
    assert client.get(f"/api/v1/urls?codes={too_many}").status_code == 400


def test_search_treats_wildcards_literally():
    create(custom_code="plain-1", long_url="https://example.com/plain")
    create(custom_code="pct-1", long_url="https://example.com/50%25off")

    res = client.get("/api/v1/urls?search=%25", headers=ADMIN_HEADERS).json()
    assert [i["short_code"] for i in res["items"]] == ["pct-1"]
    assert client.get("/api/v1/urls?search=_", headers=ADMIN_HEADERS).json()["total"] == 0


def test_unit_escape_like():
    assert escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"


# Client IP resolution / rate limiting

def test_unit_get_client_ip_trusted_hops(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    assert get_client_ip("6.6.6.6, 203.0.113.9", "10.0.0.1") == "203.0.113.9"
    assert get_client_ip("203.0.113.9", "10.0.0.1") == "203.0.113.9"
    assert get_client_ip(None, "10.0.0.1") == "10.0.0.1"

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 2)
    assert get_client_ip("6.6.6.6, 203.0.113.9, 10.1.1.1", "10.0.0.1") == "203.0.113.9"

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)
    assert get_client_ip("6.6.6.6", "10.0.0.1") == "10.0.0.1"


def test_rate_limit_cannot_be_bypassed_by_spoofing_forwarded_for():
    """A client-prepended X-Forwarded-For entry must not create a fresh rate limit bucket."""
    statuses = []
    for i in range(settings.RATE_LIMIT_REQUESTS + 2):
        res = client.post(
            "/api/v1/urls",
            json={"long_url": f"https://example.com/spoof-{i}"},
            headers={"X-Forwarded-For": f"10.9.9.{i}, 198.51.100.77"},
        )
        statuses.append(res.status_code)
    assert statuses.count(201) == settings.RATE_LIMIT_REQUESTS
    assert statuses[-1] == 429


# Validation

def test_explicit_null_is_active_rejected():
    res = create(custom_code="null-act")
    patch_res = client.patch("/api/v1/urls/null-act", json={"is_active": None}, headers=auth(res))
    assert patch_res.status_code == 422


def test_overlong_long_url_rejected():
    long_url = "https://example.com/" + "a" * settings.MAX_LONG_URL_LENGTH
    assert client.post("/api/v1/urls", json={"long_url": long_url}).status_code == 422


def test_batch_error_reports_failing_index():
    create(custom_code="taken-code")
    res = client.post("/api/v1/urls/batch", json={"urls": [
        {"long_url": "https://example.com/ok"},
        {"long_url": "https://example.com/dup", "custom_code": "taken-code"},
    ]})
    assert res.status_code == 409
    assert res.json()["detail"].startswith("urls[1]:")


# Timezones

def test_timestamps_serialized_as_utc():
    data = create(custom_code="utc-ts", expires_in_seconds=3600).json()
    assert data["created_at"].endswith("Z")
    assert data["expires_at"].endswith("Z")
    health = client.get("/health").json()
    assert health["timestamp"].endswith("Z")


def test_offset_expires_at_normalized_to_utc():
    """An expiry given with a +05:00 offset is stored as the same instant in UTC."""
    local = datetime.now(timezone(timedelta(hours=5))).replace(microsecond=0) + timedelta(hours=2)
    data = create(custom_code="tz-off", expires_at=local.isoformat()).json()
    stored = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    assert stored == local

    # 30 minutes from now written in a -05:00 zone: dropping the offset would make it look 4.5h expired
    soon = datetime.now(timezone(timedelta(hours=-5))) + timedelta(minutes=30)
    create(custom_code="tz-soon", long_url="https://example.com/soon", expires_at=soon.isoformat())
    assert client.get("/tz-soon", follow_redirects=False).status_code == 307


# Redirect behaviour

def test_head_request_redirects_without_counting_click():
    create(custom_code="head-me", max_clicks=1)
    head = client.head("/head-me", follow_redirects=False)
    assert head.status_code == 307
    assert head.headers["location"] == "https://example.com/target"
    assert client.get("/api/v1/urls/head-me/stats").json()["click_count"] == 0
    # The single allowed click is still available
    assert client.get("/head-me", follow_redirects=False).status_code == 307
    assert client.head("/head-me", follow_redirects=False).status_code == 410


def test_max_clicks_counts_buffered_clicks():
    """Clicks buffered before a cap was added count toward the cap."""
    res = create(custom_code="buf-cap")
    with patch("app.main.persist_clicks_to_db"):  # keep clicks in the buffer
        for _ in range(2):
            client.get("/buf-cap", follow_redirects=False)
    client.patch("/api/v1/urls/buf-cap", json={"max_clicks": 3}, headers=auth(res))
    assert client.get("/buf-cap", follow_redirects=False).status_code == 307
    assert client.get("/buf-cap", follow_redirects=False).status_code == 410


def test_expired_cache_entry_is_not_served():
    create(custom_code="cache-exp")
    stale = {"long_url": "https://example.com/target", "expires_at": "2020-01-01T00:00:00", "max_clicks": None}
    with patch("app.main.get_cached_url", return_value=stale):
        assert client.get("/cache-exp", follow_redirects=False).status_code == 410


def test_uncapped_cache_hit_skips_database():
    """A cached, uncapped link redirects without querying the database."""
    create(custom_code="fast-path")
    original = main_module.app.dependency_overrides[main_module.get_db]

    def db_that_must_not_be_queried():
        session = MagicMock()
        session.query.side_effect = AssertionError("redirect queried the database")
        yield session

    main_module.app.dependency_overrides[main_module.get_db] = db_that_must_not_be_queried
    try:
        res = client.get("/fast-path", follow_redirects=False)
    finally:
        main_module.app.dependency_overrides[main_module.get_db] = original
    assert res.status_code == 307
    assert client.get("/api/v1/urls/fast-path/stats").json()["click_count"] == 1


# QR

def test_qr_rejects_oversized_payload():
    with pytest.raises(ValueError):
        generate_qr_matrix("https://example.com/" + "x" * 400)


# Schema migration

def test_ensure_schema_adds_manage_token_column(monkeypatch):
    legacy_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with legacy_engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, short_code VARCHAR(16) UNIQUE NOT NULL, "
            "long_url VARCHAR(2048) NOT NULL, click_count INTEGER NOT NULL DEFAULT 0, "
            "created_at DATETIME NOT NULL, expires_at DATETIME, is_active BOOLEAN NOT NULL DEFAULT 1, "
            "max_clicks INTEGER, last_clicked_at DATETIME)"
        ))
    monkeypatch.setattr(main_module, "engine", legacy_engine)
    main_module.ensure_schema()
    main_module.ensure_schema()  # idempotent
    columns = {c["name"] for c in inspect(legacy_engine).get_columns("urls")}
    assert "manage_token_hash" in columns


# Redis resilience

def test_redis_reconnects_after_outage(monkeypatch):
    """An unreachable Redis is re-probed after a backoff instead of being abandoned forever."""
    import redis
    import app.redis_client as rc

    fake = MagicMock()
    fake.ping.side_effect = [redis.exceptions.ConnectionError("down"), True]
    monkeypatch.setattr(rc, "redis_client", fake)
    monkeypatch.setattr(rc, "_redis_available", False)
    monkeypatch.setattr(rc, "_next_reconnect_at", 0.0)

    assert rc._redis() is None          # probe fails
    assert rc._redis() is None          # inside backoff window: no second probe
    assert fake.ping.call_count == 1

    monkeypatch.setattr(rc, "_next_reconnect_at", 0.0)
    assert rc._redis() is fake          # backoff elapsed, server is back
    assert rc._redis() is fake
    assert fake.ping.call_count == 2


def test_redis_error_mid_request_falls_back_to_memory(monkeypatch):
    import redis
    import app.redis_client as rc

    fake = MagicMock()
    fake.incr.side_effect = redis.exceptions.ConnectionError("lost")
    monkeypatch.setattr(rc, "redis_client", fake)
    monkeypatch.setattr(rc, "_redis_available", True)

    assert rc.increment_url_clicks("fallback-code") == 1
    assert rc._redis_available is False
