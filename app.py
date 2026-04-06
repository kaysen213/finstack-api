"""
FinStack API v1.0
Unified Financial Data — Crypto · Stocks · Forex · Macro
One API. One key. One format. Pre-computed indicators. AI-discoverable.

Aggregates: CoinGecko, DeFiLlama, Alpha Vantage, FRED, ExchangeRate API, Alternative.me
"""

import os
import yfinance as yf
import json
import time
import math
import hashlib
import logging
from datetime import datetime, timezone
from functools import wraps
from collections import defaultdict

from flask import Flask, jsonify, request, Response, g
import requests as upstream

# ── Yahoo Finance session: browser UA prevents cloud-IP blocks ──
_YF_SESSION = None
def _yf_sess():
    global _YF_SESSION
    if _YF_SESSION is None:
        s = upstream.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _YF_SESSION = s
    return _YF_SESSION

def _sf(v, d=4):
    """Safe float: converts NaN/Inf/None to None, otherwise round to d digits."""
    try:
        if v is None: return None
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, d)
    except (TypeError, ValueError):
        return None

# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════

FRED_KEY = os.environ.get("FRED_KEY", "")
PORT = int(os.environ.get("PORT", 8000))
ENV = os.environ.get("ENV", "development")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# RapidAPI proxy secret — set this in Railway env vars after creating your RapidAPI listing.
# Every request through RapidAPI will carry this header; we reject anything that doesn't match.
RAPIDAPI_PROXY_SECRET = os.environ.get("RAPIDAPI_PROXY_SECRET", "")

# Map RapidAPI plan names (as they appear in X-RapidAPI-Subscription) to internal tiers.
# Update these to match whatever you name your plans in the RapidAPI dashboard.
RAPIDAPI_TIER_MAP = {
    "FREE": "free",
    "BASIC": "basic",
    "PRO": "pro",
    "ULTRA": "ultra",
    "MEGA": "ultra",
}

# Rate limit config (per API key, per minute)
RATE_LIMITS = {
    "free": 10,       # 10 req/min = ~600/hour = ~100/day reasonable use
    "basic": 60,      # 60 req/min
    "pro": 300,       # 300 req/min
    "ultra": 1000,    # 1000 req/min
    "internal": 9999, # internal/testing
}

# ══════════════════════════════════════════════════════════════════════
# APP SETUP
# ══════════════════════════════════════════════════════════════════════

app = Flask(__name__)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("finstack")

# ══════════════════════════════════════════════════════════════════════
# CACHE — in-memory, production would use Redis
# ══════════════════════════════════════════════════════════════════════

_cache: dict[str, dict] = {}
DEFAULT_TTL = 60


def ck(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def cache_get(key: str):
    e = _cache.get(key)
    if e and time.time() - e["t"] < e["ttl"]:
        return e["d"]
    return None


def cache_set(key: str, data, ttl: int = DEFAULT_TTL):
    _cache[key] = {"d": data, "t": time.time(), "ttl": ttl}
    # Evict old entries if cache grows too large
    if len(_cache) > 5000:
        cutoff = time.time()
        expired = [k for k, v in _cache.items() if cutoff - v["t"] > v["ttl"]]
        for k in expired:
            del _cache[k]


# ══════════════════════════════════════════════════════════════════════
# RATE LIMITER — sliding window per API key
# ══════════════════════════════════════════════════════════════════════

_rate_windows: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(api_key: str, tier: str = "free") -> tuple[bool, int]:
    """Returns (allowed, remaining)."""
    limit = RATE_LIMITS.get(tier, RATE_LIMITS["free"])
    now = time.time()
    window = _rate_windows[api_key]

    # Remove entries older than 60 seconds
    _rate_windows[api_key] = [t for t in window if now - t < 60]
    window = _rate_windows[api_key]

    if len(window) >= limit:
        return False, 0

    _rate_windows[api_key].append(now)
    return True, limit - len(window) - 1


# ══════════════════════════════════════════════════════════════════════
# MIDDLEWARE — auth, rate limit, CORS, request logging
# ══════════════════════════════════════════════════════════════════════

# Paths that don't require auth
PUBLIC_PATHS = {"/", "/health", "/llms.txt", "/ai-plugin.json", "/openapi.json",
                "/llms-full.txt", "/robots.txt", "/favicon.ico"}


@app.before_request
def before_request_handler():
    g.start_time = time.time()

    # Skip auth for public paths
    if request.path in PUBLIC_PATHS:
        return

    rapidapi_secret = request.headers.get("X-RapidAPI-Proxy-Secret")
    rapidapi_user   = request.headers.get("X-RapidAPI-User", "anonymous")
    rapidapi_sub    = request.headers.get("X-RapidAPI-Subscription", "FREE").upper()

    # ── Path 1: Request arrived through RapidAPI proxy ──────────────────
    if rapidapi_secret:
        # If RAPIDAPI_PROXY_SECRET is configured, enforce it.
        # Until you set it in Railway, all proxy requests are accepted.
        if RAPIDAPI_PROXY_SECRET and rapidapi_secret != RAPIDAPI_PROXY_SECRET:
            return jsonify({"error": "Invalid proxy secret"}), 403

        api_key = f"rapidapi:{rapidapi_user}"
        tier = RAPIDAPI_TIER_MAP.get(rapidapi_sub, "free")

    # ── Path 2: Direct request (dev, testing, or bypassing RapidAPI) ────
    else:
        # In production with a proxy secret configured, block direct access
        # so users can't bypass RapidAPI billing.
        if ENV == "production" and RAPIDAPI_PROXY_SECRET:
            return jsonify({
                "error": "Direct access disabled. Use the API via RapidAPI.",
                "url": "https://rapidapi.com/search/finstack",
            }), 403

        direct_key = (
            request.headers.get("X-API-Key")
            or request.args.get("apikey")
        )
        api_key = direct_key or (request.remote_addr or "anonymous")
        tier = "free"

    g.api_key = api_key
    g.tier = tier

    allowed, remaining = check_rate_limit(api_key, tier)
    if not allowed:
        return jsonify({
            "error": "Rate limit exceeded",
            "limit": RATE_LIMITS[tier],
            "window": "60 seconds",
            "tier": tier,
            "upgrade": "Get higher limits at https://rapidapi.com/search/finstack",
        }), 429


@app.after_request
def after_request_handler(response):
    # CORS headers
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, X-RapidAPI-Proxy-Secret"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"

    # Cache headers for CDN
    if request.method == "GET" and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=30"

    # Request logging
    duration = round((time.time() - g.get("start_time", time.time())) * 1000, 1)
    if request.path not in {"/health", "/favicon.ico"}:
        log.info(f"{request.method} {request.path} → {response.status_code} ({duration}ms)")

    return response


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint not found", "docs": "/openapi.json"}), 404


@app.errorhandler(500)
def server_error(e):
    log.error(f"Internal error: {e}")
    return jsonify({"error": "Internal server error"}), 500


# ══════════════════════════════════════════════════════════════════════
# UPSTREAM FETCHER
# ══════════════════════════════════════════════════════════════════════

def fetch(url: str, params: dict = None, headers: dict = None, timeout: int = 10):
    try:
        r = upstream.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except upstream.exceptions.Timeout:
        return {"_error": "Upstream timeout", "_source": url}
    except upstream.exceptions.ConnectionError:
        return {"_error": "Upstream connection failed", "_source": url}
    except Exception as e:
        return {"_error": str(e), "_source": url}


def is_error(data) -> bool:
    return isinstance(data, dict) and "_error" in data


def error_response(data, source: str):
    return jsonify({
        "error": data.get("_error", "Unknown upstream error"),
        "source": source,
        "timestamp": now_iso(),
    }), 502


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════

def sma(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None
    return round(sum(prices[-period:]) / period, 6)


def ema(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    val = sum(prices[:period]) / period
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return round(val, 6)


def rsi(prices: list[float], period: int = 14) -> float | None:
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(0, d))
        losses.append(max(0, -d))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)


def volatility(prices: list[float], period: int = 14) -> float | None:
    if len(prices) < period:
        return None
    s = prices[-period:]
    mean = sum(s) / len(s)
    var = sum((p - mean) ** 2 for p in s) / len(s)
    return round(math.sqrt(var), 6)


def bollinger_bands(prices: list[float], period: int = 20, std_dev: int = 2):
    if len(prices) < period:
        return None
    s = prices[-period:]
    middle = sum(s) / len(s)
    sd = math.sqrt(sum((p - middle) ** 2 for p in s) / len(s))
    return {
        "upper": round(middle + std_dev * sd, 4),
        "middle": round(middle, 4),
        "lower": round(middle - std_dev * sd, 4),
    }


def macd(prices: list[float]):
    ema12 = ema(prices, 12)
    ema26 = ema(prices, 26)
    if ema12 is None or ema26 is None:
        return None
    macd_line = round(ema12 - ema26, 6)
    return {"macd": macd_line, "ema_12": ema12, "ema_26": ema26}


def indicators(prices: list[float]) -> dict:
    return {
        "sma_7": sma(prices, 7),
        "sma_14": sma(prices, 14),
        "sma_30": sma(prices, 30),
        "sma_50": sma(prices, 50),
        "sma_200": sma(prices, 200),
        "ema_12": ema(prices, 12),
        "ema_26": ema(prices, 26),
        "rsi_14": rsi(prices, 14),
        "volatility_14d": volatility(prices, 14),
        "bollinger_20": bollinger_bands(prices, 20),
        "macd": macd(prices),
    }


# ═══════════════════════════════════════════════════════════════
# CRYPTO ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route("/v1/crypto/prices")
def crypto_prices():
    """Current prices for one or more coins with market data."""
    coins = request.args.get("coins", "bitcoin,ethereum,solana")
    vs = request.args.get("vs", "usd")

    k = ck("cp", coins, vs)
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch("https://api.coingecko.com/api/v3/simple/price", params={
        "ids": coins, "vs_currencies": vs,
        "include_24hr_change": "true", "include_market_cap": "true",
        "include_24hr_vol": "true", "include_last_updated_at": "true",
    })
    if is_error(raw):
        return error_response(raw, "coingecko")

    data = {}
    for cid, v in raw.items():
        data[cid] = {
            "price": v.get(vs),
            "change_24h_pct": round(v.get(f"{vs}_24h_change", 0), 2) if v.get(f"{vs}_24h_change") else None,
            "market_cap": v.get(f"{vs}_market_cap"),
            "volume_24h": v.get(f"{vs}_24h_vol"),
            "last_updated": v.get("last_updated_at"),
            "currency": vs,
        }

    result = {"data": data, "count": len(data), "source": "coingecko", "timestamp": now_iso()}
    cache_set(k, result, 30)
    return jsonify(result)


@app.route("/v1/crypto/history")
def crypto_history():
    """Historical prices with pre-computed technical indicators."""
    coin = request.args.get("coin", "bitcoin")
    vs = request.args.get("vs", "usd")
    days = min(int(request.args.get("days", 30)), 365)

    k = ck("ch", coin, vs, days)
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch(f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart",
                params={"vs_currency": vs, "days": days})
    if is_error(raw):
        return error_response(raw, "coingecko")

    price_data = raw.get("prices", [])
    prices = [p[1] for p in price_data]
    timestamps = [p[0] for p in price_data]

    # Downsample to max 200 points
    step = max(1, len(prices) // 200)
    sampled = [{"ts": timestamps[i], "price": round(prices[i], 4)}
               for i in range(0, len(prices), step)]

    result = {
        "coin": coin, "currency": vs, "days": days,
        "data_points": len(prices),
        "current_price": round(prices[-1], 4) if prices else None,
        "period_high": round(max(prices), 4) if prices else None,
        "period_low": round(min(prices), 4) if prices else None,
        "price_change_pct": round(((prices[-1] - prices[0]) / prices[0]) * 100, 2) if len(prices) >= 2 else None,
        "indicators": indicators(prices),
        "prices": sampled,
        "source": "coingecko", "timestamp": now_iso(),
    }
    cache_set(k, result, 300)
    return jsonify(result)


@app.route("/v1/crypto/coins")
def crypto_coins():
    """Search or list available coins."""
    query = request.args.get("q", "")

    k = ck("coins_list")
    c = cache_get(k)
    if not c:
        raw = fetch("https://api.coingecko.com/api/v3/coins/list")
        if is_error(raw):
            return error_response(raw, "coingecko")
        c = raw
        cache_set(k, c, 3600)

    if query:
        q = query.lower()
        filtered = [coin for coin in c if q in coin.get("id", "").lower()
                     or q in coin.get("symbol", "").lower()
                     or q in coin.get("name", "").lower()][:50]
    else:
        filtered = c[:100]

    return jsonify({"coins": filtered, "count": len(filtered), "source": "coingecko", "timestamp": now_iso()})


@app.route("/v1/crypto/trending")
def crypto_trending():
    """Trending coins — most searched in the last 24h."""
    k = ck("trending")
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch("https://api.coingecko.com/api/v3/search/trending")
    if is_error(raw):
        return error_response(raw, "coingecko")

    coins = [{
        "id": i["item"]["id"], "name": i["item"]["name"],
        "symbol": i["item"]["symbol"], "market_cap_rank": i["item"].get("market_cap_rank"),
        "price_btc": i["item"].get("price_btc"), "thumb": i["item"].get("thumb"),
    } for i in raw.get("coins", [])]

    result = {"trending": coins, "source": "coingecko", "timestamp": now_iso()}
    cache_set(k, result, 300)
    return jsonify(result)


@app.route("/v1/crypto/fear-greed")
def crypto_fear_greed():
    """Crypto Fear & Greed Index — current value and recent history."""
    days = min(int(request.args.get("days", 10)), 90)

    k = ck("fg", days)
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch(f"https://api.alternative.me/fng/?limit={days}&format=json")
    if is_error(raw):
        return error_response(raw, "alternative.me")

    entries = raw.get("data", [])
    history = [{
        "value": int(e["value"]),
        "label": e.get("value_classification"),
        "date": datetime.fromtimestamp(int(e["timestamp"]), tz=timezone.utc).strftime("%Y-%m-%d"),
    } for e in entries]

    result = {
        "current": history[0] if history else None,
        "history": history,
        "source": "alternative.me", "timestamp": now_iso(),
    }
    cache_set(k, result, 600)
    return jsonify(result)


@app.route("/v1/crypto/defi")
def crypto_defi():
    """Top DeFi protocols ranked by Total Value Locked."""
    limit = min(int(request.args.get("limit", 25)), 100)

    k = ck("defi", limit)
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch("https://api.llama.fi/protocols")
    if is_error(raw) or not isinstance(raw, list):
        return error_response(raw if isinstance(raw, dict) else {"_error": "Bad response"}, "defillama")

    protocols = [{
        "name": p.get("name"), "symbol": p.get("symbol"),
        "tvl": p.get("tvl"), "chain": p.get("chain"),
        "chains": p.get("chains", []), "category": p.get("category"),
        "change_1d": p.get("change_1d"), "change_7d": p.get("change_7d"),
        "mcap_tvl": round(p["mcap"] / p["tvl"], 2) if p.get("mcap") and p.get("tvl") else None,
        "logo": p.get("logo"),
    } for p in raw[:limit]]

    result = {"protocols": protocols, "count": len(protocols), "source": "defillama", "timestamp": now_iso()}
    cache_set(k, result, 300)
    return jsonify(result)


@app.route("/v1/crypto/defi/chains")
def defi_chains():
    """TVL by blockchain from DeFiLlama."""
    k = ck("defi_chains")
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch("https://api.llama.fi/v2/chains")
    if is_error(raw):
        return error_response(raw, "defillama")

    chains = [{
        "name": ch.get("name"), "tvl": ch.get("tvl"),
        "gecko_id": ch.get("gecko_id"), "token_symbol": ch.get("tokenSymbol"),
    } for ch in (raw if isinstance(raw, list) else [])[:50]]

    result = {"chains": chains, "count": len(chains), "source": "defillama", "timestamp": now_iso()}
    cache_set(k, result, 300)
    return jsonify(result)


@app.route("/v1/crypto/global")
def crypto_global():
    """Global crypto market stats."""
    k = ck("global")
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch("https://api.coingecko.com/api/v3/global")
    if is_error(raw):
        return error_response(raw, "coingecko")

    d = raw.get("data", {})
    mcp = d.get("market_cap_percentage", {})

    result = {
        "total_market_cap_usd": d.get("total_market_cap", {}).get("usd"),
        "total_volume_24h_usd": d.get("total_volume", {}).get("usd"),
        "btc_dominance": round(mcp.get("btc", 0), 2),
        "eth_dominance": round(mcp.get("eth", 0), 2),
        "active_coins": d.get("active_cryptocurrencies"),
        "markets": d.get("markets"),
        "market_cap_change_24h_pct": round(d.get("market_cap_change_percentage_24h_usd", 0), 2),
        "top_coins_by_dominance": {k: round(v, 2) for k, v in sorted(mcp.items(), key=lambda x: -x[1])[:10]},
        "source": "coingecko", "timestamp": now_iso(),
    }
    cache_set(k, result, 120)
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════
# FOREX ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route("/v1/forex/rates")
def forex_rates():
    """Exchange rates for 150+ currencies."""
    base = request.args.get("base", "USD").upper()
    targets = request.args.get("targets", "")

    k = ck("fx", base, targets)
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch(f"https://open.er-api.com/v6/latest/{base}")
    if is_error(raw) or raw.get("result") != "success":
        return error_response(raw if isinstance(raw, dict) else {"_error": "Failed"}, "exchangerate-api")

    rates = raw.get("rates", {})
    if targets:
        tl = [t.strip().upper() for t in targets.split(",")]
        rates = {k: v for k, v in rates.items() if k in tl}

    result = {"base": base, "rates": rates, "pairs": len(rates), "source": "exchangerate-api", "timestamp": now_iso()}
    cache_set(k, result, 3600)
    return jsonify(result)


@app.route("/v1/forex/convert")
def forex_convert():
    """Convert between currencies."""
    from_c = request.args.get("from", "USD").upper()
    to_c = request.args.get("to", "NZD").upper()
    amount = float(request.args.get("amount", 1))

    raw = fetch(f"https://open.er-api.com/v6/latest/{from_c}")
    if is_error(raw):
        return error_response(raw, "exchangerate-api")

    rate = raw.get("rates", {}).get(to_c)
    if not rate:
        return jsonify({"error": f"Currency {to_c} not found"}), 404

    return jsonify({
        "from": from_c, "to": to_c, "amount": amount,
        "rate": rate, "converted": round(amount * rate, 4),
        "source": "exchangerate-api", "timestamp": now_iso(),
    })


# ═══════════════════════════════════════════════════════════════
# STOCKS ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route("/v1/stocks/quote")
def stock_quote():
    """Real-time stock quote via Yahoo Finance — no rate limits."""
    symbol = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required (e.g. ?symbol=AAPL)"}), 400

    k = ck("sq", symbol)
    c = cache_get(k)
    if c:
        return jsonify(c)

    try:
        # Direct Yahoo Finance chart API — bypasses yfinance routing blocked on some cloud IPs
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d&includePrePost=false"
        resp = _yf_sess().get(url, timeout=10)
        payload = resp.json()
        chart = payload.get("chart", {})
        err = chart.get("error")
        results_list = chart.get("result")
        if err or not results_list:
            msg = err.get("description", "No data") if err else f"No data for '{symbol}'"
            return jsonify({"error": msg, "symbol": symbol, "source": "yahoo_finance"}), 404
        meta = results_list[0]["meta"]
        price      = _sf(meta.get("regularMarketPrice"))
        prev       = _sf(meta.get("previousClose") or meta.get("chartPreviousClose"))
        if price is None:
            return jsonify({"error": f"No price data for '{symbol}'. Market may be closed.", "source": "yahoo_finance"}), 404
        change     = round(price - prev, 4) if prev is not None else None
        change_pct = round((change / prev) * 100, 2) if prev and change else None
        result = {
            "symbol": meta.get("symbol", symbol),
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "open": _sf(meta.get("regularMarketOpen")),
            "high": _sf(meta.get("regularMarketDayHigh")),
            "low": _sf(meta.get("regularMarketDayLow")),
            "prev_close": prev,
            "volume": meta.get("regularMarketVolume"),
            "market_cap": None,
            "fifty_two_week_high": _sf(meta.get("fiftyTwoWeekHigh")),
            "fifty_two_week_low": _sf(meta.get("fiftyTwoWeekLow")),
            "currency": meta.get("currency"),
            "exchange": meta.get("exchangeName"),
            "market_state": meta.get("marketState"),
            "source": "yahoo_finance", "timestamp": now_iso(),
        }
    except Exception as e:
        return jsonify({"error": f"Failed to fetch '{symbol}': {str(e)}", "source": "yahoo_finance"}), 502

    cache_set(k, result, 60)
    return jsonify(result)

@app.route("/v1/stocks/history")
def stock_history():
    """Daily OHLCV history via Yahoo Finance — no rate limits.
    ?symbol=AAPL  &period=3mo  &interval=1d
    period:   1d 5d 1mo 3mo 6mo 1y 2y 5y ytd max
    interval: 1d 1wk 1mo
    """
    symbol = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400
    period   = request.args.get("period", "3mo")
    interval = request.args.get("interval", "1d")

    k = ck("sh", symbol, period, interval)
    c = cache_get(k)
    if c:
        return jsonify(c)

    try:
        hist = yf.Ticker(symbol, session=_yf_sess()).history(period=period, interval=interval)
        if hist.empty:
            return jsonify({"error": f"No history for '{symbol}'. Verify the ticker.", "source": "yahoo_finance"}), 404
        records = [
            {
                "date": str(date.date()),
                "open":   round(float(row["Open"]),   4),
                "high":   round(float(row["High"]),   4),
                "low":    round(float(row["Low"]),    4),
                "close":  round(float(row["Close"]),  4),
                "volume": int(row["Volume"]),
            }
            for date, row in hist.iterrows()
        ]
    except Exception as e:
        return jsonify({"error": f"Failed to fetch history for '{symbol}': {str(e)}", "source": "yahoo_finance"}), 502

    result = {
        "symbol": symbol, "period": period, "interval": interval,
        "count": len(records), "data": records,
        "source": "yahoo_finance", "timestamp": now_iso(),
    }
    cache_set(k, result, 43200)  # 12h — daily data only changes at market close
    return jsonify(result)

@app.route("/v1/stocks/search")
def stock_search():
    """Search for stock symbols by name or keyword via Yahoo Finance."""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "q parameter required (e.g. ?q=apple)"}), 400

    k = ck("ss", query)
    c = cache_get(k)
    if c:
        return jsonify(c)

    try:
        results = yf.Search(query, session=_yf_sess(), max_results=10).quotes
        matches = [
            {
                "symbol":   q.get("symbol"),
                "name":     q.get("shortname") or q.get("longname"),
                "type":     q.get("quoteType"),
                "exchange": q.get("exchange"),
                "currency": q.get("currency"),
            }
            for q in results
        ]
    except Exception as e:
        return jsonify({"error": f"Search failed: {str(e)}", "source": "yahoo_finance"}), 502

    result = {"query": query, "results": matches, "count": len(matches), "source": "yahoo_finance", "timestamp": now_iso()}
    cache_set(k, result, 3600)
    return jsonify(result)

@app.route("/v1/macro/indicators")
def macro_indicators():
    """Key US macroeconomic indicators from FRED."""
    if not FRED_KEY:
        return jsonify({"error": "FRED_KEY not configured. Get free key at fred.stlouisfed.org", "source": "fred"}), 503

    k = ck("macro")
    c = cache_get(k)
    if c:
        return jsonify(c)

    series_map = {
        "gdp_growth_pct": "A191RL1Q225SBEA",
        "cpi_index": "CPIAUCSL",
        "unemployment_rate": "UNRATE",
        "fed_funds_rate": "FEDFUNDS",
        "treasury_10y_yield": "DGS10",
        "treasury_2y_yield": "DGS2",
        "inflation_rate_yoy": "FPCPITOTLZGUSA",
    }

    out = {}
    for name, sid in series_map.items():
        raw = fetch("https://api.stlouisfed.org/fred/series/observations", params={
            "series_id": sid, "api_key": FRED_KEY,
            "file_type": "json", "sort_order": "desc", "limit": 5,
        })
        obs = raw.get("observations", []) if not is_error(raw) else []
        out[name] = {
            "value": obs[0]["value"] if obs else None,
            "date": obs[0]["date"] if obs else None,
            "recent": [{"date": o["date"], "value": o["value"]} for o in obs],
        }

    # Add yield curve spread (10Y - 2Y) if both available
    t10 = out.get("treasury_10y_yield", {}).get("value")
    t2 = out.get("treasury_2y_yield", {}).get("value")
    if t10 and t2 and t10 != "." and t2 != ".":
        try:
            out["yield_curve_spread"] = {"value": str(round(float(t10) - float(t2), 2)), "note": "10Y minus 2Y. Negative = inverted (recession signal)"}
        except ValueError:
            pass

    result = {"indicators": out, "source": "fred", "timestamp": now_iso()}
    cache_set(k, result, 3600)
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════
# OVERVIEW — THE KILLER ENDPOINT
# ═══════════════════════════════════════════════════════════════

@app.route("/v1/overview")
def overview():
    """Full market overview in ONE call. Crypto + forex + fear/greed + global stats."""
    k = ck("overview")
    c = cache_get(k)
    if c:
        return jsonify(c)

    # Parallel would be better but keep it simple with sequential
    crypto = fetch("https://api.coingecko.com/api/v3/simple/price", params={
        "ids": "bitcoin,ethereum,solana,binancecoin,ripple,cardano,dogecoin",
        "vs_currencies": "usd", "include_24hr_change": "true", "include_market_cap": "true",
    })
    fg = fetch("https://api.alternative.me/fng/?limit=1&format=json")
    fx = fetch("https://open.er-api.com/v6/latest/USD")
    gl = fetch("https://api.coingecko.com/api/v3/global")

    result = {"crypto": {}, "fear_greed": None, "forex": {}, "global": {}, "timestamp": now_iso()}

    if not is_error(crypto):
        for cid, v in crypto.items():
            result["crypto"][cid] = {
                "price_usd": v.get("usd"),
                "change_24h_pct": round(v.get("usd_24h_change", 0), 2) if v.get("usd_24h_change") else None,
                "market_cap": v.get("usd_market_cap"),
            }

    fg_data = fg.get("data", []) if not is_error(fg) else []
    if fg_data:
        result["fear_greed"] = {"value": int(fg_data[0]["value"]), "label": fg_data[0].get("value_classification")}

    if not is_error(fx) and fx.get("result") == "success":
        for cur in ["EUR", "GBP", "JPY", "NZD", "AUD", "CHF", "CAD", "CNY", "KRW", "SGD", "HKD"]:
            r = fx.get("rates", {}).get(cur)
            if r:
                result["forex"][f"USD_{cur}"] = r

    if not is_error(gl):
        gd = gl.get("data", {})
        result["global"] = {
            "total_market_cap_usd": gd.get("total_market_cap", {}).get("usd"),
            "btc_dominance": round(gd.get("market_cap_percentage", {}).get("btc", 0), 2),
            "active_coins": gd.get("active_cryptocurrencies"),
            "market_cap_change_24h_pct": round(gd.get("market_cap_change_percentage_24h_usd", 0), 2),
        }

    cache_set(k, result, 60)
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════
# AI DISCOVERABILITY
# ═══════════════════════════════════════════════════════════════

@app.route("/llms.txt")
def llms_txt():
    return Response(open(os.path.join(os.path.dirname(__file__), "llms.txt")).read()
                    if os.path.exists(os.path.join(os.path.dirname(__file__), "llms.txt"))
                    else _generate_llms_txt(), mimetype="text/plain")


@app.route("/llms-full.txt")
def llms_full_txt():
    """Extended version with full parameter docs for AI coding assistants."""
    return Response(_generate_llms_full_txt(), mimetype="text/plain")


@app.route("/ai-plugin.json")
def ai_plugin():
    return jsonify({
        "schema_version": "v1",
        "name_for_human": "FinStack Financial Data API",
        "name_for_model": "finstack",
        "description_for_human": "Real-time crypto, stock, forex, and macro data with pre-computed technical analysis indicators.",
        "description_for_model": "FinStack is a unified financial data API. Use it to: get cryptocurrency prices and history with RSI/SMA/EMA/MACD/Bollinger indicators pre-computed, get stock quotes and daily history with technical indicators, convert currencies with real-time forex rates, get DeFi protocol TVL rankings, get the Crypto Fear & Greed Index, get US macro indicators (GDP, CPI, unemployment, fed funds rate, treasury yields, yield curve), or get a full market overview in a single call. All responses are JSON. Base URL: /v1/",
        "auth": {"type": "none"},
        "api": {"type": "openapi", "url": "/openapi.json"},
    })


@app.route("/openapi.json")
def openapi_spec():
    return jsonify(_generate_openapi())


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nSitemap: /openapi.json\n", mimetype="text/plain")


# ═══════════════════════════════════════════════════════════════
# ROOT & HEALTH
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def root():
    return jsonify({
        "name": "FinStack API",
        "version": "1.0.0",
        "tagline": "Unified financial data — crypto, stocks, orez, macro. One API, one key, one format.",
        "docs": "/openapi.json",
        "llms": "/llms.txt",
        "llms_full": "/llms-full.txt",
        "ai_plugin": "/ai-plugin.json",
        "endpoints": {
            "crypto": ["/v1/crypto/prices", "/v1/crypto/history", "/v1/crypto/coins",
                       "/v1/crypto/trending", "/v1/crypto/fear-greed",
                       "/v1/crypto/defi", "/v1/crypto/defi/chains", "/v1/crypto/global"],
            "forex": ["/v1/forex/rates", "/v1/forex/convert"],
            "stocks": ["/v1/stocks/quote", "/v1/stocks/history", "/v1/stocks/search"],
            "macro": ["/v1/macro/indicators"],
            "overview": ["/v1/overview"],
        },
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "cache_size": len(_cache), "version": "1.0.0"})


# ═══════════════════════════════════════════════════════════════
# ════════════════════════════════
# SPEC GENERATORS
# ═══════════════════════════════════════════════════════════════

def _generate_llms_txt():
    return """# FinStack API
> Unified financial data API — crypto, stocks, forex, macro indicators. One key, one format, pre-computed technical indicators.

## Endpoints

### Crypto
- GET /v1/crypto/prices?coins=bitcoin,ethereum&vs=usd — Current prices, 24h change, market cap, volume
- GET /v1/crypto/history?coin=bitcoin&days=30 — Price history + RSI, SMA, EMA, MACD, Bollinger Bands
- GET /v1/crypto/coins?q=bit — Search available coins
- GET /v1/crypto/trending — Most searched coins in last 24h
- GET /v1/crypto/fear-greed?days=10 — Fear & Greed Index
- GET /v1/crypto/defi?limit=25 — Top DeFi protocols by TVL
- GET /v1/crypto/defi/chains — TVL by blockchain
- GET /v1/crypto/global — Total market cap, BTC dominance

### Forex
- GET /v1/forex/rates?base=USD&targets=EUR,GBP,NZD — Exchange rates (150+ currencies)
- GET /v1/forex/convert?from=USD&to=NZD&amount=100 — Convert between currencies

### Stocks
- GET /v1/stocks/quote?symbol=AAPL — Real-time quote
- GET /v1/stocks/history?symbol=AAPL — Daily OHLCV + RSI, SMA, EMA, MACD, Bollinger
- GET /v1/stocks/search?q=apple — Search symbols

### Macro
- GET /v1/macro/indicators — GDP, CPI, unemployment, fed funds rate, 10Y & 2Y treasury, yield curve spread

### Overview
- GET /v1/overview — Full market snapshot in ONE call (crypto + forex + fear/greed + global stats)

## Response Format
JSON. Every response includes `source` and `timestamp` fields. Technical indicators included where applicable.
"""


def _generate_llms_full_txt():
    return """# FinStack API — Full Documentation for AI Assistants

## Integration Example (Python)
```python
import requests
BASE = "https://your-finstack-url.com"
# Get Bitcoin price
r = requests.get(f"{BASE}/v1/crypto/prices", params={"coins": "bitcoin", "vs": "usd"})
print(r.json()["data"]["bitcoin"]["price"])
# Get market overview
r = requests.get(f"{BASE}/v1/overview")
data = r.json()
print(f"BTC: ${data['crypto']['bitcoin']['price_usd']}")
print(f"Fear/Greed: {data['fear_greed']['value']} ({data['fear_greed']['label']})")
```

## Integration Example (JavaScript)
```javascript
const BASE = "https://your-finstack-url.com";
const res = await fetch(`${BASE}/v1/crypto/prices?coins=bitcoin,ethereum&vs=usd`);
const { data } = await res.json();
console.log(`BTC: $${data.bitcoin.price}`);
```

## All Endpoints

### GET /v1/crypto/prices
Params: coins (string, comma-separated CoinGecko IDs), vs (string, currency code)
Returns: { data: { [coin]: { price, change_24h_pct, market_cap, volume_24h } } }

### GET /v1/crypto/history
Params: coin (string), vs (string), days (int, 1-365)
Returns: { current_price, period_high, period_low, price_change_pct, indicators: { sma_7, sma_14, sma_30, sma_50, sma_200, ema_12, ema_26, rsi_14, volatility_14d, bollinger_20, macd }, prices: [...] }

### GET /v1/crypto/coins
Params: q (string, search query)
Returns: { coins: [{ id, symbol, name }] }

### GET /v1/crypto/trending
Returns: { trending: [{ id, name, symbol, market_cap_rank, price_btc }] }

### GET /v1/crypto/fear-greed
Params: days (int, 1-90)
Returns: { current: { value, label }, history: [...] }

### GET /v1/crypto/defi
Params: limit (int, 1-100)
Returns: { protocols: [{ name, symbol, tvl, chain, category, change_1d, change_7d, mcap_tvl }] }

### GET /v1/crypto/defi/chains
Returns: { chains: [{ name, tvl, gecko_id, token_symbol }] }

### GET /v1/crypto/global
Returns: { total_market_cap_usd, btc_dominance, eth_dominance, active_coins, market_cap_change_24h_pct }

### GET /v1/forex/rates
Params: base (string, default USD), targets (string, comma-separated)
Returns: { base, rates: { [currency]: rate } }

### GET /v1/forex/convert
Params: from (string), to (string), amount (number)
Returns: { from, to, amount, rate, converted }

### GET /v1/stocks/quote
Params: symbol (string, required)
Returns: { symbol, price, open, high, low, volume, prev_close, change, change_pct }

### GET /v1/stocks/history
Params: symbol (string, required), outputsize (compact|full)
Returns: { symbol, indicators: { sma_*, ema_*, rsi_14, bollinger_20, macd }, history: [{ date, open, high, low, close, volume }] }

### GET /v1/stocks/search
Params: q (string, required)
Returns: { results: [{ symbol, name, type, region, currency }] }

### GET /v1/macro/indicators
Returns: { indicators: { gdp_growth_pct, cpi_index, unemployment_rate, fed_funds_rate, treasury_10y_yield, treasury_2y_yield, inflation_rate_yoy, yield_curve_spread } }

### GET /v1/overview
Returns: { crypto: {...}, fear_greed: {...}, forex: {...}, global: {...} }
"""


def _generate_openapi():
    """Generate full OpenAPI 3.0 spec."""
    paths = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint in ("static", "root", "health", "llms_txt", "llms_full_txt",
                             "ai_plugin", "openapi_spec", "robots", "not_found", "server_error"):
            continue
        if "GET" not in rule.methods:
            continue

        view = app.view_functions.get(rule.endpoint)
        doc = (view.__doc__ or "").strip().split("\n")[0] if view else ""

        path_entry = {
            "get": {
                "operationId": rule.endpoint,
                "summary": doc,
                "responses": {"200": {"description": "Success"}},
            }
        }

        # Add params from the docstring if they exist
        if view and view.__doc__ and "Query params:" in view.__doc__:
            params = []
            for line in view.__doc__.split("\n"):
                line = line.strip()
                if line.startswith("-") or (": " in line and not line.startswith("Query")):
                    continue
            path_entry["get"]["parameters"] = params

        paths[rule.rule] = path_entry

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "FinStack API",
            "version": "1.0.0",
            "description": "Unified financial data API. Crypto prices + history with RSI/SMA/EMA/MACD/Bollinger, stocks, forex, DeFi TVL, Fear & Greed, macro indicators. One key, one format.",
        },
        "servers": [{"url": "/"}],
        "paths": paths,
    }


# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info(f"🚀 FinStack API starting on port {PORT}")
    log.info(f"📄 Docs: http://localhost:{PORT}/openapi.json")
    log.info(f"🤖 AI discovery: http://localhost:{PORT}/llms.txt")
    log.info(f"📊 Endpoints: 16 total across crypto/forex/stocks/macro")
    app.run(host="0.0.0.0", port=PORT, debug=(ENV == "development"))
