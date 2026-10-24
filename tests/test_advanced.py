import io
from datetime import datetime, timezone, timedelta
from PIL import Image
import pytest

from tests.test_main import client, engine, TestingSessionLocal
from app.config import settings
from app.database import Base
from app.models import URLItem
from app.redis_client import clear_all_redis_keys
from app.utils import (
    parse_user_agent,
    normalize_referrer,
    hash_client_ip,
    is_reserved_short_code,
    is_self_referential_url,
    is_url_expired,
)
from app.qr import generate_qr_svg, generate_qr_png, generate_qr_matrix


@pytest.fixture(autouse=True)
def setup_and_teardown_advanced_db():
    Base.metadata.create_all(bind=engine)
    clear_all_redis_keys()
    yield
    Base.metadata.drop_all(bind=engine)
    clear_all_redis_keys()


# Observability & Health

def test_health_check_healthy():
    """Test GET /health returns 200, healthy status, database status, and cache backend."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["version"] == settings.APP_VERSION
    assert data["database"] == "healthy"
    assert data["cache_backend"] in ("in-memory", "redis")
    assert "timestamp" in data


def test_metrics_endpoint_initial_state():
    """Test GET /api/v1/metrics returns empty initial telemetry metrics."""
    response = client.get("/api/v1/metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["total_urls"] == 0
    assert data["active_urls"] == 0
    assert data["total_clicks"] == 0
    assert data["cache_hits"] == 0
    assert data["cache_misses"] == 0
    assert data["cache_hit_ratio"] == 0.0
    assert data["cache_backend"] in ("in-memory", "redis")


def test_metrics_cache_hit_and_miss_tracking():
    """Test that metrics accurately track URL counts, click counts, cache hits, and cache misses."""
    create_res = client.post(
        "/api/v1/urls",
        json={"long_url": "https://fastapi.tiangolo.com", "custom_code": "fastapi-docs"}
    )
    assert create_res.status_code == 201

    # First redirect hits cache (pre-populated during creation)
    redir1 = client.get("/fastapi-docs", follow_redirects=False)
    assert redir1.status_code == 307

    # Second redirect also hits cache
    redir2 = client.get("/fastapi-docs", follow_redirects=False)
    assert redir2.status_code == 307

    metrics_res = client.get("/api/v1/metrics")
    assert metrics_res.status_code == 200
    m_data = metrics_res.json()
    assert m_data["total_urls"] == 1
    assert m_data["active_urls"] == 1
    assert m_data["total_clicks"] == 2
    assert m_data["cache_hits"] >= 2

    # Querying a non-existent short code triggers a cache miss
    miss_res = client.get("/non-existent-code-for-miss", follow_redirects=False)
    assert miss_res.status_code == 404

    m_after = client.get("/api/v1/metrics").json()
    assert m_after["cache_misses"] >= 1
    assert m_after["cache_hit_ratio"] > 0.0


def test_x_process_time_ms_header_present():
    """Test that observability middleware attaches X-Process-Time-Ms to HTTP responses."""
    response = client.get("/health")
    assert response.status_code == 200
    assert "X-Process-Time-Ms" in response.headers
    elapsed_ms = float(response.headers["X-Process-Time-Ms"])
    assert elapsed_ms >= 0.0


def test_rate_limit_headers_on_rate_limited_endpoint():
    """Test that rate-limited endpoints include X-RateLimit-Limit, Remaining, and Reset headers."""
    response = client.post(
        "/api/v1/urls",
        json={"long_url": "https://example.com/rl-header-test"}
    )
    assert response.status_code == 201
    assert "X-RateLimit-Limit" in response.headers
    assert "X-RateLimit-Remaining" in response.headers
    assert "X-RateLimit-Reset" in response.headers

    limit = int(response.headers["X-RateLimit-Limit"])
    remaining = int(response.headers["X-RateLimit-Remaining"])
    assert limit == settings.RATE_LIMIT_REQUESTS
    assert remaining == settings.RATE_LIMIT_REQUESTS - 1


# Dashboard UI

def test_dashboard_ui_returns_linkpulse_html():
    """Test GET /dashboard returns HTTP 200 and renders the single-page HTML containing LinkPulse."""
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "LinkPulse" in response.text
    assert "<!DOCTYPE html>" in response.text


# Security Guardrails

def test_reserved_custom_codes_rejected():
    """Test that reserved system keywords return HTTP 400 Bad Request."""
    reserved_codes = ["docs", "api", "health", "dashboard", "metrics", "redoc", "static"]
    for code in reserved_codes:
        res = client.post(
            "/api/v1/urls",
            json={"long_url": "https://example.com", "custom_code": code}
        )
        assert res.status_code == 400
        assert "reserved system keyword" in res.json()["detail"].lower()

    # Case-insensitive rejection check
    res_upper = client.post(
        "/api/v1/urls",
        json={"long_url": "https://example.com", "custom_code": "DOCS"}
    )
    assert res_upper.status_code == 400
    assert "reserved system keyword" in res_upper.json()["detail"].lower()


def test_self_referential_url_rejected():
    """Test that URLs pointing to the shortener's own domain are rejected with HTTP 400."""
    payloads = [
        {"long_url": "http://testserver/some-path"},
        {"long_url": "http://testserver/api/v1/urls"},
        {"long_url": "http://testserver:80/loop"},
    ]
    for payload in payloads:
        res = client.post("/api/v1/urls", json=payload)
        assert res.status_code == 400
        assert "Cannot shorten a URL pointing to this shortener service." in res.json()["detail"]


def test_past_expires_at_rejected():
    """Test that specifying an expires_at timestamp in the past returns HTTP 400."""
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    res = client.post(
        "/api/v1/urls",
        json={"long_url": "https://example.com", "expires_at": past_iso}
    )
    assert res.status_code == 400
    assert "Expiration time cannot be in the past." in res.json()["detail"]


def test_patch_with_self_referential_url_rejected():
    """Test that updating a URL's destination to a self-referential link is rejected with HTTP 400."""
    create_res = client.post(
        "/api/v1/urls",
        json={"long_url": "https://valid.com", "custom_code": "patch-loop"}
    )
    assert create_res.status_code == 201

    patch_res = client.patch(
        "/api/v1/urls/patch-loop",
        json={"long_url": "http://testserver/forbidden"}
    )
    assert patch_res.status_code == 400
    assert "Cannot shorten a URL pointing to this shortener service." in patch_res.json()["detail"]


# Link Expiration & Max Clicks

def test_link_ttl_expires_in_seconds():
    """Test creating a short URL with expires_in_seconds populates expires_at and redirects properly."""
    res = client.post(
        "/api/v1/urls",
        json={"long_url": "https://example.com/ttl-test", "custom_code": "ttl-code", "expires_in_seconds": 3600}
    )
    assert res.status_code == 201
    data = res.json()
    assert data["expires_at"] is not None

    redir = client.get("/ttl-code", follow_redirects=False)
    assert redir.status_code == 307
    assert redir.headers["location"] == "https://example.com/ttl-test"


def test_expired_link_redirect_returns_410_gone():
    """Test that when a link's expires_at is in the past, GET /{short_code} returns HTTP 410 Gone."""
    client.post(
        "/api/v1/urls",
        json={"long_url": "https://example.com/will-expire", "custom_code": "expired-code"}
    )

    db = TestingSessionLocal()
    item = db.query(URLItem).filter(URLItem.short_code == "expired-code").first()
    item.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    db.close()

    redir = client.get("/expired-code", follow_redirects=False)
    assert redir.status_code == 410
    assert "expired" in redir.json()["detail"].lower()


def test_expired_link_stats_marked_expired():
    """Test that GET /api/v1/urls/{code}/stats reflects is_expired: True when expired."""
    client.post(
        "/api/v1/urls",
        json={"long_url": "https://example.com/expired-stats", "custom_code": "exp-stats"}
    )

    db = TestingSessionLocal()
    item = db.query(URLItem).filter(URLItem.short_code == "exp-stats").first()
    item.expires_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db.commit()
    db.close()

    stats_res = client.get("/api/v1/urls/exp-stats/stats")
    assert stats_res.status_code == 200
    assert stats_res.json()["is_expired"] is True


def test_max_clicks_self_destruct_lifecycle():
    """Test max_clicks self-destruct: 1st and 2nd redirects return 307, 3rd redirect returns 410 Gone."""
    create_res = client.post(
        "/api/v1/urls",
        json={"long_url": "https://destruct.example.com", "custom_code": "self-destruct", "max_clicks": 2}
    )
    assert create_res.status_code == 201

    # 1st redirect -> 307
    res1 = client.get("/self-destruct", follow_redirects=False)
    assert res1.status_code == 307

    # 2nd redirect -> 307
    res2 = client.get("/self-destruct", follow_redirects=False)
    assert res2.status_code == 307

    # 3rd redirect -> 410 Gone
    res3 = client.get("/self-destruct", follow_redirects=False)
    assert res3.status_code == 410
    assert "click limit" in res3.json()["detail"].lower()


# CRUD Operations

def test_list_urls_pagination():
    """Test GET /api/v1/urls pagination with skip and limit query parameters."""
    for i in range(5):
        client.post("/api/v1/urls", json={"long_url": f"https://example.com/page-{i}"})

    p1 = client.get("/api/v1/urls?skip=0&limit=2").json()
    assert p1["total"] == 5
    assert len(p1["items"]) == 2
    assert p1["skip"] == 0
    assert p1["limit"] == 2

    p2 = client.get("/api/v1/urls?skip=2&limit=2").json()
    assert p2["total"] == 5
    assert len(p2["items"]) == 2
    assert p2["skip"] == 2

    p3 = client.get("/api/v1/urls?skip=4&limit=2").json()
    assert p3["total"] == 5
    assert len(p3["items"]) == 1
    assert p3["skip"] == 4


def test_list_urls_search_filter():
    """Test GET /api/v1/urls search filter matching short_code or long_url substrings."""
    client.post("/api/v1/urls", json={"long_url": "https://alpha.example.com", "custom_code": "search-alpha"})
    client.post("/api/v1/urls", json={"long_url": "https://beta.example.com/special-keyword", "custom_code": "search-beta"})

    # Search by short code substring
    res_code = client.get("/api/v1/urls?search=alpha").json()
    assert res_code["total"] == 1
    assert res_code["items"][0]["short_code"] == "search-alpha"

    # Search by destination long URL substring
    res_url = client.get("/api/v1/urls?search=special-keyword").json()
    assert res_url["total"] == 1
    assert res_url["items"][0]["short_code"] == "search-beta"

    # Search non-matching term
    res_none = client.get("/api/v1/urls?search=no-such-term").json()
    assert res_none["total"] == 0
    assert len(res_none["items"]) == 0


def test_list_urls_active_only_filter():
    """Test GET /api/v1/urls?active_only=true filters out deactivated and expired URLs."""
    # Active valid URL
    client.post("/api/v1/urls", json={"long_url": "https://active.com", "custom_code": "act-1"})

    # Inactive URL
    client.post("/api/v1/urls", json={"long_url": "https://inactive.com", "custom_code": "inact-1"})
    client.patch("/api/v1/urls/inact-1", json={"is_active": False})

    # Expired URL
    client.post("/api/v1/urls", json={"long_url": "https://expired.com", "custom_code": "exp-1"})
    db = TestingSessionLocal()
    e_item = db.query(URLItem).filter(URLItem.short_code == "exp-1").first()
    e_item.expires_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.commit()
    db.close()

    res = client.get("/api/v1/urls?active_only=true").json()
    assert res["total"] == 1
    assert res["items"][0]["short_code"] == "act-1"


def test_patch_url_deactivate_and_reactivate():
    """Test PATCH /api/v1/urls/{code} deactivating returns 410 on redirect and reactivating restores 307."""
    client.post("/api/v1/urls", json={"long_url": "https://toggle.com", "custom_code": "toggle-me"})
    assert client.get("/toggle-me", follow_redirects=False).status_code == 307

    # Deactivate
    p1 = client.patch("/api/v1/urls/toggle-me", json={"is_active": False})
    assert p1.status_code == 200
    assert p1.json()["is_active"] is False

    # Redirect returns 410 Gone
    assert client.get("/toggle-me", follow_redirects=False).status_code == 410

    # Reactivate
    p2 = client.patch("/api/v1/urls/toggle-me", json={"is_active": True})
    assert p2.status_code == 200
    assert p2.json()["is_active"] is True

    # Redirect restored to 307
    assert client.get("/toggle-me", follow_redirects=False).status_code == 307


def test_patch_url_update_long_url():
    """Test PATCH /api/v1/urls/{code} updating long_url alters the redirect destination and cache."""
    client.post("/api/v1/urls", json={"long_url": "https://original-domain.com", "custom_code": "patch-target"})
    r1 = client.get("/patch-target", follow_redirects=False)
    assert r1.status_code == 307
    assert r1.headers["location"] == "https://original-domain.com/"

    # Update destination target
    p = client.patch("/api/v1/urls/patch-target", json={"long_url": "https://updated-domain.com/landing"})
    assert p.status_code == 200
    assert p.json()["long_url"] == "https://updated-domain.com/landing"

    r2 = client.get("/patch-target", follow_redirects=False)
    assert r2.status_code == 307
    assert r2.headers["location"] == "https://updated-domain.com/landing"


def test_patch_url_update_max_clicks():
    """Test PATCH /api/v1/urls/{code} updating max_clicks."""
    client.post("/api/v1/urls", json={"long_url": "https://patch-clicks.com", "custom_code": "patch-clicks"})
    p = client.patch("/api/v1/urls/patch-clicks", json={"max_clicks": 5})
    assert p.status_code == 200
    assert p.json()["max_clicks"] == 5


def test_patch_url_not_found():
    """Test PATCH /api/v1/urls/{code} on a non-existent short code returns HTTP 404."""
    res = client.patch("/api/v1/urls/ghost-url", json={"is_active": False})
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_delete_url_complete_lifecycle():
    """Test DELETE /api/v1/urls/{code} removes database records, click events, and cache mappings."""
    client.post("/api/v1/urls", json={"long_url": "https://delete-target.com", "custom_code": "del-code"})
    client.get("/del-code", follow_redirects=False)

    del_res = client.delete("/api/v1/urls/del-code")
    assert del_res.status_code == 204

    # Subsequent redirect returns 404
    assert client.get("/del-code", follow_redirects=False).status_code == 404

    # Subsequent stats returns 404
    assert client.get("/api/v1/urls/del-code/stats").status_code == 404

    # Subsequent delete returns 404
    assert client.delete("/api/v1/urls/del-code").status_code == 404


def test_delete_url_not_found():
    """Test DELETE /api/v1/urls/{code} returns HTTP 404 for non-existent code."""
    res = client.delete("/api/v1/urls/never-existed")
    assert res.status_code == 404


# Batch Operations

def test_batch_shortening_success():
    """Test POST /api/v1/urls/batch creates multiple URLs in one request and returns 201 Created."""
    batch_payload = {
        "urls": [
            {"long_url": "https://site1.example.com"},
            {"long_url": "https://site2.example.com"},
            {"long_url": "https://site3.example.com"},
        ]
    }
    res = client.post("/api/v1/urls/batch", json=batch_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["count"] == 3
    assert len(data["items"]) == 3
    for item in data["items"]:
        assert "short_code" in item
        assert "qr_code_url" in item
        assert "http://testserver/" in item["short_url"]


def test_batch_shortening_with_custom_code():
    """Test POST /api/v1/urls/batch accepts items with optional custom_code aliases."""
    batch_payload = {
        "urls": [
            {"long_url": "https://custom-batch.example.com", "custom_code": "batch-alias"},
            {"long_url": "https://auto-batch.example.com"},
        ]
    }
    res = client.post("/api/v1/urls/batch", json=batch_payload)
    assert res.status_code == 201
    items = res.json()["items"]
    assert items[0]["short_code"] == "batch-alias"


def test_batch_shortening_validation_errors():
    """Test POST /api/v1/urls/batch rejects empty batches and batches exceeding MAX_BATCH_SIZE."""
    # Empty batch
    res_empty = client.post("/api/v1/urls/batch", json={"urls": []})
    assert res_empty.status_code == 422

    # Exceeding MAX_BATCH_SIZE (20)
    oversized = [{"long_url": f"https://over.com/{i}"} for i in range(21)]
    res_over = client.post("/api/v1/urls/batch", json={"urls": oversized})
    assert res_over.status_code in (400, 422)


# Analytics & Telemetry

def test_rich_analytics_breakdown_and_unique_visitors():
    """
    Simulate clicks with distinct User-Agents, Referrers, and IPs,
    verifying unique visitors, device/browser/OS breakdown, and clicks by date.
    """
    client.post(
        "/api/v1/urls",
        json={"long_url": "https://analytics-demo.com", "custom_code": "telemetry-code"}
    )

    # Click 1: iPhone Safari, Google referrer, IP 203.0.113.1
    client.get(
        "/telemetry-code",
        follow_redirects=False,
        headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
            "Referer": "https://www.google.com/search?q=fastapi",
            "X-Forwarded-For": "203.0.113.1"
        }
    )

    # Click 2: Windows Chrome, Hacker News referrer, IP 203.0.113.2
    client.get(
        "/telemetry-code",
        follow_redirects=False,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            "Referer": "https://news.ycombinator.com/item?id=12345",
            "X-Forwarded-For": "203.0.113.2"
        }
    )

    # Click 3: Googlebot, Direct referrer, IP 203.0.113.1 (same IP as Click 1)
    client.get(
        "/telemetry-code",
        follow_redirects=False,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "X-Forwarded-For": "203.0.113.1"
        }
    )

    stats = client.get("/api/v1/urls/telemetry-code/stats").json()
    assert stats["click_count"] == 3
    assert stats["unique_visitors"] == 2
    assert stats["device_breakdown"].get("Mobile") == 1
    assert stats["device_breakdown"].get("Desktop") == 1
    assert stats["device_breakdown"].get("Bot") == 1
    assert stats["browser_breakdown"].get("Safari") == 1
    assert stats["browser_breakdown"].get("Chrome") == 1
    assert stats["browser_breakdown"].get("Bot") == 1
    assert stats["os_breakdown"].get("iOS") == 1
    assert stats["os_breakdown"].get("Windows") == 1
    assert stats["os_breakdown"].get("Other") == 1
    assert stats["top_referrers"].get("google.com") == 1
    assert stats["top_referrers"].get("news.ycombinator.com") == 1
    assert stats["top_referrers"].get("Direct") == 1

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert stats["clicks_by_date"].get(today_str) == 3
    assert stats["last_clicked_at"] is not None


def test_analytics_for_unclicked_url():
    """Test GET /api/v1/urls/{code}/stats for an unclicked link returns zero counts and empty dicts."""
    client.post("/api/v1/urls", json={"long_url": "https://zero-clicks.com", "custom_code": "zero-code"})
    stats = client.get("/api/v1/urls/zero-code/stats").json()
    assert stats["click_count"] == 0
    assert stats["unique_visitors"] == 0
    assert stats["device_breakdown"] == {}
    assert stats["browser_breakdown"] == {}
    assert stats["os_breakdown"] == {}
    assert stats["top_referrers"] == {}
    assert stats["clicks_by_date"] == {}
    assert stats["last_clicked_at"] is None


# QR Code Generation

def test_qr_code_svg_format():
    """Test GET /api/v1/urls/{code}/qr?format=svg returns 200, image/svg+xml, and valid SVG markup."""
    client.post("/api/v1/urls", json={"long_url": "https://qr-test.com", "custom_code": "qr-svg"})
    res = client.get("/api/v1/urls/qr-svg/qr?format=svg&scale=10")
    assert res.status_code == 200
    assert "image/svg+xml" in res.headers["content-type"]
    text = res.text
    assert "<svg" in text
    assert "</svg>" in text
    assert "viewBox=" in text
    assert "<rect" in text


def test_qr_code_png_format():
    """Test GET /api/v1/urls/{code}/qr?format=png returns 200, image/png, and valid PNG magic bytes."""
    client.post("/api/v1/urls", json={"long_url": "https://qr-test.com", "custom_code": "qr-png"})
    res = client.get("/api/v1/urls/qr-png/qr?format=png&scale=8")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content.startswith(b"\x89PNG\r\n\x1a\n")

    img = Image.open(io.BytesIO(res.content))
    assert img.format == "PNG"
    assert img.size[0] > 0 and img.size[1] > 0


def test_qr_code_not_found():
    """Test GET /api/v1/urls/{code}/qr returns 404 for non-existent short code."""
    res = client.get("/api/v1/urls/phantom-code/qr")
    assert res.status_code == 404


def test_qr_code_invalid_format_and_scale():
    """Test GET /api/v1/urls/{code}/qr query parameter validation for format and scale."""
    client.post("/api/v1/urls", json={"long_url": "https://qr-test.com", "custom_code": "qr-limits"})

    # Invalid format returns 422
    assert client.get("/api/v1/urls/qr-limits/qr?format=gif").status_code == 422

    # Scale < 2 returns 422
    assert client.get("/api/v1/urls/qr-limits/qr?scale=1").status_code == 422

    # Scale > 20 returns 422
    assert client.get("/api/v1/urls/qr-limits/qr?scale=25").status_code == 422


# Utility Tests

def test_unit_parse_user_agent_devices_and_browsers():
    """Unit test parse_user_agent classification of bots, devices, browsers, and OS."""
    assert parse_user_agent(None) == {"device_type": "Desktop", "browser": "Other", "os_name": "Other"}
    assert parse_user_agent("") == {"device_type": "Desktop", "browser": "Other", "os_name": "Other"}

    # Bots
    assert parse_user_agent("curl/7.88.1") == {"device_type": "Bot", "browser": "Bot", "os_name": "Other"}
    assert parse_user_agent("python-httpx/0.24.1") == {"device_type": "Bot", "browser": "Bot", "os_name": "Other"}

    # Mobile iPhone Safari
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
    assert parse_user_agent(iphone) == {"device_type": "Mobile", "browser": "Safari", "os_name": "iOS"}

    # Mobile Android Chrome
    android = "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 Chrome/114.0 Mobile Safari/537.36"
    assert parse_user_agent(android) == {"device_type": "Mobile", "browser": "Chrome", "os_name": "Android"}

    # Tablet iPad
    ipad = "Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"
    assert parse_user_agent(ipad) == {"device_type": "Tablet", "browser": "Safari", "os_name": "iOS"}

    # Desktop Windows Edge
    edge = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/114.0.1823.43"
    assert parse_user_agent(edge) == {"device_type": "Desktop", "browser": "Edge", "os_name": "Windows"}

    # Desktop Linux Firefox
    firefox = "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/114.0"
    assert parse_user_agent(firefox) == {"device_type": "Desktop", "browser": "Firefox", "os_name": "Linux"}

    # Desktop Mac Safari
    mac_safari = "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 Safari/605.1.15"
    assert parse_user_agent(mac_safari) == {"device_type": "Desktop", "browser": "Safari", "os_name": "macOS"}


def test_unit_normalize_referrer():
    """Unit test normalize_referrer URL normalization and direct fallback."""
    assert normalize_referrer(None) == "Direct"
    assert normalize_referrer("") == "Direct"
    assert normalize_referrer("Direct") == "Direct"
    assert normalize_referrer("none") == "Direct"
    assert normalize_referrer("unknown") == "Direct"
    assert normalize_referrer("https://www.google.com/search?q=url") == "google.com"
    assert normalize_referrer("http://github.com/fastapi/fastapi") == "github.com"
    assert normalize_referrer("news.ycombinator.com/item") == "news.ycombinator.com"
    assert normalize_referrer("https://sub.blog.example.org") == "sub.blog.example.org"


def test_unit_hash_client_ip():
    """Unit test hash_client_ip returns deterministic 16-character SHA-256 digests."""
    assert hash_client_ip(None) == ""
    assert hash_client_ip("") == ""
    h1 = hash_client_ip("192.168.1.1")
    assert len(h1) == 16
    assert h1 == hash_client_ip("192.168.1.1")
    h2 = hash_client_ip("192.168.1.2")
    assert h1 != h2


def test_unit_is_reserved_short_code():
    """Unit test is_reserved_short_code case-insensitive keyword detection."""
    assert is_reserved_short_code("docs") is True
    assert is_reserved_short_code("API") is True
    assert is_reserved_short_code("HeAlTh") is True
    assert is_reserved_short_code("metrics") is True
    assert is_reserved_short_code("dashboard") is True
    assert is_reserved_short_code("static") is True
    assert is_reserved_short_code("favicon.ico") is True
    assert is_reserved_short_code("my-custom-code") is False
    assert is_reserved_short_code("") is False
    assert is_reserved_short_code(None) is False


def test_unit_is_self_referential_url():
    """Unit test is_self_referential_url detects circular redirect loop vulnerabilities."""
    assert is_self_referential_url("http://testserver/path", "http://testserver") is True
    assert is_self_referential_url("http://testserver:80/path", "http://testserver:80") is True
    assert is_self_referential_url("https://example.com/page", "http://testserver") is False
    assert is_self_referential_url("http://localhost:8000/a", "http://127.0.0.1:8000/b") is True
    assert is_self_referential_url("http://localhost:8000/a", "http://localhost:9000") is False
    assert is_self_referential_url("invalid-url", "http://testserver") is False


def test_unit_is_url_expired():
    """Unit test is_url_expired handles aware and naive datetimes safely."""
    assert is_url_expired(None) is False

    now_aware = datetime.now(timezone.utc)
    assert is_url_expired(now_aware - timedelta(hours=1)) is True
    assert is_url_expired(now_aware + timedelta(hours=1)) is False

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert is_url_expired(now_naive - timedelta(hours=1)) is True
    assert is_url_expired(now_naive + timedelta(hours=1)) is False


def test_unit_generate_qr_svg_and_png():
    """Unit test generate_qr_svg and generate_qr_png matrix rendering."""
    data = "https://linkpulse.dev/unit-test"
    svg = generate_qr_svg(data, scale=6, border=1)
    assert isinstance(svg, str)
    assert "<svg" in svg
    assert "</svg>" in svg
    assert 'shape-rendering="crispEdges"' in svg

    png = generate_qr_png(data, scale=6, border=1)
    assert isinstance(png, bytes)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"

    matrix = generate_qr_matrix(data)
    assert len(matrix) > 0
    assert len(matrix) == len(matrix[0])
    # Corner finder patterns module check
    assert matrix[0][0] == 1
