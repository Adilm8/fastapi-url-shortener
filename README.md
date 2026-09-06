# URL Shortener

A simple URL shortener pet project built with **Python** and **FastAPI**. It turns long links into short links and tracks basic click statistics.

## Features

- **Shorten URLs**: Converts long web links into short 7-character codes.
- **Custom Links**: Allows creating custom short names (e.g., `/my-link`).
- **Instant Redirect**: Automatically redirects visitors to the original long URL.
- **Click Analytics**: Tracks how many times each shortened link was clicked.
- **Interactive Docs**: Built-in Swagger documentation at `/docs` to test endpoints easily.

## Tech Stack

- **Backend**: Python, FastAPI, Uvicorn
- **Database**: SQLite (default for local dev) / PostgreSQL
- **Cache & Rate Limiter**: Redis (with automatic in-memory fallback)
- **Validation**: Pydantic
- **Testing**: Pytest

## Project Structure

```
├── app/
│   ├── config.py         # App configuration
│   ├── database.py       # Database connection
│   ├── main.py           # API routes and logic
│   ├── models.py         # Database models
│   ├── redis_client.py   # Cache and rate limit helper
│   ├── schemas.py        # Request and response models
│   └── utils.py          # Base62 code generation
├── tests/
│   └── test_main.py      # Automated tests
├── Dockerfile            # Docker configuration
├── docker-compose.yml    # Run with Docker Compose
├── requirements.txt      # Python dependencies
└── README.md             # Project documentation
```

## Getting Started

### Prerequisites

- Python 3.10+
- (Optional) Docker

### Installation & Local Run

1. **Clone the repository and open the folder:**
   ```bash
   cd "URL Shortener"
   ```

2. **Create and activate a virtual environment (optional but recommended):**
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the server:**
   ```bash
   uvicorn app.main:app --reload
   ```

5. **Open in your browser:**
   - Interactive API Docs (Swagger): [http://localhost:8000/docs](http://localhost:8000/docs)
   - Alternative Docs (ReDoc): [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

### Running with Docker (Optional)

If you have Docker installed, you can start the entire stack (FastAPI + PostgreSQL + Redis) with one command:

```bash
docker compose up --build
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/urls` | Create a shortened URL |
| `GET` | `/{short_code}` | Redirect to the original URL |
| `GET` | `/api/v1/urls/{short_code}/stats` | View click count and link details |
| `GET` | `/` | API status and health check |

### Example Request

**Create a Short Link:**
```bash
curl -X POST "http://localhost:8000/api/v1/urls" \
     -H "Content-Type: application/json" \
     -d '{"long_url": "https://github.com"}'
```

**Response:**
```json
{
  "short_code": "dBfZu4w",
  "short_url": "http://localhost:8000/dBfZu4w",
  "long_url": "https://github.com",
  "created_at": "2026-09-06T19:00:00Z"
}
```

---

## Running Tests

Run the test suite using `pytest`:

```bash
python -m pytest
```
