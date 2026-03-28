# FinStack API

[![RapidAPI](https://img.shields.io/badge/RapidAPI-FinStack-blue?style=for-the-badge&logo=rapidapi)](https://rapidapi.com/kaysen213/api/finstack)
[![Free Tier](https://img.shields.io/badge/Free%20Tier-Available-green?style=for-the-badge)](https://rapidapi.com/kaysen213/api/finstack)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)

> **One API key. One format. All financial data.**

Stop juggling 6 different API keys for your financial app. FinStack aggregates crypto prices, DeFi TVL, forex rates, stock data, Fear & Greed Index, and macro indicators into a single unified REST API.

**[Subscribe on RapidAPI →](https://rapidapi.com/kaysen213/api/finstack)**

---

## Why FinStack?

| Before | After |
|--------|-------|
| CoinGecko API key | One key |
| Alpha Vantage API key | One format |
| DeFiLlama API key | One base URL |
| FRED API key | Pre-computed indicators |
| ExchangeRate API key | Free tier available |
| Alternative.me API key | |

---

## Quick Start

```python
import requests

headers = {
    "X-RapidAPI-Key": "YOUR_RAPIDAPI_KEY",
    "X-RapidAPI-Host": "finstack.p.rapidapi.com"
}

# Bitcoin price + RSI + MACD in one call
r = requests.get("https://finstack.p.rapidapi.com/v1/crypto/price/bitcoin", headers=headers)
print(r.json())
# {
#   "price_usd": 67432.18,
#   "change_24h_pct": 2.41,
#   "rsi_14": 61.3,
#   "macd": 412.5,
#   "sma_50": 63100.0
# }

# Fear & Greed Index
fg = requests.get("https://finstack.p.rapidapi.com/v1/fear-greed", headers=headers)
print(fg.json())  # {"value": 72, "label": "Greed"}

# Full market overview in one call
overview = requests.get("https://finstack.p.rapidapi.com/v1/overview", headers=headers)
```

```javascript
const axios = require('axios');

const headers = {
  'X-RapidAPI-Key': 'YOUR_RAPIDAPI_KEY',
  'X-RapidAPI-Host': 'finstack.p.rapidapi.com'
};
const base = 'https://finstack.p.rapidapi.com';

// Forex rates
const forex = await axios.get(`${base}/v1/forex/rates?base=USD`, { headers });

// Top DeFi protocols by TVL
const defi = await axios.get(`${base}/v1/defi/protocols`, { headers });

// Stock quote
const stock = await axios.get(`${base}/v1/stock/quote/AAPL`, { headers });
```

---

## All 15 Endpoints

### Crypto
| Endpoint | Description |
|----------|-------------|
| `GET /v1/crypto/price/{coin_id}` | Live price, 24h change, market cap + RSI, SMA, MACD |
| `GET /v1/crypto/history/{coin_id}` | Full OHLCV + technical indicators |
| `GET /v1/crypto/top` | Top 100 coins by market cap |
| `GET /v1/crypto/search` | Search by name or ticker |

### DeFi
| Endpoint | Description |
|----------|-------------|
| `GET /v1/defi/protocols` | Top protocols ranked by TVL |
| `GET /v1/defi/tvl/{protocol}` | TVL history for any protocol |
| `GET /v1/defi/chains` | TVL breakdown by blockchain |
| `GET /v1/defi/stats` | Global DeFi stats + BTC dominance |

### Stocks & Forex
| Endpoint | Description |
|----------|-------------|
| `GET /v1/stock/quote/{symbol}` | Live stock quote |
| `GET /v1/stock/history/{symbol}` | Daily OHLCV history |
| `GET /v1/forex/rates` | Live rates for any base currency |

### Sentiment & Macro
| Endpoint | Description |
|----------|-------------|
| `GET /v1/fear-greed` | Current + historical Fear & Greed Index |
| `GET /v1/macro/{series_id}` | Any FRED series — GDP, CPI, Fed Funds Rate |

### Combined
| Endpoint | Description |
|----------|-------------|
| `GET /v1/crypto/global` | Global crypto market stats |
| `GET /v1/overview` | Full snapshot: crypto + Fear & Greed + forex |

---

## Pricing

| Plan | Price | Requests/month |
|------|-------|----------------|
| **BASIC** | Free | 500 |
| **PRO** ⭐ | $15/mo | 500,000 |
| **ULTRA** | $50/mo | 500,000 |
| **MEGA** | $150/mo | 500,000 |

[**Get your free API key on RapidAPI**](https://rapidapi.com/kaysen213/api/finstack)

---

## Use Cases

- **Financial dashboards** — one request returns crypto + macro + sentiment
- **Trading bots** — OHLCV + RSI + MACD pre-computed, no TA libraries needed
- **Mobile apps** — single /v1/overview endpoint for market screens
- **Quant research** — historical data + technicals via one call
- **Student projects** — free tier, one API key, no setup

---

## Self-Hosting

```bash
git clone https://github.com/kaysen213/finstack-api
cd finstack-api
cp .env.example .env
# Add your data source API keys to .env
pip install -r requirements.txt
python app.py
```

---

## Data Sources

| Source | Data |
|--------|------|
| [CoinGecko](https://coingecko.com) | Crypto prices, history, market data |
| [DeFiLlama](https://defillama.com) | DeFi TVL, protocols, chains |
| [Alpha Vantage](https://alphavantage.co) | Stocks, forex |
| [FRED](https://fred.stlouisfed.org) | Macro economic indicators |
| [ExchangeRate-API](https://exchangerate-api.com) | Live forex rates |
| [Alternative.me](https://alternative.me) | Fear & Greed Index |

---

Built with love — [Get started free on RapidAPI](https://rapidapi.com/kaysen213/api/finstack)
