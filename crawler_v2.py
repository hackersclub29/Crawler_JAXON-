#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Advanced Async JSON API Crawler — single file (v2 — patched)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fixes applied over v1
─────────────────────
 1. RedisQueue.get()        → BZPOPMIN (blocking pop; eliminates 30-worker busy-poll)
                               Falls back to polled ZPOPMIN for Redis < 5.0
 2. Worker shutdown         → sentinel-based graceful drain; no mid-flight cancel
                               Local mode: N sentinels enqueued after queue.join()
                               Redis mode: safe cancel (join() guarantees idle workers)
 3. normalize_url           → trailing-slash strip REMOVED (REST-safe; /v1/ ≠ /v1)
 4. AsyncFetcher host locks → registry-lock pattern; no defaultdict race on new host
 5. param_mutate            → per-param semantic values instead of blanket "1"
                               empty values (callback/jsonp) skipped entirely
 6. _parse_json             → hard recursion depth cap (20) prevents RecursionError
 7. Playwright on_response  → tasks collected via ensure_future, awaited via gather
                               before ctx.close(); navigation errors no longer drop tasks
 8. Proxy probe             → gated behind verify_proxies config flag
                               configurable probe URL; no unconditional 3rd-party egress
 9. Wayback seeding         → raw netloc pre-filter before normalize_url
                               skips wasted work on out-of-scope CDX subdomains
10. Logging                 → per-URL noise demoted to DEBUG
                               periodic INFO stats summary every 30 s
11. Content limits          → separate size caps for HTML / JS / JSON / generic

Install (core):
    pip install aiohttp aiosqlite beautifulsoup4 lxml

Optional:
    pip install esprima                                    # JS AST
    pip install playwright && playwright install chromium  # SPA crawling
    pip install aioredis                                   # distributed queue

Usage:
    python crawler_v2.py https://target.com
    python crawler_v2.py https://target.com --playwright --wayback --param-discovery
    python crawler_v2.py https://target.com --redis redis://localhost:6379 --concurrency 30
    python crawler_v2.py https://target.com --depth 30 --concurrency 20 -v
    python crawler_v2.py https://target.com --proxies proxies.txt --no-verify-proxies
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import random
import re
import sys
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import (
    urljoin, urlparse, urldefrag,
    urlencode, parse_qsl,
)
from urllib.robotparser import RobotFileParser

import aiohttp
import aiosqlite
from aiohttp import ClientConnectorError, ClientPayloadError, ClientResponseError
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────────────────────
# Optional heavy imports
# ─────────────────────────────────────────────────────────────────────────────

try:
    import esprima as _esprima
    _HAS_ESPRIMA = True
except ImportError:
    _HAS_ESPRIMA = False

try:
    from playwright.async_api import async_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

# ─────────────────────────────────────────────────────────────────────────────
# Constants & defaults
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MAX_DEPTH      = 50
DEFAULT_CONCURRENCY    = 10
DEFAULT_TIMEOUT        = 15
DEFAULT_RETRIES        = 3
DEFAULT_DB_FILE        = "crawler.db"
DEFAULT_OUTPUT_FILE    = "found_json_links.txt"
DEFAULT_QUEUE_MAXSIZE  = 50_000

# FIX 11 — separate per-content-type body size caps
MAX_HTML_BYTES         = 5  * 1024 * 1024   # 5 MB
MAX_JS_BYTES           = 10 * 1024 * 1024   # 10 MB  (bundles are routinely large)
MAX_JSON_BYTES         = 2  * 1024 * 1024   # 2 MB
MAX_GENERIC_BYTES      = 5  * 1024 * 1024   # 5 MB

MIN_DELAY_PER_HOST     = 0.1                # seconds between same-host requests
WAYBACK_CDX_URL        = "https://web.archive.org/cdx/search/cdx"

# FIX 2 — sentinel for graceful worker exit (local queue mode)
_STOP_SENTINEL         = "\x00STOP"

# FIX 8 — default probe URL; replace with a self-hosted endpoint in production
_DEFAULT_PROXY_PROBE   = "https://ifconfig.me/ip"

DEFAULT_USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

COMMON_JSON_PATHS: List[str] = [
    "/config.json", "/manifest.json", "/openapi.json", "/swagger.json",
    "/api.json", "/data.json", "/package.json", "/asset-manifest.json",
    "/.well-known/openid-configuration",
    "/wp-json/", "/wp-json/wp/v2/",
    "/api/v1/", "/api/v2/", "/graphql", "/api/graphql",
    "/api/v1/config.json", "/api/v1/manifest.json", "/api/v2/config.json",
    "/sitemap.json", "/routes.json", "/endpoints.json",
]

COMMON_PARAMS: List[str] = [
    "id", "page", "limit", "offset", "format", "type", "api_key",
    "token", "key", "query", "q", "search", "filter", "sort",
    "order", "lang", "locale", "version", "v", "output", "callback",
    "jsonp", "fields", "include", "expand", "embed", "response_type",
]

# FIX 5 — semantically appropriate default values per parameter name.
# Parameters mapped to "" will be SKIPPED (e.g. callback/jsonp require a
# function name the caller must supply; a blank value is useless).
_PARAM_SEMANTIC_VALUES: Dict[str, str] = {
    "page":          "1",
    "offset":        "0",
    "limit":         "100",
    "per_page":      "100",
    "id":            "1",
    "format":        "json",
    "output":        "json",
    "type":          "json",
    "response_type": "json",
    "version":       "1",
    "v":             "1",
    "lang":          "en",
    "locale":        "en",
    "sort":          "id",
    "order":         "asc",
    "q":             "a",
    "query":         "a",
    "search":        "a",
    "fields":        "*",
    "include":       "all",
    "expand":        "true",
    "embed":         "true",
    "callback":      "",   # skip — needs a real function name
    "jsonp":         "",   # skip — same
    "api_key":       "",   # skip — leaking a placeholder key causes noise
    "token":         "",
    "key":           "",
}

_PARAM_FORMAT_PAIRS = [
    ("format",        "json"),
    ("output",        "json"),
    ("type",          "json"),
    ("response_type", "json"),
    ("_format",       "json"),
]

_HTTP_METHODS  = frozenset({"get","post","put","delete","patch","head","options","request","ajax"})
_FETCH_NAMES   = frozenset({"fetch","axios","got","superagent","ky","request"})

# ─────────────────────────────────────────────────────────────────────────────
# Compiled regexes
# ─────────────────────────────────────────────────────────────────────────────

_URL_RE          = re.compile(r"(?P<url>https?://[^\s'\"<>()\[\]{}\\]+)")
_JSON_PATH_RE    = re.compile(r"/(?:[^/\s'\"]+/)*[^/\s'\"]+?\.json(?:\?[^\s'\"]*)?")
_API_ENDPOINT_RE = re.compile(r"/(?:api|data|v\d+|graphql|rest|json).*$", re.IGNORECASE)
_CHARSET_RE      = re.compile(r"charset=([^\s;]+)", re.IGNORECASE)

_JS_FETCH_RE     = re.compile(
    r"""(?:fetch|axios\.(?:get|post|put|delete|patch|request))\s*\(\s*['"`]([^'"`]+)['"`]""",
    re.IGNORECASE,
)
_JS_XHR_RE       = re.compile(
    r"""\.open\s*\(\s*['"`][A-Z]+['"`]\s*,\s*['"`]([^'"`\s]+)['"`]""",
    re.IGNORECASE,
)
_JS_API_STR_RE   = re.compile(
    r"""['"`](/(?:api|v\d+|data|graphql|rest|json)[^'"`\s]*)['"`]""",
    re.IGNORECASE,
)
_JS_JSON_STR_RE  = re.compile(r"""['"`]([^'"`\s]+\.json(?:\?[^'"`\s]*)?)['"`]""")

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("jsoncrawler")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CrawlerConfig:
    start_url: str
    max_depth:              int            = DEFAULT_MAX_DEPTH
    concurrency:            int            = DEFAULT_CONCURRENCY
    proxies:                List[str]      = field(default_factory=list)
    user_agents:            List[str]      = field(default_factory=lambda: list(DEFAULT_USER_AGENTS))
    db_path:                Path           = field(default_factory=lambda: Path(DEFAULT_DB_FILE))
    output_file:            Path           = field(default_factory=lambda: Path(DEFAULT_OUTPUT_FILE))
    redis_url:              Optional[str]  = None
    queue_maxsize:          int            = DEFAULT_QUEUE_MAXSIZE
    use_playwright:         bool           = False
    playwright_concurrency: int            = 2
    enable_param_discovery: bool           = False
    enable_wayback:         bool           = False
    enable_js_ast:          bool           = True
    js_priority_boost:      float          = 2.0
    param_wordlist:         Optional[Path] = None
    # FIX 8: proxy verification gate
    verify_proxies:         bool           = True
    proxy_probe_url:        str            = _DEFAULT_PROXY_PROBE

# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def normalize_url(raw: str, base: Optional[str] = None) -> Optional[str]:
    """
    Resolve → defrag → sort query → lowercase scheme+host ONLY.
    RFC 3986: path is case-sensitive — do NOT lowercase it.
    FIX 3: trailing slash is no longer stripped. /api/v1/ and /api/v1 are
           distinct on many REST servers; stripping silently drops endpoints.
    """
    try:
        if base:
            raw = urljoin(base, raw)
        raw, _ = urldefrag(raw)
        p = urlparse(raw)
        if p.scheme not in ("http", "https"):
            return None
        query = urlencode(sorted(parse_qsl(p.query))) if p.query else p.query
        path  = p.path or "/"
        return p._replace(
            scheme=p.scheme.lower(), netloc=p.netloc.lower(),
            path=path, query=query, fragment="",
        ).geturl()
    except Exception:
        return None


def is_json_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".json")


def is_js_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith((".js", ".mjs", ".cjs"))


def hash_content(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_content(data: bytes, ct: str) -> str:
    """Decode bytes using Content-Type charset; fall back to UTF-8."""
    m = _CHARSET_RE.search(ct)
    charset = m.group(1) if m else "utf-8"
    try:
        return data.decode(charset)
    except (UnicodeDecodeError, LookupError):
        return data.decode("utf-8", errors="ignore")


def is_json_body(data: bytes) -> bool:
    """Content-based JSON detection — catches APIs with wrong/missing Content-Type."""
    if len(data) < 2:
        return False
    s = data.lstrip()
    if s[:1] not in (b"{", b"["):
        return False
    head = s[:15].lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        return False
    try:
        json.loads(data)
        return True
    except (ValueError, UnicodeDecodeError):
        return False


def extract_urls_regex(text: str, base: str) -> Set[str]:
    """Regex fallback: extract absolute URLs and relative .json paths."""
    found: Set[str] = set()
    for m in _URL_RE.finditer(text):
        u = normalize_url(m.group("url"), base)
        if u:
            found.add(u)
    for m in _JSON_PATH_RE.finditer(text):
        u = normalize_url(m.group(0), base)
        if u:
            found.add(u)
    return found


def _content_limit(mime: str) -> int:
    """FIX 11 — per content-type body size cap."""
    if "javascript" in mime or "ecmascript" in mime:
        return MAX_JS_BYTES
    if "json" in mime:
        return MAX_JSON_BYTES
    if "html" in mime:
        return MAX_HTML_BYTES
    return MAX_GENERIC_BYTES

# ─────────────────────────────────────────────────────────────────────────────
# JS AST analysis
# ─────────────────────────────────────────────────────────────────────────────

def _looks_like_api(s: str) -> bool:
    if not s or len(s) < 2:
        return False
    if s.startswith(("http://", "https://")):
        return True
    if s.startswith("/") and (_API_ENDPOINT_RE.search(s) or s.endswith(".json")):
        return True
    return s.endswith(".json")


def _ast_extract_string(node: dict, base: str) -> Optional[str]:
    if not node:
        return None
    t = node.get("type")
    if t == "Literal":
        v = node.get("value")
        if isinstance(v, str) and _looks_like_api(v):
            return normalize_url(v, base)
    elif t == "TemplateLiteral":
        parts = [
            (q.get("value") or {}).get("cooked") or (q.get("value") or {}).get("raw") or ""
            for q in node.get("quasis", [])
        ]
        reconstructed = "".join(parts)
        if _looks_like_api(reconstructed):
            return normalize_url(reconstructed, base)
    elif t == "BinaryExpression" and node.get("operator") == "+":
        for side in ("left", "right"):
            r = _ast_extract_string(node.get(side, {}), base)
            if r:
                return r
    return None


def _ast_handle_call(node: dict, results: Set[str], base: str):
    callee = node.get("callee", {})
    args   = node.get("arguments", [])
    ct     = callee.get("type")

    if ct == "Identifier" and callee.get("name") in _FETCH_NAMES:
        if args:
            u = _ast_extract_string(args[0], base)
            if u: results.add(u)
    elif ct == "MemberExpression":
        prop = callee.get("property", {}).get("name", "")
        if prop in _HTTP_METHODS and args:
            u = _ast_extract_string(args[0], base)
            if u: results.add(u)
        elif prop == "open" and len(args) >= 2:
            u = _ast_extract_string(args[1], base)
            if u: results.add(u)
    elif ct == "Identifier" and callee.get("name") == "axios":
        if args and args[0].get("type") == "ObjectExpression":
            for prop in args[0].get("properties", []):
                k = prop.get("key", {})
                kname = k.get("name") or k.get("value") or ""
                if kname == "url":
                    u = _ast_extract_string(prop.get("value", {}), base)
                    if u: results.add(u)


def _ast_walk(node: object, results: Set[str], base: str):
    if isinstance(node, dict):
        t = node.get("type")
        if t == "CallExpression":
            _ast_handle_call(node, results, base)
        if t == "Literal":
            v = node.get("value")
            if isinstance(v, str) and _looks_like_api(v):
                u = normalize_url(v, base)
                if u: results.add(u)
        if t == "TemplateLiteral":
            u = _ast_extract_string(node, base)
            if u: results.add(u)
        for v in node.values():
            _ast_walk(v, results, base)
    elif isinstance(node, list):
        for item in node:
            _ast_walk(item, results, base)


def analyze_js(code: str, base: str) -> Set[str]:
    """
    Primary: esprima AST. Fallback: regex when esprima absent or parse fails.
    """
    results: Set[str] = set()
    if _HAS_ESPRIMA:
        for parse_fn in (_esprima.parseScript, _esprima.parseModule):
            try:
                tree = parse_fn(code, tolerant=True)
                _ast_walk(tree.toDict(), results, base)
                return results
            except Exception:
                pass
    for m in _JS_FETCH_RE.finditer(code):
        u = normalize_url(m.group(1), base)
        if u: results.add(u)
    for m in _JS_XHR_RE.finditer(code):
        u = normalize_url(m.group(1), base)
        if u: results.add(u)
    for m in _JS_API_STR_RE.finditer(code):
        u = normalize_url(m.group(1), base)
        if u: results.add(u)
    for m in _JS_JSON_STR_RE.finditer(code):
        u = normalize_url(m.group(1), base)
        if u: results.add(u)
    return results

# ─────────────────────────────────────────────────────────────────────────────
# robots.txt cache
# ─────────────────────────────────────────────────────────────────────────────

class RobotsCache:
    """Per-origin robots.txt cache. One fetch per origin; async-safe per-origin locks."""

    def __init__(self, session_getter: Callable[[], aiohttp.ClientSession]):
        self._cache:  Dict[str, Optional[RobotFileParser]] = {}
        self._get_session = session_getter
        self._origin_locks: Dict[str, asyncio.Lock] = {}
        self._reg_lock = asyncio.Lock()

    async def is_allowed(self, url: str) -> bool:
        p      = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        async with self._reg_lock:
            if origin not in self._origin_locks:
                self._origin_locks[origin] = asyncio.Lock()
            lock = self._origin_locks[origin]
        async with lock:
            if origin not in self._cache:
                await self._fetch(origin)
        rp = self._cache.get(origin)
        return rp is None or rp.can_fetch("*", url)

    async def _fetch(self, origin: str):
        try:
            async with self._get_session().get(
                f"{origin}/robots.txt",
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="ignore")
                    rp   = RobotFileParser()
                    rp.parse(text.splitlines())
                    self._cache[origin] = rp
                    return
        except Exception as e:
            logger.debug(f"robots.txt error for {origin}: {e}")
        self._cache[origin] = None

# ─────────────────────────────────────────────────────────────────────────────
# Async SQLite
# ─────────────────────────────────────────────────────────────────────────────

class AsyncSQLite:
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS visited        (url  TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS json_links     (url  TEXT PRIMARY KEY,
                discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS content_hashes (hash TEXT PRIMARY KEY,
                url TEXT NOT NULL);
        """)
        await self.conn.commit()

    async def close(self):
        if self.conn: await self.conn.close()

    async def load_visited(self) -> Set[str]:
        cur = await self.conn.execute("SELECT url FROM visited")
        return {r[0] for r in await cur.fetchall()}

    async def load_json_links(self) -> Set[str]:
        cur = await self.conn.execute("SELECT url FROM json_links")
        return {r[0] for r in await cur.fetchall()}

    async def load_content_hashes(self) -> Set[str]:
        cur = await self.conn.execute("SELECT hash FROM content_hashes")
        return {r[0] for r in await cur.fetchall()}

    async def batch_insert_visited(self, urls: Set[str]):
        if urls and self.conn:
            await self.conn.executemany(
                "INSERT OR IGNORE INTO visited (url) VALUES (?)", [(u,) for u in urls]
            )
            await self.conn.commit()

    async def batch_insert_json(self, urls: Set[str]):
        if urls and self.conn:
            await self.conn.executemany(
                "INSERT OR IGNORE INTO json_links (url) VALUES (?)", [(u,) for u in urls]
            )
            await self.conn.commit()

    async def batch_insert_hashes(self, pairs: Set[Tuple[str, str]]):
        if pairs and self.conn:
            await self.conn.executemany(
                "INSERT OR IGNORE INTO content_hashes (hash, url) VALUES (?, ?)", list(pairs)
            )
            await self.conn.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Priority queue backends
# ─────────────────────────────────────────────────────────────────────────────

class QueueBackend(ABC):
    @abstractmethod
    async def put(self, url: str, depth: int, priority: float) -> bool: ...
    @abstractmethod
    async def get(self) -> Tuple[str, int]: ...
    @abstractmethod
    async def task_done(self) -> None: ...
    @abstractmethod
    async def join(self) -> None: ...
    @abstractmethod
    async def close(self) -> None: ...


class LocalPriorityQueue(QueueBackend):
    """
    asyncio.PriorityQueue.
    put_nowait + QueueFull guard prevents worker deadlock when saturated.
    FIX 2 — supports _STOP_SENTINEL for graceful per-worker shutdown.
    """
    def __init__(self, maxsize: int):
        self._q: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=maxsize)

    async def put(self, url: str, depth: int, priority: float) -> bool:
        try:
            self._q.put_nowait((priority, url, depth))
            return True
        except asyncio.QueueFull:
            logger.debug(f"Queue full — dropping: {url}")
            return False

    async def get(self) -> Tuple[str, int]:
        _, url, depth = await self._q.get()
        return url, depth

    async def task_done(self) -> None: self._q.task_done()
    async def join(self) -> None:      await self._q.join()
    async def close(self) -> None:     pass


class RedisQueue(QueueBackend):
    """
    Redis sorted-set backed queue for distributed multi-node crawling.

    FIX 1 — BZPOPMIN (blocking pop, Redis 5.0+) replaces the busy-poll loop.
             Automatically falls back to polled ZPOPMIN for older Redis.

    Data keys:
      jsoncrawler:queue    — ZSET   (payload_json → priority)
      jsoncrawler:seen     — SET    (distributed URL dedup)
      jsoncrawler:inflight — STRING (global in-flight counter)
    """
    QUEUE_KEY     = "jsoncrawler:queue"
    SEEN_KEY      = "jsoncrawler:seen"
    INFLIGHT_KEY  = "jsoncrawler:inflight"
    _BPOP_TIMEOUT = 1   # seconds to block per BZPOPMIN call

    def __init__(self, redis_url: str, maxsize: int = 50_000):
        self._url                  = redis_url
        self._maxsize              = maxsize
        self._r                    = None
        self._bzpopmin_available   = True

    async def connect(self):
        try:
            import aioredis
        except ImportError:
            raise ImportError("pip install aioredis  to use Redis queue backend")
        self._r = await aioredis.from_url(self._url, decode_responses=True)
        await self._r.setnx(self.INFLIGHT_KEY, "0")
        # Probe for BZPOPMIN support (Redis >= 5.0)
        try:
            # Use a throwaway key; timeout=0 returns immediately if key absent
            await self._r.execute_command("BZPOPMIN", "__crawlerprobe__", 0)
        except Exception as e:
            msg = str(e).lower()
            if "unknown command" in msg or "wrong number" in msg:
                self._bzpopmin_available = False
                logger.warning("Redis < 5.0 detected — BZPOPMIN unavailable, using polled ZPOPMIN")
        logger.info(f"Redis ready: {self._url} | bzpopmin={self._bzpopmin_available}")

    async def put(self, url: str, depth: int, priority: float) -> bool:
        if not self._r: return False
        if await self._r.zcard(self.QUEUE_KEY) >= self._maxsize:
            return False
        if not await self._r.sadd(self.SEEN_KEY, url):
            return False
        await self._r.zadd(self.QUEUE_KEY, {json.dumps({"url": url, "depth": depth}): priority})
        return True

    async def get(self) -> Tuple[str, int]:
        """
        FIX 1: blocking pop eliminates the original O(workers) busy-poll.
        bzpopmin returns (key, member, score) or None on timeout.
        """
        while True:
            if self._bzpopmin_available:
                try:
                    result = await self._r.bzpopmin(self.QUEUE_KEY, timeout=self._BPOP_TIMEOUT)
                    if result:
                        # aioredis: (key_str, member_str, score_float)
                        _, payload, _ = result
                        data = json.loads(payload)
                        await self._r.incr(self.INFLIGHT_KEY)
                        return data["url"], data["depth"]
                    continue  # timeout — queue empty, re-block
                except Exception as e:
                    logger.warning(f"BZPOPMIN error ({e}), switching to polled mode")
                    self._bzpopmin_available = False

            # Polled fallback for Redis < 5.0
            result = await self._r.zpopmin(self.QUEUE_KEY, 1)
            if result:
                data = json.loads(result[0][0])
                await self._r.incr(self.INFLIGHT_KEY)
                return data["url"], data["depth"]
            await asyncio.sleep(0.25)

    async def task_done(self) -> None:
        if self._r:
            v = await self._r.decr(self.INFLIGHT_KEY)
            if v < 0:
                await self._r.set(self.INFLIGHT_KEY, "0")

    async def join(self) -> None:
        while True:
            size     = await self._r.zcard(self.QUEUE_KEY)
            inflight = int(await self._r.get(self.INFLIGHT_KEY) or 0)
            if size == 0 and inflight == 0:
                break
            await asyncio.sleep(0.5)

    async def close(self) -> None:
        if self._r: await self._r.aclose()

# ─────────────────────────────────────────────────────────────────────────────
# Adaptive host scheduler
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HostStats:
    total:      int   = 0
    json_found: int   = 0
    errors:     int   = 0
    total_time: float = 0.0

    def score(self) -> float:
        if self.total == 0: return 1.0
        yield_rate      = (self.json_found + 0.1) / (self.total + 1)
        error_penalty   = 1.0 + (self.errors / max(self.total, 1)) * 2.0
        latency_penalty = 1.0 + ((self.total_time / max(self.total, 1)) / 5.0)
        return yield_rate / (error_penalty * latency_penalty)


class HostScheduler:
    """
    Assigns queue priority based on per-host historical yield.
    Priority: lower float = higher urgency (min-heap).
      JSON endpoints × 4.0 | JS files × boost | everything else × 1.0
    """
    def __init__(self, js_boost: float = 2.0):
        self.stats: Dict[str, HostStats] = defaultdict(HostStats)
        self._boost = js_boost
        self._lock  = asyncio.Lock()

    async def record(self, host: str, elapsed: float, found_json: bool, error: bool):
        async with self._lock:
            s = self.stats[host]
            s.total      += 1
            s.total_time += elapsed
            if found_json: s.json_found += 1
            if error:      s.errors     += 1

    def priority(self, url: str) -> float:
        host  = urlparse(url).netloc
        score = self.stats[host].score()
        if is_json_url(url):  score *= 4.0
        elif is_js_url(url):  score *= self._boost
        return -score  # negate for min-heap

    def top_hosts(self, n: int = 5) -> list:
        return sorted(
            [(h, round(s.score(), 4)) for h, s in self.stats.items()],
            key=lambda x: x[1], reverse=True,
        )[:n]

# ─────────────────────────────────────────────────────────────────────────────
# HTTP fetcher
# ─────────────────────────────────────────────────────────────────────────────

class AsyncFetcher:
    """
    FIX 4 — host_locks use a dedicated registry lock.
    The original defaultdict(asyncio.Lock) pattern is GIL-safe in CPython
    but fragile: two coroutines hitting a new host simultaneously could
    receive different Lock instances from separate dict insertions in edge
    cases. A registry lock guarantees exactly one Lock per host.

    Concurrency model (deadlock-safe):
      semaphore (outer) → host_lock (inner)
    Reversed order would deadlock: all slots filled by coroutines blocked
    on semaphore while holding host locks, so no coroutine can finish.
    """
    def __init__(self, config: CrawlerConfig):
        self._config             = config
        self.session:            Optional[aiohttp.ClientSession] = None
        self.semaphore           = asyncio.Semaphore(config.concurrency)
        # FIX 4 — registry-lock pattern
        self._host_lock_registry: asyncio.Lock          = asyncio.Lock()
        self._host_locks:         Dict[str, asyncio.Lock] = {}
        self.host_last_req:       Dict[str, float]        = defaultdict(float)
        self.proxy_status:        Dict[str, bool]          = {}

    async def _get_host_lock(self, host: str) -> asyncio.Lock:
        """FIX 4 — atomically create or return existing Lock for host."""
        async with self._host_lock_registry:
            if host not in self._host_locks:
                self._host_locks[host] = asyncio.Lock()
            return self._host_locks[host]

    async def start(self):
        connector    = aiohttp.TCPConnector(
            limit_per_host=self._config.concurrency,
            limit=self._config.concurrency * 4,
        )
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            connector=connector,
        )
        if self._config.proxies:
            if self._config.verify_proxies:
                # FIX 8 — only probe when explicitly requested
                await self._probe_proxies()
            else:
                # Trust all proxies without verification
                for proxy in self._config.proxies:
                    self.proxy_status[proxy] = True
                logger.info(f"Trusting {len(self._config.proxies)} proxies without probing")

    async def close(self):
        if self.session: await self.session.close()

    async def _probe_proxies(self):
        """FIX 8 — probe URL is configurable; default changed from httpbin.org."""
        logger.info(f"Probing {len(self._config.proxies)} proxies via {self._config.proxy_probe_url}…")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            for proxy in self._config.proxies:
                try:
                    async with s.get(
                        self._config.proxy_probe_url,
                        proxy=proxy,
                        timeout=aiohttp.ClientTimeout(total=10),
                        allow_redirects=False,
                    ) as r:
                        self.proxy_status[proxy] = r.status == 200
                except Exception:
                    self.proxy_status[proxy] = False
                if not self.proxy_status[proxy]:
                    logger.warning(f"Dead proxy: {proxy}")
        alive = sum(self.proxy_status.values())
        logger.info(f"Proxies alive: {alive}/{len(self.proxy_status)}")

    def _pick_proxy(self) -> Optional[str]:
        healthy = [p for p, ok in self.proxy_status.items() if ok]
        return random.choice(healthy) if healthy else None

    async def fetch(self, url: str) -> Tuple[bytes, str, str, float]:
        """Returns (body, content_type, final_url, elapsed_seconds)."""
        assert self.session, "call start() first"
        host      = urlparse(url).netloc
        proxy     = self._pick_proxy()
        headers   = {
            "User-Agent":      random.choice(self._config.user_agents),
            "Accept":          "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection":      "keep-alive",
        }
        last_exc: Optional[Exception] = None

        for attempt in range(1, DEFAULT_RETRIES + 1):
            t0 = time.monotonic()
            try:
                # FIX 4 — registry-locked host lock acquisition
                async with self.semaphore:
                    lock = await self._get_host_lock(host)
                    async with lock:
                        since = time.monotonic() - self.host_last_req[host]
                        if MIN_DELAY_PER_HOST - since > 0:
                            await asyncio.sleep(MIN_DELAY_PER_HOST - since)
                        async with self.session.get(
                            url, proxy=proxy, headers=headers, allow_redirects=True
                        ) as resp:
                            self.host_last_req[host] = time.monotonic()
                            elapsed = time.monotonic() - t0
                            st      = resp.status

                            if st == 429:
                                ra   = resp.headers.get("Retry-After")
                                wait = int(ra) if ra and ra.isdigit() else min(60 * attempt, 300)
                                logger.warning(f"429 on {url} — waiting {wait}s (attempt {attempt})")
                                await asyncio.sleep(wait)
                                last_exc = ClientResponseError(
                                    resp.request_info, resp.history, status=st
                                )
                                continue
                            if 500 <= st < 600:
                                wait = 2 ** attempt
                                logger.warning(
                                    f"{st} on {url} — retry in {wait}s (attempt {attempt})"
                                )
                                await asyncio.sleep(wait)
                                last_exc = ClientResponseError(
                                    resp.request_info, resp.history, status=st
                                )
                                continue

                            ct  = resp.headers.get("Content-Type", "").lower()
                            # FIX 11 — per content-type cap
                            cap = _content_limit(ct)
                            body = (await resp.read())[:cap]
                            return body, ct, str(resp.url), elapsed

            except (ClientConnectorError, asyncio.TimeoutError, ClientPayloadError) as e:
                last_exc = e
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                last_exc = e
                await asyncio.sleep(1)

        raise RuntimeError(f"All retries exhausted for {url}") from last_exc

# ─────────────────────────────────────────────────────────────────────────────
# Playwright worker
# ─────────────────────────────────────────────────────────────────────────────

class PlaywrightWorker:
    """
    FIX 7 — on_response handlers were fire-and-forget in v1: Playwright's event
    system calls async handlers without awaiting them, so response bodies could
    be dropped when the context closed.

    Fix: schedule each handler with ensure_future(), collect the tasks, then
    await gather() AFTER page.goto() returns (including on navigation error).
    Any tasks still pending at ctx.close() time are cancelled and awaited.
    """
    def __init__(self, user_agents: List[str], concurrency: int = 2):
        self._uas     = user_agents
        self._sem     = asyncio.Semaphore(concurrency)
        self._browser = None
        self._pw      = None
        self.enabled  = _HAS_PLAYWRIGHT

    async def start(self):
        if not _HAS_PLAYWRIGHT:
            logger.warning("playwright not installed — headless mode disabled")
            self.enabled = False
            return
        self._pw      = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        logger.info("Playwright ready")

    async def close(self):
        if self._browser: await self._browser.close()
        if self._pw:      await self._pw.stop()

    async def extract(self, url: str, base_netloc: str) -> Tuple[Set[str], Set[str]]:
        """Returns (discovered_urls, json_endpoints)."""
        if not self._browser:
            return set(), set()

        json_ep: Set[str]             = set()
        disc:    Set[str]             = set()
        # FIX 7 — task collector
        response_tasks: List[asyncio.Task] = []

        async with self._sem:
            ctx  = await self._browser.new_context(
                user_agent=random.choice(self._uas), ignore_https_errors=True
            )
            page = await ctx.new_page()

            async def _handle_response(response) -> None:
                r_url = response.url
                ct    = (response.headers.get("content-type") or "").lower()
                norm  = normalize_url(r_url)
                if not norm:
                    return
                if is_json_url(norm) or "application/json" in ct:
                    json_ep.add(norm)
                    return
                try:
                    body = await response.body()
                    if is_json_body(body):
                        json_ep.add(norm)
                except Exception:
                    pass

            def on_response(response):
                # FIX 7 — schedule on the running loop; do NOT block the event handler
                t = asyncio.ensure_future(_handle_response(response))
                response_tasks.append(t)

            page.on("response", on_response)

            nav_ok = True
            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
            except Exception as e:
                logger.debug(f"Playwright nav error on {url}: {e}")
                nav_ok = False

            # FIX 7 — always drain response tasks, even after navigation failure
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)

            if nav_ok:
                for selector, attr in [
                    ("a[href]",    "href"),
                    ("link[href]", "href"),
                    ("script[src]","src"),
                ]:
                    try:
                        vals = await page.locator(selector).evaluate_all(
                            f"els => els.map(e => e.{attr})"
                        )
                        for v in vals:
                            n = normalize_url(v, url)
                            if n and urlparse(n).netloc == base_netloc:
                                disc.add(n)
                    except Exception:
                        pass
                try:
                    data_vals = await page.locator(
                        "[data-src],[data-url],[data-href],[data-endpoint],[data-api]"
                    ).evaluate_all(
                        "els => els.map(e => e.dataset.src||e.dataset.url||e.dataset.href"
                        "||e.dataset.endpoint||e.dataset.api||'')"
                    )
                    for v in data_vals:
                        if v:
                            n = normalize_url(v, url)
                            if n:
                                disc.add(n)
                except Exception:
                    pass
                try:
                    content = await page.content()
                    for u in analyze_js(content, url):
                        n = normalize_url(u, url)
                        if n and urlparse(n).netloc == base_netloc:
                            disc.add(n)
                except Exception:
                    pass

            # Cancel any straggler tasks and close context cleanly
            straggler = [t for t in response_tasks if not t.done()]
            if straggler:
                for t in straggler:
                    t.cancel()
                await asyncio.gather(*straggler, return_exceptions=True)

            await ctx.close()

        return disc, json_ep

# ─────────────────────────────────────────────────────────────────────────────
# Recon helpers
# ─────────────────────────────────────────────────────────────────────────────

async def wayback_fetch_urls(
    session: aiohttp.ClientSession, domain: str
) -> Set[str]:
    """Fetch up to 25k archived URLs for *domain* from Wayback Machine CDX."""
    params = {
        "url":      f"*.{domain}", "output": "text", "fl": "original",
        "collapse": "urlkey",      "limit":  "25000", "filter": "statuscode:200",
    }
    urls: Set[str] = set()
    try:
        async with session.get(
            WAYBACK_CDX_URL, params=params, timeout=aiohttp.ClientTimeout(total=90)
        ) as resp:
            if resp.status == 200:
                text = await resp.text(errors="ignore")
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("http"):
                        urls.add(line)
                logger.info(f"Wayback: {len(urls)} archived URLs for {domain}")
    except Exception as e:
        logger.warning(f"Wayback CDX failed: {e}")
    return urls


def param_mutate(url: str, extra_params: Optional[List[str]] = None) -> List[str]:
    """
    FIX 5 — use semantically appropriate values per parameter (not blanket "1").
    Parameters mapped to "" in _PARAM_SEMANTIC_VALUES are skipped entirely.
    Format override pairs applied first (highest yield).
    Cap: 25 variants per URL.
    """
    p        = urlparse(url)
    existing = dict(parse_qsl(p.query))
    variants: List[str] = []

    for k, v in _PARAM_FORMAT_PAIRS:
        if k not in existing:
            q = urlencode({**existing, k: v})
            variants.append(p._replace(query=q).geturl())

    if _API_ENDPOINT_RE.search(p.path) or p.path.endswith(".json"):
        params = list(COMMON_PARAMS) + (extra_params or [])
        for param in params:
            if param in existing:
                continue
            val = _PARAM_SEMANTIC_VALUES.get(param, "1")
            if not val:
                continue  # skip empty/sensitive params (callback, api_key, etc.)
            q = urlencode({**existing, param: val})
            variants.append(p._replace(query=q).geturl())
            if len(variants) >= 25:
                break

    return list(dict.fromkeys(variants))

# ─────────────────────────────────────────────────────────────────────────────
# Main crawler
# ─────────────────────────────────────────────────────────────────────────────

class JSONCrawler:
    """
    FIX 2  — Sentinel-based graceful shutdown (local queue mode).
              After queue.join() guarantees all work is complete, N sentinels
              are enqueued (one per worker). Each worker exits cleanly on receipt
              without cancelling any in-flight fetch.
              Redis mode uses safe task.cancel() because join() already drained.
    FIX 6  — _parse_json recurse() has a 20-level depth cap.
    FIX 9  — Wayback seeding pre-filters non-matching netlocs before normalize_url.
    FIX 10 — Per-URL DEBUG logging; periodic INFO stats summary every 30 s.
    """

    def __init__(self, config: CrawlerConfig):
        self.config      = config
        self.start_url   = normalize_url(config.start_url)
        if not self.start_url:
            raise ValueError(f"Invalid start URL: {config.start_url!r}")
        self.base_netloc = urlparse(self.start_url).netloc

        self.db          = AsyncSQLite(config.db_path)
        self.fetcher     = AsyncFetcher(config)
        self.scheduler   = HostScheduler(config.js_priority_boost)

        self.robots:     Optional[RobotsCache]     = None
        self.playwright: Optional[PlaywrightWorker] = None
        self.queue:      Optional[QueueBackend]     = None
        self._extra_params: Optional[List[str]]     = None

        # In-memory state
        self.visited_set:    Set[str]            = set()
        self.json_set:       Set[str]            = set()
        self.queued_set:     Set[str]            = set()
        self.content_hashes: Set[str]            = set()

        # Flush deltas
        self._flush_visited: Set[str]            = set()
        self._flush_json:    Set[str]            = set()
        self._flush_hashes:  Set[Tuple[str,str]] = set()

        self._flush_task:  Optional[asyncio.Task] = None
        self._stats_task:  Optional[asyncio.Task] = None  # FIX 10
        self._processed:   int                    = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def init(self):
        await self.db.connect()
        self.visited_set    = await self.db.load_visited()
        self.json_set       = await self.db.load_json_links()
        self.content_hashes = await self.db.load_content_hashes()

        await self.fetcher.start()
        self.robots = RobotsCache(lambda: self.fetcher.session)

        if self.config.redis_url:
            q = RedisQueue(self.config.redis_url, self.config.queue_maxsize)
            await q.connect()
            self.queue = q
        else:
            self.queue = LocalPriorityQueue(self.config.queue_maxsize)

        if self.config.use_playwright:
            self.playwright = PlaywrightWorker(
                self.config.user_agents, self.config.playwright_concurrency
            )
            await self.playwright.start()

        if self.config.enable_param_discovery and self.config.param_wordlist:
            self._extra_params = [
                line.strip()
                for line in self.config.param_wordlist.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self._flush_task = asyncio.create_task(self._periodic_flush())
        self._stats_task = asyncio.create_task(self._periodic_stats())
        logger.info(
            f"Ready — {self.start_url} | concurrency={self.config.concurrency} | "
            f"queue={'redis' if self.config.redis_url else 'local'} | "
            f"playwright={self.config.use_playwright} | wayback={self.config.enable_wayback}"
        )

    async def close(self):
        for task in (self._flush_task, self._stats_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await self._flush_now()
        await self.db.close()
        await self.fetcher.close()
        if self.playwright: await self.playwright.close()
        if self.queue:      await self.queue.close()

    # ── DB flush ──────────────────────────────────────────────────────────────

    async def _periodic_flush(self, interval: int = 10):
        try:
            while True:
                await asyncio.sleep(interval)
                await self._flush_now()
        except asyncio.CancelledError:
            raise

    async def _flush_now(self):
        v = set(self._flush_visited); self._flush_visited.clear()
        j = set(self._flush_json);   self._flush_json.clear()
        h = set(self._flush_hashes); self._flush_hashes.clear()
        if v: await self.db.batch_insert_visited(v)
        if j: await self.db.batch_insert_json(j)
        if h: await self.db.batch_insert_hashes(h)

    # FIX 10 — periodic stats summary replaces per-URL INFO noise
    async def _periodic_stats(self, interval: int = 30):
        try:
            while True:
                await asyncio.sleep(interval)
                logger.info(
                    f"Stats — processed={self._processed} "
                    f"visited={len(self.visited_set)} "
                    f"json={len(self.json_set)} "
                    f"queued={len(self.queued_set)} "
                    f"top_hosts={self.scheduler.top_hosts(3)}"
                )
        except asyncio.CancelledError:
            raise

    # ── Enqueue ───────────────────────────────────────────────────────────────

    async def enqueue(self, url: str, depth: int):
        if depth > self.config.max_depth:
            return
        norm = normalize_url(url, self.start_url)
        if not norm:
            return
        if urlparse(norm).netloc != self.base_netloc:
            return
        if norm in self.visited_set:
            return
        if not self.config.redis_url and norm in self.queued_set:
            return
        if self.robots and not await self.robots.is_allowed(norm):
            logger.debug(f"robots.txt disallows: {norm}")
            return
        priority = self.scheduler.priority(norm)
        ok = await self.queue.put(norm, depth, priority)
        if ok and not self.config.redis_url:
            self.queued_set.add(norm)

    # ── Run loop ──────────────────────────────────────────────────────────────

    async def run(self):
        await self.enqueue(self.start_url, 0)
        await self._seed_brute_force()
        await self._seed_sitemaps()
        if self.config.enable_wayback:
            await self._seed_wayback()

        workers = [
            asyncio.create_task(self._worker(i))
            for i in range(self.config.concurrency)
        ]

        # queue.join() blocks until every enqueued item has been task_done()'d.
        # When it returns, all workers are idle and blocked on queue.get().
        await self.queue.join()

        if self.config.redis_url:
            # FIX 2 (Redis mode) — workers are safely blocked on BZPOPMIN;
            # cancelling them here is race-free because join() guarantees no
            # item is in flight.
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        else:
            # FIX 2 (local mode) — enqueue one sentinel per worker so each
            # exits cleanly from the top of its loop. No CancelledError raised
            # inside _process(); no partial state corruption possible.
            for _ in range(self.config.concurrency):
                await self.queue.put(_STOP_SENTINEL, -1, float("inf"))
            await asyncio.gather(*workers, return_exceptions=True)

        await self._flush_now()
        all_jsons = sorted(self.json_set)
        self.config.output_file.parent.mkdir(parents=True, exist_ok=True)
        with self.config.output_file.open("w", encoding="utf-8") as f:
            for u in all_jsons:
                f.write(u + "\n")
        logger.info(f"Done — {len(all_jsons)} JSON endpoints → {self.config.output_file}")
        logger.info(f"Top hosts by yield: {self.scheduler.top_hosts()}")

    async def _worker(self, idx: int):
        """FIX 2 — exits on sentinel instead of via CancelledError."""
        while True:
            try:
                url, depth = await self.queue.get()
            except asyncio.CancelledError:
                # Redis mode or unexpected cancellation — exit cleanly
                break
            # Sentinel check — task_done() then exit
            if url == _STOP_SENTINEL:
                await self.queue.task_done()
                logger.debug(f"worker-{idx} stop sentinel received — exiting")
                break
            try:
                self.queued_set.discard(url)
                await self._process(url, depth)
            except Exception as e:
                logger.exception(f"worker-{idx} error on {url}: {e}")
            finally:
                await self.queue.task_done()

    # ── Process ───────────────────────────────────────────────────────────────

    async def _process(self, url: str, depth: int):
        if url in self.visited_set:
            return
        self.visited_set.add(url)
        self._flush_visited.add(url)
        # FIX 10 — demoted to DEBUG; periodic stats carry the INFO signal
        logger.debug(f"[{depth}] {url}")
        self._processed += 1

        host       = urlparse(url).netloc
        found_json = False

        try:
            body, mime, final_url, elapsed = await self.fetcher.fetch(url)
        except Exception as e:
            logger.debug(f"Fetch failed {url}: {e}")
            await self.scheduler.record(host, 0.0, False, True)
            return

        final_norm = normalize_url(final_url, self.start_url)
        if not final_norm:
            await self.scheduler.record(host, elapsed, False, False)
            return

        # Redirect — re-enqueue target if same-origin, skip otherwise
        if final_norm != url:
            if (
                urlparse(final_norm).netloc == self.base_netloc
                and final_norm not in self.visited_set
            ):
                await self.enqueue(final_norm, depth)
            await self.scheduler.record(host, elapsed, False, False)
            return

        # Content-hash dedup
        chash = hash_content(body)
        if chash in self.content_hashes:
            logger.debug(f"Duplicate body — skipping: {final_norm}")
            await self.scheduler.record(host, elapsed, False, False)
            return
        self.content_hashes.add(chash)
        self._flush_hashes.add((chash, final_norm))

        # JSON detection: URL extension + Content-Type + content sniff
        is_json = (
            is_json_url(final_norm)
            or "application/json" in mime
            or is_json_body(body)
        )
        if is_json and final_norm not in self.json_set:
            self.json_set.add(final_norm)
            self._flush_json.add(final_norm)
            found_json = True
            logger.info(f"[JSON] {final_norm}")
            print(final_norm, flush=True)

        # Dispatch to appropriate parser
        if "text/html" in mime:
            await self._parse_html(body, mime, final_norm, depth)
        elif is_json or "application/json" in mime:
            await self._parse_json(body, final_norm, depth)
        elif is_js_url(final_norm) or "javascript" in mime:
            await self._parse_js(body, mime, final_norm, depth)
        else:
            await self._parse_generic(body, mime, final_norm, depth)

        # Parameter discovery
        if self.config.enable_param_discovery and (
            is_json_url(final_norm) or "api" in final_norm.lower()
        ):
            for variant in param_mutate(final_norm, self._extra_params):
                await self.enqueue(variant, depth + 1)

        # Playwright SPA discovery
        if self.playwright and self.playwright.enabled and "text/html" in mime:
            pw_urls, pw_json = await self.playwright.extract(final_norm, self.base_netloc)
            for u in pw_urls:
                await self.enqueue(u, depth + 1)
            for j in pw_json:
                if j not in self.json_set:
                    self.json_set.add(j)
                    self._flush_json.add(j)
                    found_json = True
                    logger.info(f"[JSON/PW] {j}")
                    print(j, flush=True)

        await self.scheduler.record(host, elapsed, found_json, False)

    # ── Parsers ───────────────────────────────────────────────────────────────

    async def _parse_html(self, body: bytes, mime: str, base_url: str, depth: int):
        text = decode_content(body, mime)
        soup = BeautifulSoup(text, "lxml")
        for tag, attr in [
            ("a",      "href"),
            ("link",   "href"),
            ("img",    "src"),
            ("script", "src"),
            ("iframe", "src"),
            ("form",   "action"),
        ]:
            for el in soup.find_all(tag, **{attr: True}):
                await self.enqueue(urljoin(base_url, el[attr]), depth + 1)
        for script in soup.find_all("script"):
            if script.has_attr("src"):
                continue
            code = script.string or ""
            if not code.strip():
                continue
            if self.config.enable_js_ast:
                for u in analyze_js(code, base_url):
                    await self.enqueue(u, depth + 1)
            else:
                for m in _JSON_PATH_RE.finditer(code):
                    u = normalize_url(m.group(0), base_url)
                    if u:
                        await self.enqueue(u, depth + 1)

    async def _parse_js(self, body: bytes, mime: str, base_url: str, depth: int):
        code = decode_content(body, mime)
        if self.config.enable_js_ast:
            for u in analyze_js(code, base_url):
                await self.enqueue(u, depth + 1)
        else:
            for u in extract_urls_regex(code, base_url):
                await self.enqueue(u, depth + 1)

    async def _parse_json(self, body: bytes, base_url: str, depth: int):
        """
        FIX 6 — hard depth cap on the recursive walk.
        GraphQL introspection and deeply-nested API responses can easily exceed
        Python's default recursion limit (~1000); cap at 20 JSON nesting levels.
        """
        try:
            data = json.loads(body)
        except Exception:
            return

        async def recurse(obj, rdepth: int = 0) -> None:
            if rdepth > 20:
                return
            if isinstance(obj, dict):
                for v in obj.values():
                    await recurse(v, rdepth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    await recurse(item, rdepth + 1)
            elif isinstance(obj, str):
                if obj.startswith(("http://", "https://")) or (
                    obj.startswith("/") and len(obj) > 1
                ):
                    u = normalize_url(obj, base_url)
                    if u:
                        await self.enqueue(u, depth + 1)

        await recurse(data)

    async def _parse_generic(self, body: bytes, mime: str, base_url: str, depth: int):
        for u in extract_urls_regex(decode_content(body, mime), base_url):
            await self.enqueue(u, depth + 1)

    # ── Seeding ───────────────────────────────────────────────────────────────

    async def _seed_brute_force(self):
        for path in COMMON_JSON_PATHS:
            full = normalize_url(path, self.start_url)
            if full:
                await self.enqueue(full, 1)

    async def _seed_sitemaps(self):
        for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap.txt"):
            url = normalize_url(path, self.start_url)
            if not url:
                continue
            try:
                body, mime, _, _ = await self.fetcher.fetch(url)
            except Exception:
                continue
            if not body or ("xml" not in mime and "text" not in mime):
                continue
            try:
                soup = BeautifulSoup(body, "lxml-xml")
                for loc in soup.find_all("loc"):
                    full = normalize_url(loc.text.strip(), self.start_url)
                    if full:
                        await self.enqueue(full, 0)
            except Exception as e:
                logger.debug(f"sitemap error {url}: {e}")

    async def _seed_wayback(self):
        """
        FIX 9 — pre-filter by raw netloc before the more expensive normalize_url.
        The CDX wildcard query (*.domain) returns subdomains which the enqueue()
        scope check would drop anyway; filtering early avoids wasted parse work.
        """
        logger.info(f"Querying Wayback Machine for {self.base_netloc}…")
        archived = await wayback_fetch_urls(self.fetcher.session, self.base_netloc)
        seeded  = 0
        skipped = 0
        for url in archived:
            try:
                raw_netloc = urlparse(url).netloc
            except Exception:
                continue
            # FIX 9 — fast pre-filter: skip subdomains and foreign hosts
            if raw_netloc != self.base_netloc:
                skipped += 1
                continue
            norm = normalize_url(url, self.start_url)
            if norm:
                await self.enqueue(norm, 0)
                seeded += 1
        logger.info(f"Wayback seeded={seeded} skipped_out_of_scope={skipped}")

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawler",
        description="Async JSON API crawler — discover endpoints, APIs, and .json files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("start_url")
    p.add_argument("--depth",       type=int,  default=DEFAULT_MAX_DEPTH)
    p.add_argument("--concurrency", type=int,  default=DEFAULT_CONCURRENCY)
    p.add_argument("--db",          type=Path, default=Path(DEFAULT_DB_FILE))
    p.add_argument("--output",      type=Path, default=Path(DEFAULT_OUTPUT_FILE))
    p.add_argument("--proxies",     type=Path, metavar="FILE",
                   help="Proxy list (one http://ip:port per line)")
    p.add_argument("--user-agents", type=Path, metavar="FILE",
                   help="User-Agent list (one per line)")
    p.add_argument("--redis",       type=str,  metavar="URL",
                   help="Redis URL for distributed mode")
    p.add_argument("--playwright",  action="store_true",
                   help="Enable headless Chromium for SPA crawling")
    p.add_argument("--playwright-concurrency", type=int, default=2)
    p.add_argument("--wayback",     action="store_true",
                   help="Seed from Wayback Machine CDX (~25k URLs)")
    p.add_argument("--param-discovery", action="store_true",
                   help="Fuzz API endpoints with common query params")
    p.add_argument("--param-wordlist",  type=Path, metavar="FILE")
    p.add_argument("--no-js-ast",   action="store_true",
                   help="Use regex-only JS extraction (disables esprima AST)")
    p.add_argument("--js-boost",    type=float, default=2.0,
                   help="Priority boost multiplier for .js files")
    # FIX 8 — proxy probe controls
    p.add_argument(
        "--no-verify-proxies", action="store_true",
        help="Skip proxy health-check — avoids unconditional egress to probe URL",
    )
    p.add_argument(
        "--proxy-probe-url", type=str, default=_DEFAULT_PROXY_PROBE,
        help="URL for proxy liveness verification (use a self-hosted endpoint in production)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _read_lines(path: Optional[Path]) -> Optional[List[str]]:
    if not path:
        return None
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


async def main():
    args = build_parser().parse_args()
    if args.verbose:
        logging.getLogger("jsoncrawler").setLevel(logging.DEBUG)

    config = CrawlerConfig(
        start_url              = args.start_url,
        max_depth              = args.depth,
        concurrency            = args.concurrency,
        proxies                = _read_lines(args.proxies) or [],
        user_agents            = _read_lines(getattr(args, "user_agents", None))
                                 or list(DEFAULT_USER_AGENTS),
        db_path                = args.db,
        output_file            = args.output,
        redis_url              = args.redis,
        use_playwright         = args.playwright,
        playwright_concurrency = args.playwright_concurrency,
        enable_wayback         = args.wayback,
        enable_param_discovery = args.param_discovery,
        param_wordlist         = args.param_wordlist,
        enable_js_ast          = not args.no_js_ast,
        js_priority_boost      = args.js_boost,
        verify_proxies         = not args.no_verify_proxies,
        proxy_probe_url        = args.proxy_probe_url,
    )

    crawler = JSONCrawler(config)
    try:
        await crawler.init()
        await crawler.run()
    finally:
        await crawler.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(0)
