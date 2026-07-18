import yfinance as yf
import pandas as pd

# Earliest date we hold data for. ECB reference rates begin in 1999, so 2000
# is safe. price and a rate always coexist. no dates with one but not the other.
DATA_START = "2000-01-01"


def getTickerPrice(tickerTag):
    tic = yf.Ticker(tickerTag)
    return tic.fast_info["last_price"]


def getTickerPrices(tickers):
    prices = {}
    for t in tickers:
        if not t:
            continue
        try:
            prices[t] = getTickerPrice(t)
        except Exception:
            prices[t] = None
    return prices


def getTickerPriceHistory(tickerTag, start=DATA_START):
    """
    Full history from `start` (default 2000-01-01) to today.
    Returns ({date: price}, currency). Tickers that IPO'd later simply
    return whatever exists from their listing date onward.
    """
    tic = yf.Ticker(tickerTag)

    try:
        currency = tic.fast_info["currency"]
    except Exception:
        currency = None

    hist = tic.history(start=start, auto_adjust=True)

    if hist.empty:
        return {}, currency

    prices = {idx.date(): float(row["Close"]) for idx, row in hist.iterrows()}
    return prices, currency


def getTickerPricesHistory(tickers, start=DATA_START):
    result = {}
    for t in tickers:
        if not t:
            continue
        try:
            result[t] = getTickerPriceHistory(t, start)
        except Exception:
            result[t] = ({}, None)
    return result

def getTickerName(tickerTag):
    """Company name for a ticker. Returns '' if unavailable."""
    try:
        info = yf.Ticker(tickerTag).info
        return info.get("shortName") or info.get("longName") or ""
    except Exception:
        return ""