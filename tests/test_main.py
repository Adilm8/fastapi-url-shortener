from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
import app.main as main_module
from app.main import app
from app.models import URLItem
from app.utils import encode_base62, decode_base62, generate_base62_short_code
from app.redis_client import clear_all_redis_keys

TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
main_module.SessionLocal = TestingSessionLocal

client = TestClient(app)

ADMIN_KEY = "test-admin-key"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


def auth(create_res) -> dict:
    """Authorization headers carrying the manage token returned when a link was created."""
    return {"Authorization": f"Bearer {create_res.json()['manage_token']}"}


@pytest.fixture(autouse=True)
def setup_and_teardown_db(monkeypatch):
    """Create fresh database tables and reset all cache/rate limit states before each test."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", ADMIN_KEY)
    Base.metadata.create_all(bind=engine)
    clear_all_redis_keys()
    yield
    Base.metadata.drop_all(bind=engine)
    clear_all_redis_keys()


def test_root_endpoint():
    """Test root health check and algorithm discovery."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "Base62" in data["algorithm"]
    assert data["docs_url"] == "/docs"


def test_create_short_url_base62():
    """Test shortening a URL with Base62 hash-based code generation."""
    payload = {"long_url": "https://fastapi.tiangolo.com/tutorial/"}
    response = client.post("/api/v1/urls", json=payload)
    
    assert response.status_code == 201
    data = response.json()
    assert "short_code" in data
    assert len(data["short_code"]) == 7
    assert data["long_url"] == "https://fastapi.tiangolo.com/tutorial/"
    assert "http://testserver/" in data["short_url"]
    assert "created_at" in data


def test_duplicate_url_creates_independent_link():
    """
    Shortening the same long URL twice yields two independently owned links, so one
    creator can never modify, pause, or delete another creator's link.
    """
    payload = {"long_url": "https://fastapi.tiangolo.com/tutorial/"}
    res1 = client.post("/api/v1/urls", json=payload)
    res2 = client.post("/api/v1/urls", json=payload)
    assert res1.status_code == res2.status_code == 201
    code1, code2 = res1.json()["short_code"], res2.json()["short_code"]
    assert code1 != code2

    # The second creator's token cannot touch the first link
    assert client.patch(f"/api/v1/urls/{code1}", json={"is_active": False}, headers=auth(res2)).status_code == 403
    assert client.get(f"/{code1}", follow_redirects=False).status_code == 307
    assert client.get(f"/{code2}", follow_redirects=False).status_code == 307


def test_repeated_shortening_never_exhausts_code_generation():
    """Shortening one URL many times keeps producing unique codes (random salts after the first)."""
    codes = set()
    for i in range(15):
        res = client.post(
            "/api/v1/urls",
            json={"long_url": "https://example.com/popular"},
            headers={"X-Forwarded-For": f"198.51.100.{i}"},
        )
        assert res.status_code == 201
        codes.add(res.json()["short_code"])
    assert len(codes) == 15


def test_create_does_not_mutate_existing_link():
    """Re-submitting an existing URL with new limits must not alter (or reactivate) the original."""
    original = client.post("/api/v1/urls", json={"long_url": "https://example.com/owned"})
    code = original.json()["short_code"]
    client.patch(f"/api/v1/urls/{code}", json={"is_active": False}, headers=auth(original))

    client.post("/api/v1/urls", json={"long_url": "https://example.com/owned", "max_clicks": 1})

    stats = client.get(f"/api/v1/urls/{code}/stats").json()
    assert stats["is_active"] is False
    assert stats["max_clicks"] is None


def test_collision_detection_and_resolution():
    """
    Test that when an initial Base62 candidate collides with a DIFFERENT URL,
    the system automatically salts and resolves to a unique code.
    """
    url_a = "https://example.com/site-a"
    url_b = "https://example.com/site-b"

    first_candidate = generate_base62_short_code(url=url_a, salt="", length=7)

    db = TestingSessionLocal()
    dummy = URLItem(short_code=first_candidate, long_url=url_b)
    db.add(dummy)
    db.commit()
    db.close()

    response = client.post("/api/v1/urls", json={"long_url": url_a})
    assert response.status_code == 201
    data = response.json()

    assert data["short_code"] != first_candidate
    assert len(data["short_code"]) == 7


def test_create_short_url_custom_code():
    """Test shortening a URL with a custom alias."""
    payload = {
        "long_url": "https://www.python.org",
        "custom_code": "py-org"
    }
    response = client.post("/api/v1/urls", json=payload)
    
    assert response.status_code == 201
    data = response.json()
    assert data["short_code"] == "py-org"
    assert data["long_url"] == "https://www.python.org/"


def test_create_duplicate_custom_code():
    """Test that creating a short URL with an existing custom code returns 409 Conflict."""
    payload1 = {
        "long_url": "https://www.python.org",
        "custom_code": "duplicate-code"
    }
    res1 = client.post("/api/v1/urls", json=payload1)
    assert res1.status_code == 201

    payload2 = {
        "long_url": "https://www.google.com",
        "custom_code": "duplicate-code"
    }
    res2 = client.post("/api/v1/urls", json=payload2)
    assert res2.status_code == 409
    assert "already in use" in res2.json()["detail"]


def test_invalid_url_validation():
    """Test that invalid URLs are rejected with HTTP 422 Unprocessable Entity."""
    invalid_payload = {"long_url": "not-a-valid-url"}
    response = client.post("/api/v1/urls", json=invalid_payload)
    assert response.status_code == 422


def test_rate_limiting_enforcement():
    """Test that exceeding the rate limit returns HTTP 429 Too Many Requests."""
    limit = settings.RATE_LIMIT_REQUESTS
    headers = {"X-Forwarded-For": "192.168.1.200"}
    for i in range(limit):
        res = client.post(
            "/api/v1/urls",
            json={"long_url": f"https://example.com/rl-{i}"},
            headers=headers
        )
        assert res.status_code == 201

    blocked_res = client.post(
        "/api/v1/urls",
        json={"long_url": "https://example.com/rl-blocked"},
        headers=headers
    )
    assert blocked_res.status_code == 429
    assert "Retry-After" in blocked_res.headers


def test_redirect_and_background_click_analytics():
    """Test redirection (HTTP 307) and background click persistence."""
    create_res = client.post(
        "/api/v1/urls",
        json={"long_url": "https://github.com", "custom_code": "github"}
    )
    assert create_res.status_code == 201

    redirect_res = client.get("/github", follow_redirects=False)
    assert redirect_res.status_code == 307
    assert redirect_res.headers["location"] == "https://github.com/"

    stats_res = client.get("/api/v1/urls/github/stats")
    assert stats_res.status_code == 200
    stats_data = stats_res.json()
    assert stats_data["click_count"] == 1
    assert stats_data["short_code"] == "github"

    client.get("/github", follow_redirects=False)
    stats_res_2 = client.get("/api/v1/urls/github/stats")
    assert stats_res_2.json()["click_count"] == 2


def test_redis_cache_hit_redirect():
    """Test that cache-hit path redirects cleanly."""
    client.post(
        "/api/v1/urls",
        json={"long_url": "https://redis.io", "custom_code": "redis-doc"}
    )

    cached = {"long_url": "https://redis.io/", "expires_at": None, "max_clicks": None}
    with patch("app.main.get_cached_url", return_value=cached):
        res = client.get("/redis-doc", follow_redirects=False)
        assert res.status_code == 307
        assert res.headers["location"] == "https://redis.io/"

    stats_res = client.get("/api/v1/urls/redis-doc/stats")
    assert stats_res.status_code == 200
    assert stats_res.json()["click_count"] == 1


def test_redirect_not_found():
    """Test requesting a non-existent short URL returns 404."""
    response = client.get("/nonexistent123", follow_redirects=False)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_stats_not_found():
    """Test requesting stats for a non-existent code returns 404."""
    response = client.get("/api/v1/urls/nonexistent123/stats")
    assert response.status_code == 404


def test_utils_base62_and_salting():
    """Unit tests for Base62 encoding, decoding, and salt variation."""
    test_numbers = [0, 1, 61, 62, 12345, 987654321]
    for num in test_numbers:
        encoded = encode_base62(num)
        assert isinstance(encoded, str)
        decoded = decode_base62(encoded)
        assert decoded == num

    code_1 = generate_base62_short_code("https://example.com", salt="", length=7)
    code_2 = generate_base62_short_code("https://example.com", salt="", length=7)
    assert code_1 == code_2
    assert len(code_1) == 7

    code_salted = generate_base62_short_code("https://example.com", salt="_salt_1", length=7)
    assert code_salted != code_1
    assert len(code_salted) == 7
