import time
from fastapi import Request

_rate_calls: dict = {}


def allow_rate(key: str, max_calls: int, window: int) -> bool:
    now = time.time()
    arr = [t for t in _rate_calls.get(key, ()) if now - t < window]
    if len(arr) >= max_calls:
        _rate_calls[key] = arr
        return False
    arr.append(now)
    _rate_calls[key] = arr
    return True


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
