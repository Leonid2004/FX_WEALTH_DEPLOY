
import numpy as np
from datetime import timedelta
from django.utils import timezone

from .models import AssetPrice, FxRate



# Hard floor. Below this many observations, bootstrap estimates are unreliable
# regardless of what window the user requests - the sample is too thin to
# contain a representative range of market conditions (notably drawdowns).
MIN_TRADING_DAYS = 500

TRADING_DAYS_PER_YEAR = 252

TICKER_BASED_ASSETS = {"listed_equity", "etf"}

DEFAULT_LOOKBACK_YEARS = 5
DEFAULT_HORIZON_YEARS = 1
DEFAULT_N_PATHS = 10000


# ----------------------------------------------------------------------
# Data assembly
# ----------------------------------------------------------------------

def _cutoff_date(lookback_years):
    """The earliest date to include, given a lookback window in years."""
    return timezone.now().date() - timedelta(days=int(lookback_years * 365.25))


def build_aligned_growth_matrix(tickers, currency_pairs, lookback_years):
    """
    Build a matrix of daily growth factors where:
        every ROW    = one historical date
        every COLUMN = one risk factor (an asset, or an FX pair)

    Only dates on which EVERY factor has data are kept. That intersection is
    what makes each row a genuine simultaneous snapshot - the basis for
    preserving correlation when we later draw whole rows at random.

    Returns:

    (growth_matrix, factor_names, error)
        growth_matrix : ndarray (n_dates - 1, n_factors) of growth factors
        factor_names  : list of names, aligned to columns
        error         : str reason, or None on success
    """
    cutoff = _cutoff_date(lookback_years)

    series = {}   # factor name -> {date: value}

    for t in tickers:
        rows = (
            AssetPrice.objects
            .filter(ticker=t, date__gte=cutoff)
            .values_list("date", "price")
        )
        s = {d: float(v) for d, v in rows}
        if not s:
            return None, None, f"No stored price history for {t}"
        series[t] = s

    for base, quote in currency_pairs:
        name = f"{base}/{quote}"
        rows = (
            FxRate.objects
            .filter(base=base, quote=quote, date__gte=cutoff)
            .values_list("date", "rate")
        )
        s = {d: float(v) for d, v in rows}
        if not s:
            return None, None, f"No stored FX history for {name}"
        series[name] = s

    if not series:
        return None, None, "No risk factors to simulate"

    # keep only dates present for EVERY factor
    common_dates = None
    for s in series.values():
        dates = set(s.keys())
        common_dates = dates if common_dates is None else (common_dates & dates)

    if not common_dates:
        return None, None, "No overlapping dates across assets and FX rates"

    common_dates = sorted(common_dates)   # chronological order is essential

    if len(common_dates) < MIN_TRADING_DAYS + 1:
        return None, None, (
            f"Insufficient overlapping history: {len(common_dates)} days "
            f"available, {MIN_TRADING_DAYS + 1} required"
        )

    factor_names = list(series.keys())

    # (n_dates x n_factors) matrix of levels
    price_matrix = np.array([
        [series[name][d] for name in factor_names]
        for d in common_dates
    ])

    # growth factor = level[t+1] / level[t], computed down the DATE axis
    growth_matrix = price_matrix[1:, :] / price_matrix[:-1, :]

    return growth_matrix, factor_names, None


# ----------------------------------------------------------------------
# Statistics helper
# ----------------------------------------------------------------------

def _summarize(endings, start_value):
    """Turn an array of simulated ending values into the risk numbers."""
    p5 = float(np.percentile(endings, 5))
    p25 = float(np.percentile(endings, 25))
    p50 = float(np.percentile(endings, 50))
    p75 = float(np.percentile(endings, 75))
    p95 = float(np.percentile(endings, 95))

    return {
        "start_value": float(start_value),
        "median": p50,
        "p5": p5,
        "p25": p25,
        "p75": p75,
        "p95": p95,
        "var_95": float(start_value - p5),  # 95%-confidence loss bound
        "var_95_display": float(max(start_value - p5, 0.0)),  # convention: VaR floored at 0
        "no_loss_at_95": bool(p5 >= start_value),  # 5th percentile ends above today
        "prob_loss": float(np.mean(endings < start_value)),
        "median_return_pct": float((p50 / start_value - 1) * 100),
        "p5_return_pct": float((p5 / start_value - 1) * 100),
        "p95_return_pct": float((p95 / start_value - 1) * 100),
    }



# ENTRY POINT 1 -- a single position (user clicked one holding)


def simulate_position(position, current_price, current_fx_rate, base_ccy,
                      lookback_years=DEFAULT_LOOKBACK_YEARS,
                      horizon_years=DEFAULT_HORIZON_YEARS,
                      n_paths=DEFAULT_N_PATHS,
                      seed=None,
                      keep_paths=True):
    """
    Simulate ONE holding forward, in the portfolio's base currency.

    Both the asset price AND its exchange rate are simulated together, drawn
    from the same historical days - so if the asset and the currency tend to
    move together, that relationship is preserved.

    Parameters
    ----------
    position        : a Position instance
    current_price   : today's price in the asset's native currency
    current_fx_rate : today's rate  position.currency -> base_ccy  (None if same)
    base_ccy        : e.g. 'CHF'
    keep_paths      : if True, return the full (n_paths x n_days) array for charting

    Returns a dict. On failure: {'simulatable': False, 'reason': ...}
    """
    if position.asset_type not in TICKER_BASED_ASSETS or not position.ticker:
        return {
            "simulatable": False,
            "reason": "Only listed equities and ETFs can be simulated "
                      "(no market price history for this asset type)",
        }

    if current_price is None:
        return {"simulatable": False, "reason": "No current price available"}

    same_ccy = (position.currency == base_ccy)

    if not same_ccy and current_fx_rate is None:
        return {
            "simulatable": False,
            "reason": f"No FX rate available for {position.currency}/{base_ccy}",
        }

    tickers = {position.ticker}
    currency_pairs = set() if same_ccy else {(position.currency, base_ccy)}

    growth_matrix, factor_names, error = build_aligned_growth_matrix(
        tickers, currency_pairs, lookback_years
    )
    if growth_matrix is None:
        return {"simulatable": False, "reason": error}

    n_dates = growth_matrix.shape[0]
    col = {name: i for i, name in enumerate(factor_names)}

    rng = np.random.default_rng(seed)

    n_days = int(horizon_years * TRADING_DAYS_PER_YEAR)

    # - draw random DATES (row indices), shared by every factor -
    date_idx = rng.integers(0, n_dates, size=(n_paths, n_days))

    # asset growth compounded along each path
    asset_growth = np.cumprod(growth_matrix[date_idx, col[position.ticker]], axis=1)

    # FX growth compounded along the SAME drawn days
    if same_ccy:
        fx_growth = np.ones_like(asset_growth)
        fx_now = 1.0
    else:
        fx_col = col[f"{position.currency}/{base_ccy}"]
        fx_growth = np.cumprod(growth_matrix[date_idx, fx_col], axis=1)
        fx_now = float(current_fx_rate)

    price_now = float(current_price)
    qty = float(position.quantity)

    # today's value of this holding, in base currency
    v_now = price_now * qty * fx_now

    # value on every path, every day:  today's value x asset growth x fx growth
    value_paths = v_now * asset_growth * fx_growth

    # prepend today's value as day 0 so paths start from reality
    value_paths = np.hstack([np.full((n_paths, 1), v_now), value_paths])

    endings = value_paths[:, -1]

    result = {
        "simulatable": True,
        "ticker": position.ticker,
        "currency": position.currency,
        "base_currency": base_ccy,
        "quantity": qty,
        "current_price": price_now,
        "days_of_history": int(n_dates),
        "lookback_years": lookback_years,
        "horizon_years": horizon_years,
        "n_paths": n_paths,
        "factors": factor_names,
        "endings": endings,
    }
    result.update(_summarize(endings, v_now))

    if keep_paths:
        result["paths"] = value_paths

    return result



# ENTRY POINT 2 -- the whole portfolio, correlated


def simulate_portfolio(positions, price_lookup, rate_lookup, base_ccy,
                       price_ccy=None,
                       lookback_years=DEFAULT_LOOKBACK_YEARS,
                       horizon_years=DEFAULT_HORIZON_YEARS,
                       n_paths=DEFAULT_N_PATHS,
                       seed=None,
                       keep_paths=False):
    """
    Simulate the ENTIRE portfolio forward, with all holdings and all currencies
    correlated - every asset and rate replays the same drawn historical days,
    so assets that crash together in reality crash together in the simulation.

    This matters: sampling each asset independently would let one asset's bad
    day cancel another's good day, overstating diversification and
    UNDERSTATING portfolio risk.

    Parameters
    ----------
    positions    : iterable of Position objects
    price_lookup : {ticker: current_price}     (from getTickerPrices)
    rate_lookup  : {'USD/CHF': Decimal, ...}   (today's rates)
    base_ccy     : e.g. 'CHF'
    keep_paths   : if True, also return the full portfolio path array

    Returns a dict. On failure: {'simulatable': False, 'reason': ...}
    """
    #  decide what can be simulated, and collect the factors we need
    price_ccy = price_ccy or {}
    sim_positions = []
    excluded = []
    tickers = set()
    currency_pairs = set()

    for p in positions:
        if p.asset_type not in TICKER_BASED_ASSETS or not p.ticker:
            excluded.append({
                "label": p.ticker or p.get_asset_type_display(),
                "reason": "No market price history for this asset type",
            })
            continue

        if price_lookup.get(p.ticker) is None:
            excluded.append({"label": p.ticker, "reason": "No current price"})
            continue

        stored = price_ccy.get(p.ticker)
        if stored and stored != p.currency:
            excluded.append({
                "label": p.ticker,
                "reason": (
                    f"Currency mismatch: position says {p.currency}, "
                    f"stored prices are in {stored}"
                ),
            })
            continue

        if p.currency != base_ccy and rate_lookup.get(f"{p.currency}/{base_ccy}") is None:
            excluded.append({
                "label": p.ticker,
                "reason": f"No FX rate for {p.currency}/{base_ccy}",
            })
            continue

        # --- NEW: screen out thin-history tickers BEFORE they poison the
        #     date intersection. One 19-day IPO would otherwise collapse the
        #     usable history for the entire portfolio.
        n_obs = _ticker_history_length(p.ticker, lookback_years)
        if n_obs < MIN_TRADING_DAYS + 1:
            excluded.append({
                "label": p.ticker,
                "reason": (
                    f"Insufficient history: {max(0, n_obs - 1)} days, "
                    f"{MIN_TRADING_DAYS} required"
                ),
            })
            continue

        sim_positions.append(p)
        tickers.add(p.ticker)
        if p.currency != base_ccy:
            currency_pairs.add((p.currency, base_ccy))

    if not sim_positions:
        return {
            "simulatable": False,
            "reason": "No positions have enough data to simulate",
            "excluded": excluded,
        }

    # one aligned matrix covering EVERY factor in the portfolio
    growth_matrix, factor_names, error = build_aligned_growth_matrix(
        tickers, currency_pairs, lookback_years
    )
    if growth_matrix is None:
        return {"simulatable": False, "reason": error, "excluded": excluded}

    n_dates = growth_matrix.shape[0]
    col = {name: i for i, name in enumerate(factor_names)}

    if seed is not None:
        np.random.seed(seed)

    n_days = int(horizon_years * TRADING_DAYS_PER_YEAR)

    # === THE KEY STEP ===
    # One set of drawn dates, shared by EVERY position and EVERY currency.
    # Cell (i, j) says: "on path i, day j, replay historical day date_idx[i, j]."
    date_idx = np.random.randint(0, n_dates, size=(n_paths, n_days))

    portfolio_endings = np.zeros(n_paths)
    portfolio_paths = np.zeros((n_paths, n_days)) if keep_paths else None
    start_value = 0.0
    per_position = []

    for p in sim_positions:
        asset_growth = np.cumprod(growth_matrix[date_idx, col[p.ticker]], axis=1)

        if p.currency == base_ccy:
            fx_growth = np.ones_like(asset_growth)
            fx_now = 1.0
        else:
            fx_col = col[f"{p.currency}/{base_ccy}"]
            fx_growth = np.cumprod(growth_matrix[date_idx, fx_col], axis=1)
            fx_now = float(rate_lookup[f"{p.currency}/{base_ccy}"])

        price_now = float(price_lookup[p.ticker])
        qty = float(p.quantity)
        v_now = price_now * qty * fx_now

        # this holding's value on every path, every day
        value_paths = v_now * asset_growth * fx_growth
        position_endings = value_paths[:, -1]

        # aggregate PATH BY PATH - path i of NVDA adds to path i of AAPL,
        # so the portfolio total on each path reflects one coherent scenario
        portfolio_endings += position_endings
        start_value += v_now

        if keep_paths:
            portfolio_paths += value_paths

        pos_summary = {
            "ticker": p.ticker,
            "currency": p.currency,
            "quantity": qty,
            "weight_pct": None,      # filled in below, once start_value is known
        }
        pos_summary.update(_summarize(position_endings, v_now))
        per_position.append(pos_summary)

    # position weights, now that the portfolio total is known
    for ps in per_position:
        ps["weight_pct"] = (
            ps["start_value"] / start_value * 100 if start_value else 0.0
        )

    result = {
        "simulatable": True,
        "base_currency": base_ccy,
        "days_of_history": int(n_dates),
        "lookback_years": lookback_years,
        "horizon_years": horizon_years,
        "n_paths": n_paths,
        "factors": factor_names,
        "per_position": per_position,
        "excluded": excluded,
        "endings": portfolio_endings,
    }
    result.update(_summarize(portfolio_endings, start_value))

    if keep_paths:
        portfolio_paths = np.hstack([
            np.full((n_paths, 1), start_value), portfolio_paths
        ])
        result["paths"] = portfolio_paths

    return result



# chart data (percentile bands) without shipping 5000 paths


def percentile_bands(paths, levels=(5, 25, 50, 75, 95)):
    """
    Collapse a (n_paths x n_days) array into a few percentile lines, so the
    template can draw a clean fan chart without receiving 5000 series.

    Returns {level: [value_per_day, ...]}
    """
    return {
        level: [float(v) for v in np.percentile(paths, level, axis=0)]
        for level in levels
    }

def _ticker_history_length(ticker, lookback_years):
    """How many usable observations does this ticker have in the window?"""
    cutoff = _cutoff_date(lookback_years)
    return (
        AssetPrice.objects
        .filter(ticker=ticker, date__gte=cutoff)
        .count()
    )