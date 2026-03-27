"""
FinStack API v1.0
Unified Financial Data â Crypto Â· Stocks Â· Forex Â· Macro
One API. One key. One format. Pre-computed indicators. AI-discoverable.

Aggregates: CoinGecko, DeFiLlama, Alpha Vantage, FRED, ExchangeRate API, Alternative.me
"""

import os
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

# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# CONFIG
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "demo")
FRED_KEY = os.environ.get("FRED_KEY", "")
PORT = int(os.environ.get("PORT", 8000))
ENV = os.environ.get("ENV", "development")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Rate limit config (per API key, per minute)
RATE_LIMITS = {
    "free": 10,       # 10 req/min = ~600/hour = ~100/day reasonable use
    "basic": 60,      # 60 req/min
    "pro": 300,       # 300 req/min
    "ultra": 1000,    # 1000 req/min
    "internal": 9999, # internal/testing
}

# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# APP SETUP
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

app = Flask(__name__)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("finstack")

# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# CACHE â in-memory, production would use Redis
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# RATE LIMITER â sliding window per API key
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MIDDLEWARE â auth, rate limit, CORS, request logging
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

# Paths that don't require auth
PUBLIC_PATHS = {"/", "/health", "/llms.txt", "/ai-plugin.json", "/openapi.json",
                "/llms-full.txt", "/robots.txt", "/favicon.ico"}


@app.before_request
def before_request_handler():
    g.start_time = time.time()

    # Skip auth for public paths
    if request.path in PUBLIC_PATHS:
        return

    # Extract API key from header or query param
    # RapidAPI sends it as X-RapidAPI-Proxy-Secret or the user sends X-API-Key
    api_key = (
        request.headers.get("X-RapidAPI-Proxy-Secret")
        or request.headers.get("X-API-Key")
        or request.args.get("apikey")
    )

    if not api_key:
        # Allow unauthenticated requests at free tier rate
        api_key = request.remote_addr or "anonymous"
        tier = "free"
    else:
        # In production, look up tier from database
        # For now, all authenticated requests get "basic" tier
        tier = "basic"

    g.api_key = api_key
    g.tier = tier

    allowed, remaining = check_rate_limit(api_key, tier)
    if not allowed:
        return jsonify({
            "error": "Rate limit exceeded",
            "limit": RATE_LIMITS[tier],
            "window": "60 seconds",
            "tier": tier,
            "upgrade": "Get higher limits at https://rapidapi.com/finstack/api/finstack",
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
        log.info(f"{request.method} {request.path} â {response.status_code} ({duration}ms)")

    return response


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint not found", "docs": "/openapi.json"}), 404


@app.errorhandler(500)
def server_error(e):
    log.error(f"Internal error: {e}")
    return jsonify({"error": "Internal server error"}), 500


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# UPSTREAM FETCHER
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# TECHNICAL INDICATORS
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# CRYPTO ENDPOINTS
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
    """Trending coins â most searched in the last 24h."""
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
    """Crypto Fear & Greed Index â current value and recent history."""
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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# FOREX ENDPOINTS
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# STOCKS ENDPOINTS
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.route("/v1/stocks/quote")
def stock_quote():
    """Real-time stock quote."""
    symbol = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required (e.g. ?symbol=AAPL)"}), 400

    k = ck("sq", symbol)
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch("https://www.alphavantage.co/query",
                params={"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": ALPHA_VANTAGE_KEY})
    if is_error(raw):
        return error_response(raw, "alphavantage")

    q = raw.get("Global Quote", {})
    if not q:
        note = raw.get("Note", raw.get("Information", ""))
        if "call frequency" in str(note).lower():
            return jsonify({"error": "Alpha Vantage rate limit hit. Free tier allows 25 requests/day. Upgrade your ALPHA_VANTAGE_KEY for more.", "source": "alphavantage"}), 429
        return jsonify({"error": f"No data for symbol '{symbol}'", "source": "alphavantage"}), 404

    try:
        result = {
            "symbol": q.get("01. symbol"),
            "price": float(q.get("05. price", 0)),
            "open": float(q.get("02. open", 0)),
            "high": float(q.get("03. high", 0)),
            "low": float(q.get("04. low", 0)),
            "volume": int(q.get("06. volume", 0)),
            "prev_close": float(q.get("08. previous close", 0)),
            "change": float(q.get("09. change", 0)),
            "change_pct": float(q.get("10. change percent", "0%").replace("%", "")),
            "latest_day": q.get("07. latest trading day"),
            "source": "alphavantage", "timestamp": now_iso(),
        }
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Data parsing error: {e}", "source": "alphavantage"}), 502

    cache_set(k, result, 60)
    return jsonify(result)


@app.route("/v1/stocks/history")
def stock_history():
    """Daily stock history with technical indicators."""
    symbol = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400
    size = request.args.get("outputsize", "compact")

    k = ck("sh", symbol, size)
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch("https://www.alphavantage.co/query", params={
        "function": "TIME_SERIES_DAILY", "symbol": symbol,
        "outputsize": size, "apikey": ALPHA_VANTAGE_KEY,
    })
    if is_error(raw):
        return error_response(raw, "alphavantage")

    ts = raw.get("Time Series (Daily)", {})
    if not ts:
        note = raw.get("Note", raw.get("Information", ""))
        if "call frequency" in str(note).lower():
            return jsonify({"error": "Alpha Vantage rate limit. Free tier: 25 req/day.", "source": "alphavantage"}), 429
        return jsonify({"error": f"No history for '{symbol}'", "source": "alphavantage"}), 404

    dates = sorted(ts.keys())
    closes = [float(ts[d]["4. close"]) for d in dates]

    history = [{
        "date": d,
        "open": float(ts[d]["1. open"]), "high": float(ts[d]["2. high"]),
        "low": float(ts[d]["3. low"]), "close": float(ts[d]["4. close"]),
        "volume": int(ts[d]["5. volume"]),
    } for d in dates[-60:]]

    result = {
        "symbol": symbol, "data_points": len(history),
        "indicators": indicators(closes),
        "history": history,
        "source": "alphavantage", "timestamp": now_iso(),
    }
    cache_set(k, result, 900)
    return jsonify(result)


@app.route("/v1/stocks/search")
def stock_search():
    """Search for stock symbols by name or keyword."""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "q parameter required (e.g. ?q=apple)"}), 400

    k = ck("ss", query)
    c = cache_get(k)
    if c:
        return jsonify(c)

    raw = fetch("https://www.alphavantage.co/query",
                params={"function": "SYMBOL_SEARCH", "keywords": query, "apikey": ALPHA_VANTAGE_KEY})
    if is_error(raw):
        return error_response(raw, "alphavantage")

    matches = [{
        "symbol": m.get("1. symbol"), "name": m.get("2. name"),
        "type": m.get("3. type"), "region": m.get("4. region"),
        "currency": m.get("8. currency"),
    } for m in raw.get("bestMatches", [])]

    result = {"query": query, "results": matches, "count": len(matches), "source": "alphavantage", "timestamp": now_iso()}
    cache_set(k, result, 3600)
    return jsonify(result)


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MACRO ENDPOINTS
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# OVERVIEW â THE KILLER ENDPOINT
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# AI DISCOVERABILITY
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# ROOT & HEALTH
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.route("/")
def root():
    return jsonify({
        "name": "FinStack API",
        "version": "1.0.0",
        "tagline": "Unified financial data â crypto, stocks, forex, macro. One API, one key, one format.",
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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# SPEC GENERATORS
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def _generate_llms_txt():
    return """# FinStack API
> Unified financial data API â crypto, stocks, forex, macro indicators. One key, one format, pre-computed technical indicators.

## Endpoints

### Crypto
- GET /v1/crypto/prices?coins=bitcoin,ethereum&vs=usd â Current prices, 24h change, market cap, volume
- GET /v1/crypto/history?coin=bitcoin&days=30 â Price history + RSI, SMA, EMA, MACD, Bollinger Bands
- GET /v1/crypto/coins?q=bit â Search available coins
- GET /v1/crypto/trending â Most searched coins in last 24h
- GET /v1/crypto/fear-greed?days=10 â Fear & Greed Index
- GET /v1/crypto/defi?limit=25 â Top DeFi protocols by TVL
- GET /v1/crypto/defi/chains â TVL by blockchain
- GET /v1/crypto/global â Total market cap, BTC dominance

### Forex
- GET /v1/forex/rates?base=USD&targets=EUR,GBP,NZD â Exchange rates (150+ currencies)
- GET /v1/forex/convert?from=USD&to=NZD&amount=100 â Convert between currencies

### Stocks
- GET /v1/stocks/quote?symbol=AAPL â Real-time quote
- GET /v1/stocks/history?symbol=AAPL â Daily OHLCV + RSI, SMA, EMA, MACD, Bollinger
- GET /v1/stocks/search?q=apple â Search symbols

### Macro
- GET /v1/macro/indicators â GDP, CPI, unemployment, fed funds rate, 10Y & 2Y treasury, yield curve spread

### Overview
- GET /v1/overview â Full market snapshot in ONE call (crypto + forex + fear/greed + global stats)

## Response Format
JSON. Every response includes `source` and `timestamp` fields. Technical indicators included where applicable.
"""


def _generate_llms_full_txt():
    return """# FinStack API â Full Documentation for AI Assistants

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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# RUN
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

if __name__ == "__main__":
    log.info(f"ð FinStack API starting on port {PORT}")
    log.info(f"ð Docs: http://localhost:{PORT}/openapi.json")
    log.info(f"ð¤ AI discovery: http://localhost:{PORT}/llms.txt")
    log.info(f"ð Endpoints: 16 total across crypto/forex/stocks/macro")
    app.run(host="0.0.0.0", port=PORT, debug=(ENV == "development"))
