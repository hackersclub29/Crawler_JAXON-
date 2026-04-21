# 🕷️ Advanced Async JSON API Crawler

> **A practitioner-grade, async Python tool for crawling web targets and enumerating JSON API endpoints, `.json` files, and hidden API routes — built for security researchers, penetration testers, and bug bounty hunters.**

---

## ⚠️ DISCLAIMER

> **This tool is intended strictly for authorized security testing, penetration testing engagements, bug bounty programs, CTF competitions, and personal lab environments where explicit written permission has been granted.**
>
> **Unauthorized use of this tool against systems you do not own or have explicit permission to test is illegal under the Computer Fraud and Abuse Act (CFAA), the Computer Misuse Act (CMA), and equivalent laws in most jurisdictions worldwide.**
>
> **The author(s) and contributors assume NO liability and are NOT responsible for any misuse, damage, or legal consequences resulting from the use of this tool.**
>
> **Always obtain written authorization before testing any system. Use responsibly.**

---

## 📄 License

```
Copyright 2026 Contributors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

---

## 📖 Table of Contents

- [What This Tool Does](#-what-this-tool-does)
- [Architecture Overview](#-architecture-overview)
- [Requirements](#-requirements)
- [Installation](#-installation)
  - [Linux / macOS](#linux--macos)
  - [Windows](#windows)
- [Quick Start](#-quick-start)
- [Usage Reference](#-usage-reference)
- [Feature Deep Dive](#-feature-deep-dive)
- [Effective Usage Guide](#-effective-usage-guide)
- [Output Files](#-output-files)
- [Configuration Reference](#-configuration-reference)
- [Troubleshooting](#-troubleshooting)
- [Changelog (v2 Fixes)](#-changelog-v2-fixes)

---

## 🔍 What This Tool Does

This crawler discovers **JSON API endpoints** on a target web application by:

- Crawling HTML pages and extracting all links recursively
- Parsing JavaScript files using both AST analysis (`esprima`) and regex fallback to find `fetch()`, `axios`, `XMLHttpRequest` calls and embedded API path strings
- Brute-forcing common JSON/API paths (`/api/v1/`, `/config.json`, `/swagger.json`, etc.)
- Detecting JSON responses by **content sniffing** — not just `Content-Type` headers (catches misconfigured APIs)
- Seeding from the **Wayback Machine CDX API** (25k+ archived URLs per domain)
- Fuzzing discovered endpoints with **semantic query parameters** (`format=json`, `output=json`, etc.)
- Running **headless Chromium (Playwright)** to discover endpoints in Single-Page Applications (SPAs) where static crawling misses dynamically loaded routes
- Supporting **distributed crawling** via Redis-backed priority queues for large-scope engagements

### Typical Findings

| Finding Type | Example |
|---|---|
| Exposed config | `/config.json`, `/manifest.json` |
| API schemas | `/swagger.json`, `/openapi.json`, `/.well-known/openid-configuration` |
| REST endpoints | `/api/v1/users`, `/api/v2/products?format=json` |
| GraphQL | `/graphql`, `/api/graphql` |
| WordPress REST | `/wp-json/wp/v2/users` |
| Embedded in JS | `fetch("/api/internal/tokens")` inside bundled JS |

---

## 🏗️ Architecture Overview

```
JSONCrawler
├── QueueBackend (LocalPriorityQueue or RedisQueue)
│   └── Priority scoring: JSON URLs > JS files > HTML
├── AsyncFetcher
│   ├── aiohttp session + per-host rate limiting
│   ├── Proxy rotation + health probing
│   └── Retry logic (429 / 5xx with backoff)
├── HostScheduler
│   └── Adaptive priority: rewards hosts with JSON yield
├── Parsers
│   ├── HTML  → BeautifulSoup link extraction
│   ├── JS    → esprima AST + regex fallback
│   ├── JSON  → recursive key/value URL extraction
│   └── Generic → regex URL extraction
├── PlaywrightWorker (optional)
│   └── Headless Chromium + XHR/fetch interception
├── RobotsCache
│   └── Per-origin robots.txt compliance
└── AsyncSQLite
    └── Persistent visited / JSON links / content hash dedup
```

---

## 📦 Requirements

### Python Version

```
Python >= 3.9
```

### Core Dependencies (Required)

| Package | Purpose |
|---|---|
| `aiohttp` | Async HTTP client |
| `aiosqlite` | Async SQLite for persistent state |
| `beautifulsoup4` | HTML parsing |
| `lxml` | Fast HTML/XML parser backend for BeautifulSoup |

### Optional Dependencies

| Package | Purpose | Install When |
|---|---|---|
| `esprima` | JavaScript AST analysis (significantly better JS coverage) | Always recommended |
| `playwright` | Headless Chromium for SPA crawling | Target uses React/Vue/Angular |
| `aioredis` | Redis-backed distributed queue | Multi-node / large-scope engagements |

### System Requirements

- Python 3.9+
- pip
- Internet access (or VPN/proxy to target)
- ~200MB disk for SQLite DB on large crawls
- For Playwright: Chromium browser (auto-installed via `playwright install chromium`)
- For Redis mode: Redis server 5.0+ (locally or remote)

---

## ⚙️ Installation

### Linux / macOS

#### Step 1 — Clone the Repository

```bash
https://github.com/hackersclub29/Crawler_JAXON-.git
cd Crawler_JAXON
```

#### Step 2 — Create and Activate Virtual Environment

```bash
# Create venv
python3 -m venv venv

# Activate venv
source venv/bin/activate

# Confirm you're inside venv (should show venv path)
which python
```

#### Step 3 — Install Core Dependencies

```bash
pip install --upgrade pip
pip install aiohttp aiosqlite beautifulsoup4 lxml
```

#### Step 4 — Install Optional Dependencies

```bash
# JavaScript AST analysis (recommended)
pip install esprima

# SPA / headless browser support
pip install playwright
playwright install chromium

# Distributed Redis queue (only if using --redis)
pip install aioredis
```

#### Step 5 — Verify Installation

```bash
python crawler_v2.py --help
```

---

### Windows

#### Step 1 — Clone the Repository

```powershell
git https://github.com/hackersclub29/Crawler_JAXON-.git
cd Crawler_JAXON
```

#### Step 2 — Create and Activate Virtual Environment

```powershell
# Create venv
python -m venv venv

# Activate venv (PowerShell)
.\venv\Scripts\Activate.ps1

# If PowerShell execution policy blocks this, run first:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Activate venv (Command Prompt)
.\venv\Scripts\activate.bat

# Confirm venv is active (path shows venv)
where python
```

#### Step 3 — Install Core Dependencies

```powershell
pip install --upgrade pip
pip install aiohttp aiosqlite beautifulsoup4 lxml
```

#### Step 4 — Install Optional Dependencies

```powershell
# JavaScript AST analysis (recommended)
pip install esprima

# SPA / headless browser support
pip install playwright
playwright install chromium

# Distributed Redis queue
pip install aioredis
```

#### Step 5 — Verify Installation

```powershell
python crawler_v2.py --help
```

---

### requirements.txt

Save as `requirements.txt` in the project root for reproducible installs:

```
# Core (required)
aiohttp>=3.9.0
aiosqlite>=0.19.0
beautifulsoup4>=4.12.0
lxml>=4.9.0

# Optional — JS AST analysis (strongly recommended)
esprima>=4.0.1

# Optional — SPA headless crawling
playwright>=1.40.0

# Optional — distributed Redis queue
aioredis>=2.0.0
```

Install from file:

```bash
# Core only
pip install -r requirements.txt

# All optional too
pip install -r requirements.txt esprima playwright aioredis
```

---

## 🚀 Quick Start

```bash
# Activate your venv first (Linux/macOS)
source venv/bin/activate

# Activate your venv first (Windows PowerShell)
.\venv\Scripts\Activate.ps1

# Basic crawl — default depth 50, 10 concurrent workers
python crawler_v2.py https://target.example.com

# Full recon crawl — all features enabled
python crawler_v2.py https://target.example.com \
  --playwright \
  --wayback \
  --param-discovery \
  --concurrency 20 \
  --depth 30 \
  -v
```

**Results are printed live to stdout AND saved to `found_json_links.txt`.**

---

## 📋 Usage Reference

```
usage: crawler [-h] [--depth DEPTH] [--concurrency CONCURRENCY]
               [--db DB] [--output OUTPUT] [--proxies FILE]
               [--user-agents FILE] [--redis URL] [--playwright]
               [--playwright-concurrency PLAYWRIGHT_CONCURRENCY]
               [--wayback] [--param-discovery] [--param-wordlist FILE]
               [--no-js-ast] [--js-boost JS_BOOST]
               [--no-verify-proxies] [--proxy-probe-url PROXY_PROBE_URL]
               [-v]
               start_url
```

### All Flags

| Flag | Default | Description |
|---|---|---|
| `start_url` | *(required)* | Target URL to begin crawl from |
| `--depth N` | `50` | Maximum crawl depth from start URL |
| `--concurrency N` | `10` | Number of parallel async workers |
| `--db PATH` | `crawler.db` | SQLite database path for persistent state |
| `--output PATH` | `found_json_links.txt` | Output file for discovered JSON endpoints |
| `--proxies FILE` | none | Proxy list file (one `http://ip:port` per line) |
| `--user-agents FILE` | built-in | User-Agent list (one per line) |
| `--redis URL` | none | Redis URL for distributed mode (`redis://localhost:6379`) |
| `--playwright` | off | Enable headless Chromium for SPA crawling |
| `--playwright-concurrency N` | `2` | Concurrent Playwright browser contexts |
| `--wayback` | off | Seed queue from Wayback Machine CDX (25k URLs) |
| `--param-discovery` | off | Fuzz API endpoints with semantic query params |
| `--param-wordlist FILE` | none | Custom parameter wordlist (one param per line) |
| `--no-js-ast` | off | Use regex-only JS extraction (skip esprima AST) |
| `--js-boost N` | `2.0` | Priority multiplier for `.js` file fetching |
| `--no-verify-proxies` | off | Skip proxy health check (no egress to probe URL) |
| `--proxy-probe-url URL` | `https://ifconfig.me/ip` | URL used for proxy liveness verification |
| `-v / --verbose` | off | Enable DEBUG-level logging |

---

## 🔬 Feature Deep Dive

### 1. Adaptive Priority Queue

URLs are scored by host yield history. Hosts that previously returned JSON endpoints are deprioritized less aggressively — the crawler automatically focuses on productive attack surface.

- JSON URLs (`*.json`) get a **4× priority boost**
- JS files get a **configurable boost** (default `2×`) since they often contain embedded API paths
- 5xx errors and slow hosts are penalized in scoring

### 2. JavaScript AST Analysis

When `esprima` is installed, the crawler parses `.js` files as a full Abstract Syntax Tree and walks every `CallExpression` node looking for:

- `fetch("/api/endpoint")`
- `axios.get("/api/data")`
- `xhr.open("GET", "/api/resource")`
- `axios({ url: "/api/config" })`
- Template literals that resolve to API paths

Falls back to regex extraction when esprima is absent or the file fails to parse (common with minified bundles).

### 3. Content-Based JSON Detection

The crawler does NOT solely rely on `Content-Type: application/json`. It sniffs response bodies:

- Body starts with `{` or `[`
- Is not an HTML document
- Passes `json.loads()` validation

This catches APIs that return JSON with `text/html` or `text/plain` content types — a common misconfiguration.

### 4. Playwright SPA Support

When `--playwright` is enabled:

- A headless Chromium browser loads the page and waits for `networkidle`
- All XHR/fetch network responses are intercepted in real time
- DOM is queried for `data-src`, `data-url`, `data-api`, `data-endpoint` attributes
- Page HTML is analyzed post-render for dynamically injected scripts

This is essential for React/Vue/Angular/Next.js targets where static crawling sees only a shell `index.html`.

### 5. Wayback Machine Seeding

With `--wayback`, the tool queries the Wayback CDX API for up to 25,000 historically archived URLs for the target domain. These are filtered to the same origin and bulk-enqueued. This recovers:

- Deleted or deprecated API endpoints still accessible on the live target
- Hidden routes never linked from the current UI
- Legacy versioned endpoints (`/api/v1/`, `/api/v0/`)

### 6. Parameter Discovery

With `--param-discovery`, discovered API endpoints are mutated with semantically correct query parameter values:

| Parameter | Value Injected |
|---|---|
| `format` | `json` |
| `output` | `json` |
| `page` | `1` |
| `limit` | `100` |
| `id` | `1` |
| `fields` | `*` |
| `expand` | `true` |

Parameters like `callback`, `jsonp`, `api_key`, and `token` are **skipped** — empty or placeholder values cause noise rather than useful results.

### 7. Distributed Redis Mode

For large-scope bug bounty programs or internal network assessments:

```bash
# Terminal 1 — Start Redis
redis-server

# Terminal 2 — Node 1
python crawler_v2.py https://target.com --redis redis://localhost:6379 --concurrency 30

# Terminal 3 — Node 2 (shares same queue)
python crawler_v2.py https://target.com --redis redis://localhost:6379 --concurrency 30
```

Uses `BZPOPMIN` (blocking pop) for zero-busy-wait dequeue. Falls back to polled `ZPOPMIN` on Redis < 5.0.

---

## 🎯 Effective Usage Guide

### Scenario 1 — Quick Initial Recon

Use this first to get fast results before committing to a deep crawl.

```bash
python crawler_v2.py https://target.com \
  --depth 5 \
  --concurrency 15 \
  --output quick_recon.txt
```

Review `quick_recon.txt` to identify interesting API prefixes (`/api/v2/`, `/internal/`, etc.) before running deeper.

---

### Scenario 2 — Full Bug Bounty Engagement

```bash
python crawler_v2.py https://target.com \
  --playwright \
  --wayback \
  --param-discovery \
  --depth 30 \
  --concurrency 20 \
  --js-boost 3.0 \
  --output full_endpoints.txt \
  -v
```

- `--playwright` catches SPA-rendered routes
- `--wayback` recovers deprecated/hidden endpoints
- `--param-discovery` triggers JSON responses from endpoints that default to HTML
- `--js-boost 3.0` aggressively prioritizes JS bundle analysis

---

### Scenario 3 — Stealth / Rate-Limited Target

```bash
python crawler_v2.py https://target.com \
  --concurrency 3 \
  --depth 20 \
  --proxies proxies.txt \
  --no-verify-proxies \
  --output stealth_scan.txt
```

- Low concurrency (`3`) avoids triggering WAF rate limits
- Proxy rotation distributes requests across IPs
- `--no-verify-proxies` skips the health check if the probe URL is blocked by policy

---

### Scenario 4 — SPA-Heavy Target (React / Next.js / Vue)

```bash
python crawler_v2.py https://spa-target.com \
  --playwright \
  --playwright-concurrency 4 \
  --no-js-ast \
  --depth 10 \
  --concurrency 5
```

- Playwright handles dynamic rendering
- `--no-js-ast` reduces overhead since Playwright already intercepts XHR calls
- Lower depth/concurrency since browser contexts are heavier than raw HTTP

---

### Scenario 5 — Large Scope / Multiple Subdomains via Redis

```bash
# Start Redis
redis-server --daemonize yes

# Run multiple crawler instances targeting different subdomains
python crawler_v2.py https://api.target.com --redis redis://127.0.0.1:6379 --concurrency 25 &
python crawler_v2.py https://admin.target.com --redis redis://127.0.0.1:6379 --concurrency 25 &
python crawler_v2.py https://app.target.com --redis redis://127.0.0.1:6379 --concurrency 25 &
```

---

### Pro Tips for Maximum Yield

**Feed output into further tools:**

```bash
# Pipe live JSON finds directly into curl for quick response inspection
python crawler_v2.py https://target.com | xargs -I{} curl -s -o /dev/null -w "%{http_code} {}\n" {}

# Pipe into ffuf for further endpoint fuzzing
cat found_json_links.txt | ffuf -u FUZZ -w - -mc 200,201,301,302,403
```

**Custom parameter wordlist:**

```bash
# Use a targeted wordlist based on observed API naming conventions
echo -e "user_id\naccount_id\norg_id\ntenant\nrole\ntoken\nscope" > params.txt
python crawler_v2.py https://target.com --param-discovery --param-wordlist params.txt
```

**Resume interrupted crawls:** The SQLite database (`crawler.db`) persists visited URLs and JSON finds. Re-running the same command skips already-visited URLs automatically — no data is lost on interruption.

**Adjust JS boost for API-heavy targets:**

```bash
# JS bundles often contain hundreds of embedded API routes — boost aggressively
python crawler_v2.py https://target.com --js-boost 5.0
```

**Verbose mode for debugging missed endpoints:**

```bash
python crawler_v2.py https://target.com -v 2>&1 | grep -E "\[JSON\]|robots|Fetch failed"
```

---

## 📁 Output Files

| File | Contents |
|---|---|
| `found_json_links.txt` | One discovered JSON endpoint URL per line |
| `crawler.db` | SQLite: visited URLs, JSON endpoints, content hashes |

The SQLite database can be queried directly:

```bash
# All JSON endpoints found
sqlite3 crawler.db "SELECT url FROM json_links ORDER BY discovered_at DESC;"

# Total visited pages
sqlite3 crawler.db "SELECT COUNT(*) FROM visited;"

# Endpoints containing 'api'
sqlite3 crawler.db "SELECT url FROM json_links WHERE url LIKE '%/api/%';"
```

---

## ⚙️ Configuration Reference

### Proxy File Format

One proxy per line:

```
http://192.168.1.10:8080
http://10.0.0.5:3128
socks5://127.0.0.1:1080
```

### User-Agent File Format

One User-Agent string per line:

```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36
Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0
```

### Parameter Wordlist Format

One parameter name per line:

```
user_id
account_id
tenant_id
include_deleted
api_version
```

---

## 🔧 Troubleshooting

**`ModuleNotFoundError: No module named 'aiohttp'`**
You are not inside the virtual environment. Run `source venv/bin/activate` (Linux/macOS) or `.\venv\Scripts\Activate.ps1` (Windows) first.

**`playwright not installed — headless mode disabled`**
Install with `pip install playwright && playwright install chromium` inside your active venv.

**`pip install aioredis` fails**
Ensure pip is upgraded: `pip install --upgrade pip`, then retry.

**`Redis < 5.0 detected — BZPOPMIN unavailable`**
The tool automatically falls back to polled ZPOPMIN. Consider upgrading Redis for better performance under high concurrency.

**Very slow crawl on JS-heavy targets without esprima**
Install `esprima`: `pip install esprima`. Without it, JS analysis falls back to regex which misses many dynamically assembled URL strings.

**`All retries exhausted` for many URLs**
The target may be rate-limiting. Reduce `--concurrency` to `3–5` and optionally add `--proxies`.

**Windows — `asyncio.run()` errors on Python < 3.10**
Ensure you are using Python 3.9+. On Windows, the default asyncio event loop policy changed in 3.10; the tool is compatible with 3.9+.

**Content-type warnings / unexpected JSON detections**
The crawler intentionally sniffs body content independent of headers. This is by design — misconfigured APIs serving JSON with wrong content-types are a real finding.

---

## 📝 Changelog (v2 Fixes)

| Fix | Description |
|---|---|
| **FIX 1** | `RedisQueue.get()` replaced busy-poll loop with `BZPOPMIN` blocking pop; falls back to polled `ZPOPMIN` on Redis < 5.0 |
| **FIX 2** | Worker shutdown uses sentinel-based graceful drain instead of mid-flight task cancellation |
| **FIX 3** | Trailing-slash stripping removed from URL normalization — `/v1/` and `/v1` are distinct REST paths |
| **FIX 4** | Host locks use registry-lock pattern to eliminate race conditions on first-seen hosts |
| **FIX 5** | `param_mutate` uses per-parameter semantic values; empty-value params (callback, api_key) are skipped |
| **FIX 6** | `_parse_json` has a hard 20-level recursion depth cap to prevent `RecursionError` on deep API responses |
| **FIX 7** | Playwright `on_response` handlers are collected via `ensure_future` and awaited via `gather` before context close |
| **FIX 8** | Proxy health probing is gated behind `--verify-proxies` flag; probe URL is configurable |
| **FIX 9** | Wayback seeding pre-filters by raw `netloc` before `normalize_url` to avoid wasted work on out-of-scope CDN subdomains |
| **FIX 10** | Per-URL logging demoted to `DEBUG`; periodic `INFO` stats summary every 30 seconds |
| **FIX 11** | Separate per-content-type body size caps: HTML 5MB, JS 10MB, JSON 2MB, generic 5MB |

---

## 🤝 Contributing

Pull requests welcome. Please ensure any new features include:

- Handling for both local queue and Redis queue modes
- Appropriate logging at `DEBUG` (per-item) vs `INFO` (summary) levels
- Updated entry in the flags reference table above

---

*Built for authorized security testing. Use legally and responsibly.*
