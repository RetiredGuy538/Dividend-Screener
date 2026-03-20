# 📈 Dividend Panic Screener

Flags high-quality dividend stocks and ETFs that have been oversold due to
panic, sentiment, or sector-wide news events — generating a buying opportunity
dashboard served via GitHub Pages.

---

## How It Works

The screener computes a composite **Opportunity Score (0–100)** for each ticker:

| Signal | Weight | Description |
|---|---|---|
| Drop vs. 52-Week High | 25% | How far has price fallen from its peak? |
| Drop vs. 50-Day MA | 20% | Short-term technical pullback |
| Yield Signal | 20% | Is the yield elevated vs. your minimum? |
| P/E Signal | 20% | Is forward P/E below historical average? |
| News Sentiment | 15% | Bearish sector news = opportunity |

**Tiers:**
- 🔥 **Strong Buy Signal** — Score ≥ 70
- 👀 **Watch List** — Score 50–69
- ⏳ **Hold** — Score 30–49
- ✅ **Fairly Valued** — Score < 30

---

## Setup

### 1. Clone / fork this repo

```bash
git clone https://github.com/YOUR_USERNAME/dividend-screener.git
cd dividend-screener
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Get free API keys

- **Financial Modeling Prep (FMP):** https://financialmodelingprep.com/developer/docs
  - Free tier: 250 requests/day (plenty for a watchlist)
  - Used for: dividend history, payout ratios, historical P/E
- **NewsAPI:** https://newsapi.org/register
  - Free tier: 100 requests/day
  - Used for: sector news sentiment

### 4. Configure `config.yaml`

Edit `config.yaml` to:
- Add your watchlist tickers
- Set your minimum yield threshold (default: 3%)
- Set per-ticker drop thresholds
- Set your alert score threshold

### 5. Set up Gmail alerts

In Gmail, create an **App Password** (not your regular password):
1. Go to myaccount.google.com → Security → 2-Step Verification → App passwords
2. Create one named "Dividend Screener"

Then set environment variables:
```bash
export ALERT_EMAIL_FROM="you@gmail.com"
export ALERT_EMAIL_PASSWORD="your-app-password"
export ALERT_EMAIL_TO="you@gmail.com"
```

### 6. Run locally

```bash
python dividend_screener.py
# Watchlist only (faster):
python dividend_screener.py --watchlist-only
# Skip email:
python dividend_screener.py --no-email
# Custom output path:
python dividend_screener.py --output ~/Desktop/screener.html
```

Then open `docs/index.html` in your browser.

---

## GitHub Pages Setup

### 1. Push to GitHub

```bash
git add .
git commit -m "Initial setup"
git push origin main
```

### 2. Enable GitHub Pages

In your repo → **Settings → Pages → Source: Deploy from branch → main → /docs**

### 3. Add GitHub Secrets

In your repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | Value |
|---|---|
| `FMP_API_KEY` | Your FMP API key |
| `NEWSAPI_KEY` | Your NewsAPI key |
| `ALERT_EMAIL_FROM` | Your Gmail address |
| `ALERT_EMAIL_PASSWORD` | Your Gmail App Password |
| `ALERT_EMAIL_TO` | Recipient email |

### 4. The workflow runs automatically

- **Daily:** Weekdays at 8:00 AM Central
- **Manual:** Actions tab → "Dividend Screener — Daily Run" → Run workflow

---

## Customizing the Watchlist

Edit `config.yaml` — the `watchlist` section is your hand-picked list.
The broad universe scan runs separately and adds anything meeting your yield floor.

### Per-ticker drop thresholds

```yaml
ticker_overrides:
  T:
    drop_threshold_pct: 15   # Need 15% drop for AT&T to flag
  O:
    drop_threshold_pct: 8    # Flag Realty Income after just 8% drop
```

---

## Data Sources

| Source | Used For | Cost |
|---|---|---|
| yfinance | Prices, 52W high, MA, basic yield | Free |
| Financial Modeling Prep | Dividend history/streak, payout ratio, hist P/E | Free (250 req/day) |
| NewsAPI | Sector sentiment | Free (100 req/day) |
