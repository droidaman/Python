"""Polite, cached JSON fetching over HTTPS using only the standard library."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, Protocol

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, detail: str = ""):
        super().__init__(f"HTTP {status} from {url}" + (f": {detail}" if detail else ""))
        self.status = status
        self.url = url


class Transport(Protocol):
    def get(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]: ...


class UrllibTransport:
    """Honours HTTPS_PROXY and the system / SSL_CERT_FILE trust store."""

    def get(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


class RateLimiter:
    """At most ``rate`` requests per second, spaced evenly."""

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if not rate > 0:
            raise ValueError("rate must be positive")
        self.interval = 1.0 / rate
        self._clock, self._sleep = clock, sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


class FileCache:
    """Response bodies on disk, keyed by URL, with their fetch time as the file mtime."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def get(self, key: str, max_age: float, now: float) -> bytes | None:
        path = self._path(key)
        if not path.exists() or now - path.stat().st_mtime > max_age:
            return None
        return path.read_bytes()

    def put(self, key: str, body: bytes, now: float) -> None:
        path = self._path(key)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(body)
        os.replace(temporary, path)
        os.utime(path, (now, now))


class JsonClient:
    def __init__(
        self,
        headers: Mapping[str, str],
        limiter: RateLimiter,
        cache: FileCache | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 4,
        timeout: float = 30.0,
    ):
        self.headers = dict(headers)
        self.limiter = limiter
        self.cache = cache
        self.transport = transport or UrllibTransport()
        self._clock, self._sleep = clock, sleep
        self.retries, self.timeout = retries, timeout

    def get_json(self, url: str, max_age: float = 0.0):
        """Parsed JSON, or None for 404. Retries 429/5xx and network errors with backoff."""
        if self.cache is not None and max_age > 0:
            cached = self.cache.get(url, max_age, self._clock())
            if cached is not None:
                return json.loads(cached)
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            try:
                status, body = self.transport.get(url, self.headers, self.timeout)
            except OSError as exc:
                if attempt == self.retries:
                    raise HttpError(0, url, str(exc)) from exc
                self._sleep(2 ** attempt)
                continue
            if status == 200:
                data = json.loads(body)
                if self.cache is not None:
                    self.cache.put(url, body, self._clock())
                return data
            if status == 404:
                return None
            if status in RETRYABLE_STATUS and attempt < self.retries:
                self._sleep(2 ** attempt)
                continue
            raise HttpError(status, url)
        raise HttpError(0, url, "retries exhausted")
