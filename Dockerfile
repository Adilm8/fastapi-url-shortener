FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Behind a proxy (TRUSTED_PROXY_HOPS > 0), honour X-Forwarded-Proto so short URLs use https;
# the app resolves client IPs itself from the trusted end of X-Forwarded-For. With no proxy,
# forwarded headers are client-controlled, so uvicorn must not apply them at all.
CMD ["sh", "-c", "if [ \"${TRUSTED_PROXY_HOPS:-1}\" -gt 0 ]; then PROXY_FLAGS='--proxy-headers --forwarded-allow-ips=*'; else PROXY_FLAGS='--no-proxy-headers'; fi; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} $PROXY_FLAGS"]
