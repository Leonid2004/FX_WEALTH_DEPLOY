import math
from decimal import Decimal
from .models import FxRate, AssetPrice
from .fxcurrency import *
from .pyfinancedata import getTickerPricesHistory


def fillTheFxRateTable():
    callFoo = getFullFxRates()
    curr = callFoo[0]
    ref_date = callFoo[1]
   # curr, ref_date = getFullFxRates()

    objs = []
    for pair, rate in curr.items():
        base, quote = pair.split("/")          # 'EUR/CHF' -> ('EUR', 'CHF')
        objs.append(
            FxRate(
                base=base,
                quote=quote,
                rate=Decimal(str(rate[0])),        # str() first to avoid float noise
                date=ref_date,
                source=rate[1],
            )
        )

    created = FxRate.objects.bulk_create(objs, ignore_conflicts=True)
    return created


def populateHistoricFxTable(start=DATA_START):
    history = getFullHistoricalFxRates(start)
    objs = []
    for date, rates in history.items():
        for pair, (rate, src) in rates.items():
            base, quote = pair.split("/")
            objs.append(FxRate(
                base=base, quote=quote,
                rate=Decimal(str(rate)),
                date=date, source=src,
            ))
    FxRate.objects.bulk_create(objs, ignore_conflicts=True)
    return len(objs)


def populateHistoricAssetPriceTable(tickers, start=DATA_START):
    histories = getTickerPricesHistory(tickers, start)

    objs = []
    for ticker, (series, currency) in histories.items():
        if not series:
            continue

        # LSE stocks are quoted in pence ("GBp"/"GBX"): 100 pence = 1 pound.  !!!!!!
        # Normalize to pounds ON WRITE so every consumer downstream
        # (valuation, attribution, simulation) sees normal units.
        pence = currency in ("GBp", "GBX")
        stored_currency = "GBP" if pence else (currency or "")

        for d, price in series.items():
            # yfinance gaps: never let NaN into the price table
            if price is None or (isinstance(price, float) and math.isnan(price)):
                continue
            if pence:
                price = price / 100.0
            objs.append(AssetPrice(
                ticker=ticker,
                price=Decimal(str(price)),
                currency=stored_currency,
                date=d,
                source="yfinance",
            ))

    AssetPrice.objects.bulk_create(objs, ignore_conflicts=True)
    return len(objs)


def backfillAllPortfolioTickers(start=DATA_START):
    from tracker.models import Position

    ticker_based = {"listed_equity", "etf"}
    tickers = (
        Position.objects
        .filter(asset_type__in=ticker_based)
        .exclude(ticker="")
        .values_list("ticker", flat=True)
        .distinct()
    )
    return populateHistoricAssetPriceTable(set(tickers), start)


def getLatestStoredPrices(tickers):
    """
    Latest stored close per ticker, from AssetPrice.
    Returns {ticker: Decimal}. Single query, no network.
    """
    if not tickers:
        return {}

    prices = {}
    for t in tickers:
        row = (
            AssetPrice.objects
            .filter(ticker=t)
            .order_by("-date")
            .first()
        )
        if row:
            prices[t] = row.price
    return prices


def refreshRecentPrices(tickers, since=None, days_back=7):
    """
    Top up stored prices.
    since     - fetch from this date forward (fills the exact gap)
    days_back - fallback window if `since` isn't given
    """
    from datetime import timedelta
    from django.utils import timezone

    if since is None:
        since = timezone.now().date() - timedelta(days=days_back)

    return populateHistoricAssetPriceTable(tickers, start=since.isoformat())


def getStoredPriceCurrencies(tickers):
    """{ticker: currency-of-latest-stored-price}. Empty string if unknown."""
    if not tickers:
        return {}
    out = {}
    for t in tickers:
        row = (
            AssetPrice.objects
            .filter(ticker=t)
            .order_by("-date")
            .values_list("currency", flat=True)
            .first()
        )
        if row:
            out[t] = row
    return out