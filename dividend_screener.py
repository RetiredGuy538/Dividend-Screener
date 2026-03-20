#!/usr/bin/env python3
"""
Dividend Panic Screener
Flags high-quality dividend stocks/ETFs that have been oversold due to panic/sentiment.
Generates a self-contained HTML dashboard + sends Gmail alerts.
"""

import os
import json
import math
import smtplib
import datetime
import argparse
import requests
import yfinance as yf
import yaml
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

# ── Data Fetching ─────────────────────────────────────────────────────────────

def fetch_yfinance(ticker: str) -> dict:
    """Fetch price data, yield, moving average from yfinance."""
    try:
        t = yf.Ticker(ticker)
        info = t.info
        hist = t.history(period="1y")

        if hist.empty:
            return {}

        current_price = hist["Close"].iloc[-1]
        high_52w = hist["Close"].max()
        ma_50 = hist["Close"].tail(50).mean()
        ma_200 = hist["Close"].tail(200).mean() if len(hist) >= 200 else None

        drop_from_52w = ((current_price - high_52w) / high_52w) * 100 if high_52w else 0
        drop_from_ma50 = ((current_price - ma_50) / ma_50) * 100 if ma_50 else 0

        div_yield = info.get("dividendYield") or info.get("yield") or 0
        if div_yield:
            if div_yield < 1:
                div_yield *= 100  # convert decimal (0.0292) to percentage (2.92%)

        forward_pe = info.get("forwardPE") or info.get("trailingPE") or None
        trailing_pe = info.get("trailingPE") or None
        market_cap = info.get("marketCap") or None
        name = info.get("longName") or info.get("shortName") or ticker
        sector = info.get("sector") or info.get("category") or "N/A"
        asset_type = "ETF" if info.get("quoteType") == "ETF" else "Stock"

        return {
            "ticker": ticker,
            "name": name,
            "sector": sector,
            "asset_type": asset_type,
            "current_price": round(current_price, 2),
            "high_52w": round(high_52w, 2),
            "ma_50": round(ma_50, 2),
            "ma_200": round(ma_200, 2) if ma_200 else None,
            "drop_from_52w": round(drop_from_52w, 2),
            "drop_from_ma50": round(drop_from_ma50, 2),
            "div_yield": round(div_yield, 2),
            "forward_pe": round(forward_pe, 2) if forward_pe else None,
            "trailing_pe": round(trailing_pe, 2) if trailing_pe else None,
            "market_cap": market_cap,
        }
    except Exception as e:
        print(f"  [yfinance] Error for {ticker}: {e}")
        return {}


def fetch_dividend_streak(ticker: str) -> dict:
    """
    Compute dividend growth/hold streak from yfinance.
    Streak counts consecutive years where the annual dividend was
    equal to or greater than the prior year (no cuts allowed).
    Also returns payout ratio.
    """
    try:
        t = yf.Ticker(ticker)

        # Get full dividend history
        divs = t.dividends
        if divs.empty:
            return {"dividend_streak": 0, "avg_payout_ratio": None}

        # Sum dividends by calendar year
        divs.index = divs.index.tz_localize(None) if divs.index.tzinfo else divs.index
        annual = divs.groupby(divs.index.year).sum()

        if len(annual) < 2:
            return {"dividend_streak": 1 if len(annual) == 1 else 0, "avg_payout_ratio": None}

        # Walk backwards from most recent full year counting unbroken equal-or-growth streak
        current_year = datetime.date.today().year
        years = sorted(annual.index.tolist(), reverse=True)

        # Skip current partial year if we're not in December
        if datetime.date.today().month < 12 and years and years[0] == current_year:
            years = years[1:]

        streak = 0
        for i in range(len(years) - 1):
            this_year = years[i]
            prev_year = years[i + 1]

            # Ensure years are consecutive (no gap)
            if this_year - prev_year != 1:
                break

            this_div = annual.get(this_year, 0)
            prev_div = annual.get(prev_year, 0)

            if prev_div <= 0:
                break

            # Allow up to 2% tolerance for rounding/timing differences
            if this_div >= prev_div * 0.98:
                streak += 1
            else:
                break  # dividend was cut — stop streak

        # Payout ratio from yfinance info
        info = t.info
        payout = info.get("payoutRatio") or None
        if payout and payout < 10:  # decimal like 0.45
            payout = round(payout * 100, 1)
        elif payout:
            payout = round(payout, 1)

        return {
            "dividend_streak": streak,
            "avg_payout_ratio": payout,
        }
    except Exception as e:
        print(f"  [dividend_streak] Error for {ticker}: {e}")
        return {"dividend_streak": 0, "avg_payout_ratio": None}


def fetch_historical_pe(ticker: str) -> dict:
    """Compute avg historical P/E from yfinance earnings + price history. No API key needed."""
    try:
        t = yf.Ticker(ticker)

        # yfinance earnings history — quarterly EPS rolled up to annual
        earnings = t.income_stmt  # annual income statement
        price_hist = t.history(period="10y", interval="1mo")

        if earnings is None or earnings.empty or price_hist.empty:
            return {}

        pe_vals = []

        # earnings columns are fiscal year-end dates
        for col in earnings.columns[:8]:  # up to 8 years back
            try:
                # Get diluted EPS or net income / shares
                eps = None
                if "Diluted EPS" in earnings.index:
                    eps = float(earnings.loc["Diluted EPS", col])
                elif "Basic EPS" in earnings.index:
                    eps = float(earnings.loc["Basic EPS", col])

                if not eps or eps <= 0:
                    continue

                # Find stock price closest to that fiscal year end date
                target = col.to_pydatetime().replace(tzinfo=None)
                idx = price_hist.index.tz_localize(None) if price_hist.index.tzinfo else price_hist.index
                window_mask = (
                    (idx >= target - datetime.timedelta(days=45)) &
                    (idx <= target + datetime.timedelta(days=45))
                )
                window = price_hist[window_mask]
                if window.empty:
                    continue

                price_at_date = float(window["Close"].iloc[-1])
                pe = price_at_date / eps
                if 3 < pe < 200:  # filter nonsensical values
                    pe_vals.append(round(pe, 2))
            except Exception:
                continue

        if not pe_vals:
            return {}

        avg_pe = sum(pe_vals) / len(pe_vals)
        return {"avg_historical_pe": round(avg_pe, 2)}

    except Exception as e:
        print(f"  [historical_pe] Error for {ticker}: {e}")
        return {}


def fetch_analyst_rating(ticker: str) -> dict:
    """
    Pull analyst consensus rating from yfinance — no API key or scraping needed.
    Uses the recommendations_summary which gives Buy/Hold/Sell counts.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info

        # yfinance provides recommendationMean (1=Strong Buy, 5=Strong Sell)
        # and recommendationKey (e.g. "buy", "hold", "sell")
        rec_mean = info.get("recommendationMean")
        rec_key  = info.get("recommendationKey") or ""
        num_analysts = info.get("numberOfAnalystOpinions") or 0
        target_price = info.get("targetMeanPrice")

        if rec_mean is None:
            return {
                "sentiment_label": "— N/A",
                "sentiment_score": 50,
                "news_headlines": [],
                "analyst_recommendation": "No analyst coverage"
            }

        # Map mean score to label
        if rec_mean <= 1.5:
            label = "Strong Buy 🟢"
            score = 15
        elif rec_mean <= 2.5:
            label = "Buy 🟢"
            score = 30
        elif rec_mean <= 3.5:
            label = "Hold 🟡"
            score = 50
        elif rec_mean <= 4.5:
            label = "Sell 🔴"
            score = 70
        else:
            label = "Strong Sell 🔴"
            score = 85

        rec_str = f"{rec_mean:.1f}/5.0"
        if num_analysts:
            rec_str += f" ({num_analysts} analysts)"
        if target_price:
            rec_str += f" · Target ${target_price:.2f}"

        return {
            "sentiment_label": label,
            "sentiment_score": score,
            "news_headlines": [],
            "analyst_recommendation": rec_str,
        }
    except Exception as e:
        print(f"  [analyst_rating] Error for {ticker}: {e}")
        return {
            "sentiment_label": "— N/A",
            "sentiment_score": 50,
            "news_headlines": [],
            "analyst_recommendation": "—"
        }


def fetch_news_sentiment(sector: str, ticker: str, api_key: str) -> dict:
    """Fetch recent news and compute a simple sentiment score."""
    if not api_key or api_key == "YOUR_NEWSAPI_KEY":
        return {"sentiment_score": 50, "news_headlines": [], "sentiment_label": "— no key"}
    try:
        query = f"{ticker} stock"
        url = (
            f"https://newsapi.org/v2/everything?q={query}"
            f"&sortBy=publishedAt&pageSize=10&language=en&apiKey={api_key}"
        )
        resp = requests.get(url, timeout=10).json()
        articles = resp.get("articles", [])

        negative_words = [
            r"\bcrash\b", r"\bplunge\b", r"\bplunges\b", r"\bdecline\b", r"\bdeclines\b",
            r"\bslump\b", r"\bslumps\b", r"\bfear\b", r"\bpanic\b", r"\brecession\b",
            r"\bdowngrade\b", r"\bdowngrades\b", r"\bwarning\b", r"\bthreat\b",
            r"\bdisappoint\b", r"\bdisappoints\b", r"\bdisappointing\b",
            r"\bmiss\b", r"\bmisses\b", r"\bloss\b", r"\blosses\b",
            r"\bcuts dividend\b", r"\bdividend cut\b", r"\bsells off\b",
            r"\bsell.?off\b", r"\bweakness\b", r"\bweak results\b",
        ]
        positive_words = [
            r"\bgain\b", r"\bgains\b", r"\bbeat\b", r"\bbeats\b",
            r"\bupgrade\b", r"\bupgrades\b", r"\brally\b", r"\brallies\b",
            r"\bsurge\b", r"\bsurges\b", r"\bprofit\b", r"\bprofits\b",
            r"\boutperform\b", r"\boutperforms\b", r"\bdividend increase\b",
            r"\braises dividend\b", r"\bstrong earnings\b", r"\brecord earnings\b",
            r"\brecord profit\b", r"\brecord revenue\b",
        ]

        import re
        neg_count = 0
        pos_count = 0
        headlines = []

        for a in articles[:10]:
            title = (a.get("title") or "").lower()
            headlines.append(a.get("title", ""))
            neg_count += sum(1 for w in negative_words if re.search(w, title))
            pos_count += sum(1 for w in positive_words if re.search(w, title))

        total = neg_count + pos_count
        if total == 0:
            sentiment_score = 50
            label = "Neutral"
        else:
            # High negative sentiment = LOW score = higher opportunity
            sentiment_score = int((pos_count / total) * 100)
            if sentiment_score < 30:
                label = "Bearish 📉"
            elif sentiment_score < 60:
                label = "Mixed"
            else:
                label = "Bullish 📈"

        return {
            "sentiment_score": sentiment_score,
            "sentiment_label": label,
            "news_headlines": headlines[:5],
        }
    except Exception as e:
        print(f"  [NewsAPI] Error for {ticker}: {e}")
        return {"sentiment_score": 50, "news_headlines": [], "sentiment_label": "Neutral"}


# ── Opportunity Score ──────────────────────────────────────────────────────────

def compute_opportunity_score(data: dict, thresholds: dict) -> dict:
    """
    Composite score 0–100. Higher = better buying opportunity.
    Weights:
      25% — drop from 52-week high
      20% — drop from 50-day MA
      20% — yield vs historical (proxy: current yield elevated)
      20% — forward PE vs historical PE
      15% — news sentiment (bearish news = opportunity)
    """
    scores = {}
    weights = {
        "drop_52w": 0.25,
        "drop_ma50": 0.20,
        "yield_signal": 0.20,
        "pe_signal": 0.20,
        "sentiment": 0.15,
    }

    # 1. Drop from 52-week high (negative = below high = opportunity)
    drop_52w = abs(min(data.get("drop_from_52w", 0), 0))  # only count drops, not gains
    ticker_threshold = thresholds.get("drop_threshold_pct", 10)
    scores["drop_52w"] = min(100, (drop_52w / ticker_threshold) * 100)

    # 2. Drop from 50-day MA (negative = below MA = opportunity)
    drop_ma = abs(min(data.get("drop_from_ma50", 0), 0))
    scores["drop_ma50"] = min(100, (drop_ma / 8) * 100)

    # 3. Yield signal — if yield >= min_yield, scale up; cap at 150% of min
    min_yield = thresholds.get("min_yield_pct", 3.0)
    current_yield = data.get("div_yield", 0)
    if current_yield >= min_yield:
        scores["yield_signal"] = min(100, ((current_yield - min_yield) / min_yield) * 100 + 50)
    else:
        scores["yield_signal"] = max(0, (current_yield / min_yield) * 50)

    # 4. PE signal — forward PE below historical average
    fwd_pe = data.get("forward_pe")
    hist_pe = data.get("avg_historical_pe")
    if fwd_pe and hist_pe and hist_pe > 0:
        ratio = fwd_pe / hist_pe
        if ratio < 1:
            scores["pe_signal"] = min(100, (1 - ratio) * 200)
        else:
            scores["pe_signal"] = max(0, 100 - (ratio - 1) * 100)
    else:
        scores["pe_signal"] = 50  # neutral if no data

    # 5. News sentiment — lower sentiment = higher opportunity
    sentiment = data.get("sentiment_score", 50)
    scores["sentiment"] = 100 - sentiment  # invert: bearish news = higher score

    # Weighted composite
    composite = sum(scores[k] * weights[k] for k in weights)

    # Opportunity tier
    if composite >= 70:
        tier = "Strong Buy"
        tier_class = "tier-strong"
    elif composite >= 50:
        tier = "Watch List"
        tier_class = "tier-watch"
    elif composite >= 30:
        tier = "⏳ Hold"
        tier_class = "tier-hold"
    else:
        tier = "Fairly Valued"
        tier_class = "tier-fair"

    return {
        **data,
        "opportunity_score": round(composite, 1),
        "score_components": {k: round(v, 1) for k, v in scores.items()},
        "tier": tier,
        "tier_class": tier_class,
    }


# ── Broad Universe Screening ───────────────────────────────────────────────────

BROAD_UNIVERSE = [
    # High-dividend ETFs
    "VYM", "HDV", "SCHD", "DVY", "SDY", "DGRO", "VIG", "NOBL",
    "SPYD", "FVD", "IDV", "PFF", "PFFD",
    # REITs (high yield stocks)
    "O", "MAIN", "STAG", "NNN", "WPC", "VICI", "AMT", "CCI",
    # Dividend aristocrats / quality stocks
    "JNJ", "KO", "PG", "MMM", "T", "VZ", "MO", "PM", "XOM", "CVX",
    "ABT", "EMR", "ITW", "GPC", "CLX", "SYY", "ADP", "TGT", "WMT",
    # BDCs & CEF proxies
    "ARCC", "MAIN", "FS", "GBDC",
    # Utilities
    "NEE", "D", "SO", "DUK", "AEP", "XEL", "WEC", "ES",
]


def screen_universe(config: dict) -> list:
    """Screen a broad universe for tickers above the min yield threshold."""
    min_yield = config["screening"]["min_yield_pct"]
    print(f"\n📡 Screening broad universe for yield >= {min_yield}%...")
    qualified = []
    for ticker in BROAD_UNIVERSE:
        try:
            t = yf.Ticker(ticker)
            info = t.info
            dy = info.get("dividendYield") or info.get("yield") or 0
            if dy:
                dy *= 100
            if dy >= min_yield:
                qualified.append(ticker)
                print(f"  ✓ {ticker}: {dy:.1f}%")
        except Exception:
            pass
    return qualified


# ── HTML Generation ───────────────────────────────────────────────────────────

def generate_html(results: list, config: dict, generated_at: str) -> str:
    results_json = json.dumps(results, indent=2)
    min_yield = config["screening"]["min_yield_pct"]
    min_streak = config["screening"]["min_dividend_streak_years"]
    score_threshold = config["alerts"]["score_threshold"]

    rows = ""
    for r in results:
        score = r.get("opportunity_score", 0)
        bar_color = "#4ade80" if score >= 70 else "#f59e0b" if score >= 50 else "#6b7280"
        rows += f"""
        <tr class="data-row" data-ticker="{r['ticker']}" onclick="toggleDetail('{r['ticker']}')">
          <td class="ticker-cell">
            <span class="ticker-tag">{r['ticker']}</span>
            <span class="asset-badge asset-{r.get('asset_type','Stock').lower()}">{r.get('asset_type','')}</span>
          </td>
          <td class="name-cell">{r.get('name', r['ticker'])[:32]}</td>
          <td>{r.get('sector','N/A')[:20]}</td>
          <td class="score-cell">
            <div class="score-bar-wrap">
              <div class="score-bar" style="width:{min(score,100)}%; background:{bar_color}"></div>
              <span class="score-num">{score}</span>
            </div>
          </td>
          <td><span class="tier-badge {r.get('tier_class','')}">{r.get('tier','')}</span></td>
          <td class="num">{r.get('div_yield', 'N/A')}%</td>
          <td class="num">{r.get('dividend_streak', '—')}{' yrs' if r.get('dividend_streak') else ''}</td>
          <td class="num">{f"{r['drop_from_52w']:.1f}%" if isinstance(r.get('drop_from_52w'), (int,float)) else '—'}</td>
          <td class="num">{f"{r['drop_from_ma50']:.1f}%" if isinstance(r.get('drop_from_ma50'), (int,float)) else '—'}</td>
          <td class="num">{r.get('forward_pe', '—')}</td>
          <td class="num">{r.get('avg_historical_pe', '—')}</td>
          <td>{r.get('sentiment_label', '—')}</td>
        </tr>
        <tr class="detail-row" id="detail-{r['ticker']}" style="display:none">
          <td colspan="12">
            <div class="detail-panel">
              <div class="detail-grid">
                <div class="detail-block">
                  <h4>📊 Score Breakdown</h4>
                  {build_score_breakdown(r)}
                </div>
                <div class="detail-block">
                  <h4>💰 Dividend Details</h4>
                  <p>Current Yield: <strong>{r.get('div_yield','—')}%</strong></p>
                  <p>Growth/Hold Streak: <strong>{r.get('dividend_streak','—')} yrs</strong> (no cuts)</p>
                  <p>Payout Ratio: <strong>{r.get('avg_payout_ratio','—')}%</strong></p>
                </div>
                <div class="detail-block">
                  <h4>📈 Valuation</h4>
                  <p>Forward P/E: <strong>{r.get('forward_pe','—')}</strong></p>
                  <p>Trailing P/E: <strong>{r.get('trailing_pe','—')}</strong></p>
                  <p>Avg Historical P/E: <strong>{r.get('avg_historical_pe','—')}</strong></p>
                </div>
                <div class="detail-block">
                  <h4>📉 Price Action</h4>
                  <p>Current: <strong>${r.get('current_price','—')}</strong></p>
                  <p>52-Week High: <strong>${r.get('high_52w','—')}</strong></p>
                  <p>50-Day MA: <strong>${r.get('ma_50','—')}</strong></p>
                  <p>200-Day MA: <strong>${r.get('ma_200','—')}</strong></p>
                </div>
                <div class="detail-block news-block">
                  <h4>📊 Analyst Rating</h4>
                  <p>Consensus: <strong>{r.get('sentiment_label','—')}</strong></p>
                  <p>Recommendation: <strong>{r.get('analyst_recommendation','—')}</strong></p>
                  {"".join(f'<p class="headline">• {h}</p>' for h in r.get('news_headlines', []))}
                </div>
              </div>
            </div>
          </td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dividend Panic Screener</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Libre+Baskerville:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0d1117;
    --surface: #161b22;
    --surface2: #1c2128;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #f0b429;
    --red: #ef4444;
    --green: #22c55e;
    --amber: #f59e0b;
    --blue: #3b82f6;
    --serif: 'Libre Baskerville', Georgia, serif;
    --mono: 'DM Mono', monospace;
    --sans: 'DM Sans', sans-serif;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    min-height: 100vh;
  }}

  /* Header */
  .header {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 28px 40px 24px;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 20px;
  }}
  .header-left h1 {{
    font-family: var(--serif);
    font-size: 28px;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: -0.5px;
  }}
  .header-left h1 em {{
    font-style: italic;
    color: #fff;
  }}
  .header-left p {{
    font-size: 13px;
    color: var(--muted);
    margin-top: 4px;
    font-family: var(--mono);
  }}
  .header-stats {{
    display: flex;
    gap: 24px;
    align-items: flex-end;
  }}
  .stat-pill {{
    text-align: right;
  }}
  .stat-pill .num {{
    font-family: var(--mono);
    font-size: 22px;
    font-weight: 500;
    color: var(--text);
    display: block;
  }}
  .stat-pill .label {{
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }}

  /* Controls */
  .controls {{
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
    padding: 14px 40px;
    display: flex;
    gap: 16px;
    align-items: center;
    flex-wrap: wrap;
  }}
  .controls label {{
    font-size: 12px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-right: 4px;
  }}
  .controls select, .controls input {{
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 10px;
    border-radius: 6px;
    font-family: var(--mono);
    font-size: 13px;
    cursor: pointer;
  }}
  .controls select:focus, .controls input:focus {{
    outline: none;
    border-color: var(--accent);
  }}
  .divider {{ width: 1px; height: 24px; background: var(--border); }}

  /* Table wrapper */
  .table-wrap {{
    padding: 24px 40px;
    overflow-x: auto;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}
  thead tr {{
    border-bottom: 2px solid var(--border);
  }}
  th {{
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    padding: 10px 12px;
    text-align: left;
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
  }}
  th:hover {{ color: var(--text); }}
  th.sorted {{ color: var(--accent); }}

  .data-row {{
    border-bottom: 1px solid #1e2530;
    cursor: pointer;
    transition: background 0.15s;
  }}
  .data-row:hover {{ background: #1a2030; }}
  td {{
    padding: 11px 12px;
    vertical-align: middle;
  }}
  .num {{ font-family: var(--mono); text-align: right; }}
  .drop-high {{ color: var(--red); font-weight: 500; }}

  .ticker-cell {{ display: flex; align-items: center; gap: 8px; }}
  .ticker-tag {{
    font-family: var(--mono);
    font-weight: 500;
    font-size: 14px;
    color: var(--accent);
  }}
  .asset-badge {{
    font-size: 10px;
    padding: 2px 6px;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
  }}
  .asset-etf {{ background: #1e3a5f; color: #60a5fa; }}
  .asset-stock {{ background: #1e3f2a; color: #4ade80; }}

  /* Score bar */
  .score-bar-wrap {{
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 120px;
  }}
  .score-bar {{
    height: 6px;
    border-radius: 3px;
    transition: width 0.3s ease;
    flex-shrink: 0;
  }}
  .score-num {{
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 500;
    min-width: 32px;
  }}

  /* Tier badges */
  .tier-badge {{
    font-size: 12px;
    padding: 3px 9px;
    border-radius: 20px;
    font-weight: 500;
    white-space: nowrap;
  }}
  .tier-strong {{ background: #14291e; color: #4ade80; border: 1px solid #166534; }}
  .tier-watch  {{ background: #3f2d00; color: #fbbf24; border: 1px solid #78350f; }}
  .tier-hold   {{ background: #1e293b; color: #94a3b8; border: 1px solid #334155; }}
  .tier-fair   {{ background: #3f1515; color: #f87171; border: 1px solid #7f1d1d; }}

  /* Detail panel */
  .detail-row td {{ padding: 0; }}
  .detail-panel {{
    background: #111827;
    border-left: 3px solid var(--accent);
    padding: 20px 24px;
    margin: 4px 0 8px;
  }}
  .detail-grid {{
    display: flex;
    flex-wrap: wrap;
    gap: 20px;
  }}
  .detail-block {{
    min-width: 180px;
    flex: 1;
  }}
  .detail-block h4 {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 10px;
    font-family: var(--mono);
  }}
  .detail-block p {{
    font-size: 13px;
    margin-bottom: 5px;
    color: #cbd5e1;
  }}
  .detail-block strong {{ color: var(--text); }}
  .news-block {{ flex: 2; min-width: 280px; }}
  .headline {{
    font-size: 12px;
    color: var(--muted);
    border-left: 2px solid var(--border);
    padding-left: 8px;
    margin-bottom: 4px;
    line-height: 1.4;
  }}

  /* Score breakdown bars */
  .breakdown-item {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    font-size: 12px;
    font-family: var(--mono);
  }}
  .breakdown-label {{ min-width: 100px; color: var(--muted); }}
  .breakdown-bar-wrap {{ flex: 1; background: #1e293b; border-radius: 3px; height: 5px; }}
  .breakdown-bar {{ height: 5px; border-radius: 3px; background: var(--accent); }}
  .breakdown-val {{ min-width: 30px; text-align: right; color: var(--text); }}

  /* Config info bar */
  .config-bar {{
    background: #0d1117;
    border-top: 1px solid var(--border);
    padding: 10px 40px;
    font-family: var(--mono);
    font-size: 11px;
    color: #4b5563;
    display: flex;
    gap: 24px;
    flex-wrap: wrap;
  }}
  .config-bar span {{ color: #6b7280; }}
  .config-bar strong {{ color: #9ca3af; }}

  /* Empty state */
  .empty {{ text-align: center; padding: 60px; color: var(--muted); }}
  .empty h3 {{ font-family: var(--serif); font-size: 20px; margin-bottom: 8px; }}

  @media (max-width: 768px) {{
    .header {{ padding: 20px; flex-direction: column; align-items: flex-start; }}
    .controls {{ padding: 12px 20px; }}
    .table-wrap {{ padding: 16px 20px; }}
    .config-bar {{ padding: 10px 20px; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h1>Dividend <em>Panic</em> Screener</h1>
    <p>Updated {generated_at} · Flags oversold high-yield investments</p>
  </div>
  <div class="header-stats">
    <div class="stat-pill">
      <span class="num" id="stat-screened">0</span>
      <span class="label">Screened</span>
    </div>
    <div class="stat-pill">
      <span class="num" id="stat-signals">0</span>
      <span class="label">Signals</span>
    </div>
    <div class="stat-pill">
      <span class="num" id="stat-watching">0</span>
      <span class="label">Watch List</span>
    </div>
  </div>
</div>

<div class="controls">
  <label>Min Score</label>
  <input type="number" id="filter-score" value="0" min="0" max="100" style="width:70px" oninput="applyFilters()">
  <div class="divider"></div>
  <label>Min Yield</label>
  <input type="number" id="filter-yield" value="{min_yield}" min="0" max="20" step="0.5" style="width:70px" oninput="applyFilters()">%
  <div class="divider"></div>
  <label>Min Streak</label>
  <input type="number" id="filter-streak" value="{min_streak}" min="0" max="50" style="width:60px" oninput="applyFilters()">
  <div class="divider"></div>
  <label>Type</label>
  <select id="filter-type" onchange="applyFilters()">
    <option value="">All</option>
    <option value="Stock">Stocks</option>
    <option value="ETF">ETFs</option>
  </select>
  <div class="divider"></div>
  <label>Sort</label>
  <select id="sort-col" onchange="applyFilters()">
    <option value="opportunity_score">Opportunity Score</option>
    <option value="div_yield">Dividend Yield</option>
    <option value="drop_from_52w">Drop from 52W High</option>
    <option value="dividend_streak">Dividend Streak</option>
  </select>
</div>

<div class="table-wrap">
  <table id="main-table">
    <thead>
      <tr>
        <th>Ticker</th>
        <th>Name</th>
        <th>Sector</th>
        <th>Opp. Score ▼</th>
        <th>Signal</th>
        <th class="num">Yield</th>
        <th class="num">Growth Streak</th>
        <th class="num">vs 52W Hi</th>
        <th class="num">vs MA50</th>
        <th class="num">Fwd P/E</th>
        <th class="num">Hist P/E</th>
        <th>Analyst Rating</th>
      </tr>
    </thead>
    <tbody id="table-body">
      {rows}
    </tbody>
  </table>
  <div class="empty" id="empty-state" style="display:none">
    <h3>No results match your filters</h3>
    <p>Try lowering the minimum score or yield threshold.</p>
  </div>
</div>

<div class="config-bar">
  <span>Config: <strong>Min Yield {min_yield}%</strong></span>
  <span>Alert Threshold: <strong>Score ≥ {score_threshold}</strong></span>
  <span>Min Streak: <strong>{min_streak} years</strong></span>
  <span>Data: <strong>yfinance + NewsAPI + Finviz</strong></span>
</div>

<script>
const ALL_DATA = {results_json};

function toggleDetail(ticker) {{
  const row = document.getElementById('detail-' + ticker);
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}}

function applyFilters() {{
  const minScore = parseFloat(document.getElementById('filter-score').value) || 0;
  const minYield = parseFloat(document.getElementById('filter-yield').value) || 0;
  const minStreak = parseInt(document.getElementById('filter-streak').value) || 0;
  const typeFilter = document.getElementById('filter-type').value;
  const sortCol = document.getElementById('sort-col').value;

  let filtered = ALL_DATA.filter(r => {{
    if ((r.opportunity_score || 0) < minScore) return false;
    if ((r.div_yield || 0) < minYield) return false;
    if ((r.dividend_streak || 0) < minStreak) return false;
    if (typeFilter && r.asset_type !== typeFilter) return false;
    return true;
  }});

  filtered.sort((a, b) => (b[sortCol] || 0) - (a[sortCol] || 0));

  const tbody = document.getElementById('table-body');
  const rows = tbody.querySelectorAll('.data-row, .detail-row');
  rows.forEach(r => r.style.display = 'none');

  filtered.forEach(r => {{
    const dataRow = tbody.querySelector(`tr[data-ticker="${{r.ticker}}"]`);
    if (dataRow) dataRow.style.display = '';
  }});

  document.getElementById('empty-state').style.display = filtered.length === 0 ? 'block' : 'none';

  // Update stats
  const signals = filtered.filter(r => (r.opportunity_score || 0) >= 70).length;
  const watching = filtered.filter(r => (r.opportunity_score || 0) >= 50 && (r.opportunity_score || 0) < 70).length;
  document.getElementById('stat-screened').textContent = filtered.length;
  document.getElementById('stat-signals').textContent = signals;
  document.getElementById('stat-watching').textContent = watching;
}}

// Initialize stats on load
applyFilters();
</script>
</body>
</html>"""


def build_score_breakdown(r: dict) -> str:
    components = r.get("score_components", {})
    labels = {
        "drop_52w": "vs 52W High",
        "drop_ma50": "vs MA-50",
        "yield_signal": "Yield Signal",
        "pe_signal": "P/E Signal",
        "sentiment": "Sentiment",
    }
    html = ""
    for key, label in labels.items():
        val = components.get(key, 0)
        html += f"""
        <div class="breakdown-item">
          <span class="breakdown-label">{label}</span>
          <div class="breakdown-bar-wrap">
            <div class="breakdown-bar" style="width:{min(val,100)}%"></div>
          </div>
          <span class="breakdown-val">{val:.0f}</span>
        </div>"""
    return html


# ── Email Alert ────────────────────────────────────────────────────────────────

def send_email_alert(results: list, config: dict):
    alert_cfg = config.get("alerts", {})
    threshold = alert_cfg.get("score_threshold", 65)
    alerts = [r for r in results if r.get("opportunity_score", 0) >= threshold]

    if not alerts:
        print("\n📧 No tickers crossed the alert threshold. No email sent.")
        return

    smtp_host = alert_cfg.get("smtp_host", "smtp.gmail.com")
    smtp_port = alert_cfg.get("smtp_port", 587)
    sender = os.environ.get("ALERT_EMAIL_FROM") or alert_cfg.get("from_email", "")
    password = os.environ.get("ALERT_EMAIL_PASSWORD", "")
    recipient = os.environ.get("ALERT_EMAIL_TO") or alert_cfg.get("to_email", "")

    if not all([sender, password, recipient]):
        print("\n⚠️  Email credentials missing — skipping alert. Set ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD, ALERT_EMAIL_TO env vars.")
        return

    subject = f"🔥 Dividend Screener: {len(alerts)} Buying Opportunity Alert(s)"

    rows = ""
    for r in alerts:
        rows += f"""
        <tr>
          <td style="padding:8px 12px;font-family:monospace;color:#f0b429;font-weight:bold">{r['ticker']}</td>
          <td style="padding:8px 12px">{r.get('name','')[:30]}</td>
          <td style="padding:8px 12px;font-family:monospace">{r.get('opportunity_score',0)}</td>
          <td style="padding:8px 12px">{r.get('tier','')}</td>
          <td style="padding:8px 12px;font-family:monospace">{r.get('div_yield','—')}%</td>
          <td style="padding:8px 12px;font-family:monospace">{r.get('drop_from_52w','—')}%</td>
        </tr>"""

    html_body = f"""
    <html><body style="background:#0d1117;color:#e6edf3;font-family:'DM Sans',sans-serif;padding:24px">
    <h2 style="color:#f0b429;font-family:Georgia,serif">🔥 Dividend Panic Screener Alert</h2>
    <p style="color:#8b949e">{len(alerts)} ticker(s) crossed your alert threshold of {threshold}.</p>
    <table style="border-collapse:collapse;width:100%;margin-top:16px;background:#161b22;border-radius:8px">
      <thead>
        <tr style="border-bottom:2px solid #30363d;color:#8b949e;font-size:12px">
          <th style="padding:8px 12px;text-align:left">Ticker</th>
          <th style="padding:8px 12px;text-align:left">Name</th>
          <th style="padding:8px 12px;text-align:left">Score</th>
          <th style="padding:8px 12px;text-align:left">Signal</th>
          <th style="padding:8px 12px;text-align:left">Yield</th>
          <th style="padding:8px 12px;text-align:left">vs 52W Hi</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="margin-top:20px;color:#4b5563;font-size:12px">View full dashboard on GitHub Pages.</p>
    </body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(sender, password)
            s.sendmail(sender, recipient, msg.as_string())
        print(f"\n✅ Alert email sent to {recipient} ({len(alerts)} tickers).")
    except Exception as e:
        print(f"\n❌ Email failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dividend Panic Screener")
    parser.add_argument("--no-email", action="store_true", help="Skip email alert")
    parser.add_argument("--watchlist-only", action="store_true", help="Only screen watchlist, skip broad universe")
    parser.add_argument("--output", default="docs/index.html", help="Output HTML path")
    args = parser.parse_args()

    config = load_config()
    news_key = os.environ.get("NEWSAPI_KEY") or config.get("api_keys", {}).get("newsapi", "")

    # Build ticker list
    watchlist = config["screening"].get("watchlist", [])
    if args.watchlist_only:
        tickers = list(dict.fromkeys(watchlist))
    else:
        universe_tickers = screen_universe(config)
        tickers = list(dict.fromkeys(watchlist + universe_tickers))

    print(f"\n🔍 Processing {len(tickers)} tickers...\n")

    results = []
    for ticker in tickers:
        print(f"  → {ticker}")
        thresholds = config["screening"].get("ticker_overrides", {}).get(
            ticker, {"drop_threshold_pct": config["screening"]["default_drop_threshold_pct"]}
        )
        thresholds["min_yield_pct"] = config["screening"]["min_yield_pct"]

        yf_data = fetch_yfinance(ticker)
        if not yf_data:
            print(f"    ⚠️  No data for {ticker}, skipping.")
            continue

        streak_data = fetch_dividend_streak(ticker)
        av_data = fetch_historical_pe(ticker)
        # Try Finviz analyst rating first; fall back to NewsAPI sentiment
        sentiment_data = fetch_analyst_rating(ticker)
        if sentiment_data.get("sentiment_label") == "— N/A" and news_key:
            sentiment_data = fetch_news_sentiment(yf_data.get("sector", ""), ticker, news_key)

        merged = {**yf_data, **streak_data, **av_data, **sentiment_data}

        # Apply quality filters
        min_yield = config["screening"]["min_yield_pct"]
        min_streak = config["screening"]["min_dividend_streak_years"]
        if merged.get("div_yield", 0) < min_yield:
            print(f"    ⛔ Yield {merged.get('div_yield',0)}% < {min_yield}% threshold, skipping.")
            continue
        streak = merged.get("dividend_streak", 0)
        if streak and streak < min_streak:
            print(f"    ⛔ Streak {streak} yrs < {min_streak} yr minimum, skipping.")
            continue

        scored = compute_opportunity_score(merged, thresholds)
        results.append(scored)
        print(f"    ✓ Score: {scored['opportunity_score']} | {scored['tier']}")

    # Sort by score
    results.sort(key=lambda x: x.get("opportunity_score", 0), reverse=True)

    # Generate HTML
    generated_at = datetime.datetime.now().strftime("%B %d, %Y at %I:%M %p")
    html = generate_html(results, config, generated_at)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"\n✅ Dashboard written to {output_path} ({len(results)} tickers)")

    # Send alert
    if not args.no_email:
        send_email_alert(results, config)


if __name__ == "__main__":
    main()
