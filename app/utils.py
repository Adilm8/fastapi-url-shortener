import hashlib
import string

BASE62_ALPHABET = string.digits + string.ascii_letters
BASE = len(BASE62_ALPHABET)


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
