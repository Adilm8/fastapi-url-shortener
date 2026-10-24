import hashlib
import secrets
import string
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from app.config import settings

BASE62_ALPHABET = string.digits + string.ascii_letters
BASE = len(BASE62_ALPHABET)


def utcnow() -> datetime:
    """Current UTC time as a naive datetime, matching how DateTime columns are stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Convert an aware datetime to naive UTC; naive values are assumed to already be UTC."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def get_client_ip(forwarded_for: Optional[str], peer_host: Optional[str]) -> Optional[str]:
    """
    Resolve the real client IP. Each trusted proxy appends the address it received the
    request from to X-Forwarded-For, so the entry TRUSTED_PROXY_HOPS from the right is the
    first one a client cannot forge. Entries further left are client-controlled.
    """
    hops = settings.TRUSTED_PROXY_HOPS
    if hops > 0 and forwarded_for:
        entries = [e.strip() for e in forwarded_for.split(",") if e.strip()]
        if entries:
            return entries[-hops] if len(entries) >= hops else entries[0]
    return peer_host


def generate_manage_token() -> str:
    """Return a new random secret that authorizes managing a single short URL."""
    return secrets.token_urlsafe(32)


def hash_manage_token(token: str) -> str:
    """Hash a management token for storage; tokens are high-entropy so no salt is needed."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards so user search input matches literally (escape char: backslash)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def encode_base62(num: int) -> str:
    """Convert a non-negative integer to a Base62 string."""
    if num == 0:
        return BASE62_ALPHABET[0]
    
    digits = []
    while num > 0:
        digits.append(BASE62_ALPHABET[num % BASE])
        num //= BASE
    
    return "".join(reversed(digits))


def decode_base62(code: str) -> int:
    """Convert a Base62 string back to an integer."""
    num = 0
    for char in code:
        num = num * BASE + BASE62_ALPHABET.index(char)
    return num


def generate_base62_short_code(url: str, salt: str = "", length: int = 7) -> str:
    """
    Generate a Base62 short code from a URL using cryptographic SHA-256 hashing.
    - If a collision occurs with another URL, passing a unique `salt` (e.g. counter)
      produces a completely new Base62 hash candidate.
    - 7 characters in Base62 yield 62^7 = ~3.52 trillion possible combinations.
    """
    raw_input = f"{url}{salt}"
    hash_bytes = hashlib.sha256(raw_input.encode("utf-8")).digest()
    
    hash_int = int.from_bytes(hash_bytes[:8], byteorder="big")
    
    base62_str = encode_base62(hash_int)
    
    if len(base62_str) < length:
        base62_str = base62_str.rjust(length, BASE62_ALPHABET[0])
    
    return base62_str[:length]


def is_reserved_short_code(code: str) -> bool:
    """Check if a short code is reserved by the system (case-insensitive)."""
    if not code:
        return False
    return code.strip().lower() in {s.lower() for s in settings.RESERVED_SHORT_CODES}


def is_self_referential_url(long_url: str, base_url: str) -> bool:
    """
    Return True if the target URL points to the same hostname/domain as this service,
    preventing infinite redirect loops or circular shortening.
    """
    try:
        target_parsed = urlparse(str(long_url).strip())
        base_parsed = urlparse(str(base_url).strip())

        target_netloc = target_parsed.netloc.lower()
        base_netloc = base_parsed.netloc.lower()

        if not target_netloc or not base_netloc:
            return False

        if target_netloc == base_netloc:
            return True

        target_host = target_parsed.hostname.lower() if target_parsed.hostname else ""
        base_host = base_parsed.hostname.lower() if base_parsed.hostname else ""

        # Normalize loopback addresses
        loopbacks = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
        is_target_loopback = target_host in loopbacks
        is_base_loopback = base_host in loopbacks

        target_port = target_parsed.port or (443 if target_parsed.scheme == "https" else 80)
        base_port = base_parsed.port or (443 if base_parsed.scheme == "https" else 80)

        if (target_host == base_host or (is_target_loopback and is_base_loopback)) and target_port == base_port:
            return True

        return False
    except Exception:
        return False


def hash_client_ip(ip: Optional[str]) -> str:
    """Return a 16-character SHA-256 hex digest of salted IP address for privacy-safe tracking."""
    if not ip or not ip.strip():
        return ""
    salted = f"{settings.IP_HASH_SALT}:{ip.strip()}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()[:16]


def parse_user_agent(user_agent: Optional[str]) -> dict[str, str]:
    """
    Parse a User-Agent header string and classify device_type, browser, and os_name.
    Recognizes Bots, Mobile, Tablet, Desktop, common browsers, and operating systems.
    """
    if not user_agent or not user_agent.strip():
        return {
            "device_type": "Desktop",
            "browser": "Other",
            "os_name": "Other"
        }

    ua_lower = user_agent.lower()

    bot_signatures = (
        "bot", "crawler", "spider", "curl", "python-httpx", "testclient",
        "wget", "python-requests", "aiohttp", "scrapy", "slurp", "baiduspider", "postman"
    )
    if any(sig in ua_lower for sig in bot_signatures):
        return {
            "device_type": "Bot",
            "browser": "Bot",
            "os_name": "Other"
        }

    if "ipad" in ua_lower or "tablet" in ua_lower or ("android" in ua_lower and "mobile" not in ua_lower):
        device_type = "Tablet"
    elif "iphone" in ua_lower or "ipod" in ua_lower or "windows phone" in ua_lower or "mobile" in ua_lower:
        device_type = "Mobile"
    else:
        device_type = "Desktop"

    if "edg" in ua_lower or "edge" in ua_lower:
        browser = "Edge"
    elif "chrome" in ua_lower or "crios" in ua_lower:
        browser = "Chrome"
    elif "firefox" in ua_lower or "fxios" in ua_lower:
        browser = "Firefox"
    elif "safari" in ua_lower:
        browser = "Safari"
    else:
        browser = "Other"

    if "windows" in ua_lower:
        os_name = "Windows"
    elif "iphone" in ua_lower or "ipad" in ua_lower or "ipod" in ua_lower or "ios" in ua_lower:
        os_name = "iOS"
    elif "mac os" in ua_lower or "macintosh" in ua_lower:
        os_name = "macOS"
    elif "android" in ua_lower:
        os_name = "Android"
    elif "linux" in ua_lower:
        os_name = "Linux"
    else:
        os_name = "Other"

    return {
        "device_type": device_type,
        "browser": browser,
        "os_name": os_name
    }


def normalize_referrer(referrer: Optional[str]) -> str:
    """
    Extract clean normalized domain/hostname from referrer URL,
    stripping 'www.' prefix, or return 'Direct' if absent or invalid.
    """
    if not referrer:
        return "Direct"

    ref = referrer.strip()
    if not ref or ref.lower() in ("direct", "none", "unknown"):
        return "Direct"

    if "://" not in ref and not ref.startswith("//"):
        ref = f"http://{ref}"

    try:
        parsed = urlparse(ref)
        hostname = parsed.hostname
        if not hostname:
            return "Direct"

        hostname = hostname.lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]

        return hostname if hostname else "Direct"
    except Exception:
        return "Direct"


def is_url_expired(expires_at: Optional[datetime]) -> bool:
    """
    Check whether an expiration datetime has passed.
    Safely handles both naive UTC datetimes and timezone-aware UTC datetimes.
    """
    if expires_at is None:
        return False

    if expires_at.tzinfo is None:
        return datetime.now(timezone.utc).replace(tzinfo=None) > expires_at

    return datetime.now(timezone.utc) > expires_at
