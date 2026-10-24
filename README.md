<div align="center">

# ⚡ LinkPulse: Distributed URL Shortener & Telemetry Platform

An enterprise-grade, high-throughput URL shortening and real-time telemetry service engineered with **FastAPI**, **Redis**, and **PostgreSQL**. Features sub-millisecond cache-aside redirects, write-behind click buffering, privacy-preserving visitor analytics, and a zero-dependency Reed-Solomon QR code generator.

[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io/)
[![Docker](https://img.shields.io/badge/Docker-Compose%20Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![CI Status](https://img.shields.io/badge/CI-Passing-brightgreen?style=for-the-badge&logo=githubactions&logoColor=white)](#automated-testing--ci)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)

[Interactive Dashboard](http://localhost:8000/dashboard) • [API Swagger Docs](http://localhost:8000/docs) • [ReDoc](http://localhost:8000/redoc)

</div>

---

## 🌟 Executive Engineering Highlights

- **Sub-Millisecond Redirect Path (~0.8ms P50)**: Decouples HTTP 307 temporary redirects from relational database writes using Redis cache-aside reads and atomic write-behind buffering (`HINCRBY`).
- **Base62 Slicing with Salted Collision Resolution**: Generates concise 7-character aliases across a 62-character alphabet ($62^7 \approx 3.52$ trillion keys) using truncated SHA-256 digests with deterministic salting reprobing.
- **Granular, Privacy-Preserving Telemetry**: Logs device categories (Desktop, Mobile, Tablet, Bot), browsers, operating systems, and referring domains, while tracking unique visitors via salted SHA-256 IP truncation without storing raw PII.
- **Pure-Python Reed-Solomon QR Code Engine**: Generates ISO/IEC 18004 visual matrices directly in vector SVG XML and raster PNG bytes using Galois Field $GF(2^8)$ arithmetic with zero cloud API dependencies.
- **Graceful Dual-Layer Degradation (L2 -> L1 Fallback)**: Automatically falls back to thread-safe in-memory caching and sliding-window rate tracking if Redis is unreachable, guaranteeing zero-crash operation in local development and CI.
- **Full Link Lifecycle Governance**: Supports custom aliases, automated time-to-live (TTL) expiration (`expires_at` / `expires_in_seconds`), click quotas (`max_clicks`), and soft deactivation/reactivation (`PATCH`).
- **Sliding-Window Rate Limiting**: Protects creation endpoints with Redis-backed per-IP rate limiting, emitting standard `X-RateLimit-*` and `Retry-After` HTTP headers.
- **Self-Contained Dark-Mode Web Dashboard**: Responsive single-page application built with Tailwind CSS at `/dashboard`, supporting 1-click link creation, batch shortening, live analytics drawer, and QR code downloads.

---

## 🏛️ System Architecture

```mermaid
flowchart LR
    subgraph Clients["Clients & Presentation"]
        Browser["End Users / HTTP Clients"]
        Dashboard["Tailwind SPA Dashboard<br/>(/dashboard)"]
    end

    subgraph Edge["FastAPI Application Tier"]
        Middleware["Timing & CORS Middleware<br/>(X-Process-Time-Ms)"]
        RateLimiter["Sliding-Window Rate Limiter<br/>(X-RateLimit-*)"]
        Router["REST Controllers & Redirect Router"]
        BgWorker["Background Write-Behind Worker<br/>(persist_clicks_to_db)"]
        QREngine["Zero-Dependency QR Engine<br/>(Galois GF(2^8) Reed-Solomon)"]
    end

    subgraph CacheTier["Distributed In-Memory Tier"]
        RedisCache[("Redis L2 Cache<br/>Hot Keys & TTL")]
        RedisCounter[("Redis Atomic Buffers<br/>HINCRBY Counters")]
        MemoryFallback[("In-Memory L1 Fallback<br/>Thread-Safe Dicts")]
    end

    subgraph StorageTier["Persistent Relational Tier"]
        PostgresDB[("PostgreSQL 16 / SQLite<br/>urls & click_events")]
    end

    Browser -->|HTTP Requests| Middleware
    Dashboard -->|REST API / SSE| Middleware
    Middleware --> RateLimiter
    RateLimiter -->|Check Quota| RedisCounter
    RateLimiter -.->|Fallback if Offline| MemoryFallback
    RateLimiter --> Router

    Router -->|1. Cache-Aside Read| RedisCache
    RedisCache -.->|Fallback on Miss/Down| PostgresDB
    Router -->|2. Atomic Click Buffer| RedisCounter
    Router -->|3. Generate Matrix| QREngine
    Router -->|4. Non-Blocking 307 Redirect| Browser

    Router -.->|Enqueue Task| BgWorker
    BgWorker -->|Flush Click Delta| RedisCounter
    BgWorker -->|Batch Persistence & Telemetry| PostgresDB
```

---

## 📊 Feature Matrix

| Feature | LinkPulse | Basic Implementations |
| :--- | :---: | :---: |
| **Shortcode Generation** | **Deterministic SHA-256 + Base62 ($62^7$ keys)** | Sequential Auto-Increment ID (Crawling Risk) |
| **Collision Resolution** | **Deterministic Salted Linear Reprobing** | Unhandled / Crash on Unique Violation |
| **Redirect Latency** | **Sub-millisecond (~0.8ms P50 via Redis)** | 20ms - 150ms (Synchronous DB write) |
| **Click Persisting** | **Non-blocking Write-Behind (`BackgroundTasks`)** | Synchronous relational DB `UPDATE` |
| **Visitor Telemetry** | **Device, OS, Browser, Referrer, Unique Visitors** | Basic integer increment |
| **Privacy / PII Compliance** | **Salted SHA-256 IP Truncation (Zero-PII)** | Raw IP logged / Stored |
| **QR Code Engine** | **Zero-Dependency Reed-Solomon ($GF(2^8)$)** | External 3rd-party API (Privacy Leak) |
| **Link Governance** | **TTL Expiration, Click Quotas, Soft Toggling** | Permanent only |
| **Rate Limiting** | **Sliding-Window with `Retry-After` Headers** | None |
| **Resilience Tier** | **Graceful L2 (Redis) -> L1 (In-Memory) Fallback** | Hard crash if Redis offline |
| **Web Dashboard** | **Tailwind CSS Dark-Mode SPA (`/dashboard`)** | None / Generic Swagger only |

---

## 🔌 API Reference & Endpoints

All endpoints are versioned under `/api/v1` (with standard roots for health, redirect, and dashboard).

| Method | Endpoint | Description | Response Model |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | API status, version, and algorithmic metadata | JSON discovery object |
| `GET` | `/health` | System health check (PostgreSQL/SQLite + Redis status) | `HealthResponse` |
| `GET` | `/api/v1/metrics` | Operational metrics (cache hit ratio, total links/clicks) | `MetricsResponse` |
| `POST` | `/api/v1/urls` | Create short URL (Base62 or Custom Alias, TTL, Max Clicks) | `URLResponse` (201) |
| `POST` | `/api/v1/urls/batch` | Batch shorten multiple URLs (up to 20 per request) | `URLBatchResponse` (201) |
| `GET` | `/api/v1/urls` | Paginated link list with search & active-status filters | `URLListResponse` |
| `GET` | `/api/v1/urls/{short_code}/stats` | Granular telemetry, demographic breakdowns, & unique visitors | `URLStatsResponse` |
| `PATCH` | `/api/v1/urls/{short_code}` | Update target URL, active status, expiration, or click cap | `URLResponse` |
| `DELETE` | `/api/v1/urls/{short_code}` | Decommission link, delete click events, and purge cache | `204 No Content` |
| `GET` | `/api/v1/urls/{short_code}/qr` | Generate scannable QR Code matrix (`format=svg` or `png`) | `image/svg+xml` / `image/png` |
| `GET` | `/{short_code}` | Sub-millisecond temporary redirect with async telemetry | `307 Temporary Redirect` |

---

## 💻 CLI & `curl` Examples

### 1. Create a Shortened URL with Custom Alias, Expiration, and Max Clicks
```bash
curl -X POST "http://localhost:8000/api/v1/urls" \
     -H "Content-Type: application/json" \
     -d '{
       "long_url": "https://fastapi.tiangolo.com/tutorial/",
       "custom_code": "fastapi-docs",
       "expires_in_seconds": 86400,
       "max_clicks": 500
     }'
```

**Response (`201 Created`):**
```json
{
  "short_code": "fastapi-docs",
  "short_url": "http://localhost:8000/fastapi-docs",
  "long_url": "https://fastapi.tiangolo.com/tutorial/",
  "created_at": "2026-10-24T21:00:00Z",
  "expires_at": "2026-10-25T21:00:00Z",
  "is_active": true,
  "max_clicks": 500,
  "click_count": 0,
  "qr_code_url": "http://localhost:8000/api/v1/urls/fastapi-docs/qr"
}
```

### 2. Batch Shorten Multiple Links
```bash
curl -X POST "http://localhost:8000/api/v1/urls/batch" \
     -H "Content-Type: application/json" \
     -d '{
       "urls": [
         {"long_url": "https://github.com"},
         {"long_url": "https://redis.io"},
         {"long_url": "https://www.postgresql.org"}
       ]
     }'
```

### 3. Test Low-Latency Redirect & Timing Headers
```bash
curl -I "http://localhost:8000/fastapi-docs"
```

**Headers:**
```http
HTTP/1.1 307 Temporary Redirect
location: https://fastapi.tiangolo.com/tutorial/
x-process-time-ms: 0.78
x-ratelimit-limit: 60
x-ratelimit-remaining: 59
```

### 4. Fetch Rich Visitor Analytics
```bash
curl "http://localhost:8000/api/v1/urls/fastapi-docs/stats"
```

**Response:**
```json
{
  "short_code": "fastapi-docs",
  "short_url": "http://localhost:8000/fastapi-docs",
  "long_url": "https://fastapi.tiangolo.com/tutorial/",
  "click_count": 42,
  "created_at": "2026-10-24T21:00:00Z",
  "is_active": true,
  "is_expired": false,
  "unique_visitors": 29,
  "clicks_by_date": { "2026-10-24": 42 },
  "top_referrers": { "google.com": 18, "Direct": 14, "t.co": 10 },
  "device_breakdown": { "Desktop": 28, "Mobile": 14 },
  "browser_breakdown": { "Chrome": 24, "Safari": 12, "Firefox": 6 },
  "os_breakdown": { "macOS": 20, "Windows": 14, "iOS": 8 }
}
```

### 5. Download Standalone QR Code (Vector SVG or Raster PNG)
```bash
# Save vector SVG directly:
curl "http://localhost:8000/api/v1/urls/fastapi-docs/qr?format=svg" -o qrcode.svg

# Save raster PNG:
curl "http://localhost:8000/api/v1/urls/fastapi-docs/qr?format=png&scale=10" -o qrcode.png
```

---

## 🚀 Quickstart Guide

### Option A: 1-Command Docker Compose (Full Stack)

Run the full distributed stack (**FastAPI + PostgreSQL 16 + Redis 7**) with zero local configuration:

```bash
docker compose up --build
```

- **Web Dashboard**: [http://localhost:8000/dashboard](http://localhost:8000/dashboard)
- **Interactive OpenAPI Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Alternative ReDoc UI**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

### Option B: Local Python Development (Zero Dependencies)

LinkPulse runs out-of-the-box using local SQLite and automatic in-memory caching/rate-limiting if PostgreSQL and Redis are not running:

1. **Clone and enter repository:**
   ```bash
   git clone https://github.com/<your-username>/fastapi-url-shortener.git
   cd fastapi-url-shortener
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv venv
   # Windows:
   .\venv\Scripts\activate
   # macOS/Linux:
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Start the ASGI server:**
   ```bash
   uvicorn app.main:app --reload
   ```

---

## 🧪 Automated Testing & CI

The platform includes full automated test coverage covering Base62 encoding, salted collision resolution, write-behind analytics, cache-aside redirection, and rate limiting.

Run the test suite locally with `pytest`:

```bash
python -m pytest -v
```

### GitHub Actions CI Matrix

Every `push` and `pull_request` to `main` executes an automated GitHub Actions pipeline ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) testing across:
- **Python 3.11**
- **Python 3.12**
- **Python 3.13**

---

## 📄 License

This project is licensed under the terms of the **MIT License**.
