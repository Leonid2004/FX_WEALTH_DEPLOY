from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView
from django.contrib.auth import login
from .forms import *
from django.contrib.auth.mixins import LoginRequiredMixin
from marketdata.models import *
from marketdata.mdfiller import fillTheFxRateTable, getLatestStoredPrices, refreshRecentPrices, getStoredPriceCurrencies
import json
from decimal import Decimal
from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from .models import Position
from marketdata.models import FxRate
from marketdata.mcsimulation import simulate_position, percentile_bands, simulate_portfolio
import numpy as np
import logging
logger = logging.getLogger(__name__)


def get_portfolio(user):
    """The user's portfolio, created on the fly if missing (e.g. superusers)."""
    portfolio, _ = Portfolio.objects.get_or_create(
        user=user, defaults={"base_currency": "CHF"}
    )
    return portfolio



# Create your views here.
def register_view(request):

    if request.method == "POST":

        form = RegisterForm(request.POST)

        if form.is_valid():
            user = form.save()

            Portfolio.objects.create(
                user=user,
                base_currency=form.cleaned_data["base_currency"]
            )

            login(request, user)

            return redirect("home")

    else:

        form = RegisterForm()

    return render(request, "register.html", {

        "form": form

    })

class HomeView(LoginRequiredMixin, ListView):
    model = Position
    template_name = "index.html"
    context_object_name = "positions"

    def get_queryset(self):
        from datetime import timedelta
        from marketdata.models import AssetPrice

        today = timezone.now().date()

        # --- FX freshness (your existing check) ---
        latest_fx = FxRate.objects.order_by("-date").values_list("date", flat=True).first()
        if latest_fx is None or (today - latest_fx).days > 3:
            try:
                fillTheFxRateTable()
            except Exception:
                logger.warning("FX refresh failed; serving stored rates", exc_info=True)

        positions = get_portfolio(self.request.user).position_set.all()

        # --- price freshness: top up stored prices if stale ---
        ticker_based = {"listed_equity", "etf"}
        tickers = {
            p.ticker for p in positions
            if p.asset_type in ticker_based and p.ticker
        }

        for t in tickers:
            latest_price = (
                AssetPrice.objects
                    .filter(ticker=t)
                    .order_by("-date")
                    .values_list("date", flat=True)
                    .first()
            )
            try:
                if latest_price is None:
                    refreshRecentPrices({t})
                elif (today - latest_price).days > 3:
                    refreshRecentPrices({t}, since=latest_price)
            except Exception:
                logger.warning("Price refresh failed for %s", t, exc_info=True)

        return positions

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        latest_date = (
            FxRate.objects.order_by("-date")
                .values_list("date", flat=True)
                .first()
        )

        # empty table -> no conversions possible, but don't crash
        if latest_date is None:
            rate_lookup = {}
        else:
            rate_lookup = {
                f"{r.base}/{r.quote}": r.rate
                for r in FxRate.objects.filter(date=latest_date)
            }

        portfolio = get_portfolio(self.request.user)
        base_ccy = portfolio.base_currency
        positions = context["positions"]

        ticker_based = {"listed_equity", "etf"}
        needed_tickers = {
            p.ticker for p in positions
            if p.asset_type in ticker_based and p.ticker
        }
        price_lookup = getLatestStoredPrices(needed_tickers)
        price_ccy = getStoredPriceCurrencies(needed_tickers)

        total = Decimal("0")
        any_missing = False

        for p in positions:
            local = p.current_local_value(price_lookup)
            p.local = local

            if local is None:
                p.value_base = None
                any_missing = True
                continue

            # position currency must match the currency the stored price is
            # actually denominated in, otherwise we'd convert at the wrong
            # rate. Better to show "missing" than a silently wrong number.
            stored = price_ccy.get(p.ticker)
            if p.ticker and stored and stored != p.currency:
                p.value_base = None
                any_missing = True
                continue

            if p.currency == base_ccy:
                p.value_base = local
            else:
                rate = rate_lookup.get(f"{p.currency}/{base_ccy}")
                if rate is None:
                    p.value_base = None
                    any_missing = True
                    continue
                p.value_base = local * rate

            total += p.value_base
        port_total_money = Decimal("0")
        port_asset_money = Decimal("0")
        port_currency_money = Decimal("0")
        port_interaction_money = Decimal("0")
        port_start_value = Decimal("0")
        for p in positions:
            # only ticker-based positions have market-price attribution
            if p.asset_type not in ticker_based or not p.ticker or not p.acquisition_date:
                p.r_base = None
                continue

            # skip attribution if the position's declared currency doesn't
            # match the currency the stored prices are actually in
            stored = price_ccy.get(p.ticker)
            if stored and stored != p.currency:
                p.r_base = None
                continue

            start = p.acquisition_date

            p_start = get_price_on_or_before(p.ticker, start)
            p_end = price_lookup.get(p.ticker)

            if p_start is None or p_end is None or p_start == 0:
                p.r_base = None
                continue

            r_local = (p_end / p_start) - 1

            # --- FX at start and end (position currency -> base) ---
            if p.currency == base_ccy:
                r_fx = Decimal("0")
                s_start = Decimal("1")
            else:
                s_start = get_rate_on_or_before(p.currency, base_ccy, start)
                s_end = rate_lookup.get(f"{p.currency}/{base_ccy}")
                if s_start is None or s_end is None or s_start == 0:
                    p.r_base = None
                    continue
                r_fx = (s_end / s_start) - 1

            r_base = (1 + r_local) * (1 + r_fx) - 1
            r_interaction = r_local * r_fx

            # --- money decomposition (uses decimals) ---
            v_start_base = p_start * p.quantity * s_start
            p.asset_effect_money = v_start_base * r_local
            p.currency_effect_money = v_start_base * r_fx
            p.interaction_effect_money = v_start_base * r_interaction
            p.total_effect_money = v_start_base * r_base  # <-- r_base, NOT p.r_base

            # --- percentage versions (display only, ×100) ---
            p.r_local = r_local * 100
            p.r_fx = r_fx * 100
            p.r_interaction = r_interaction * 100
            p.r_base = r_base * 100

                # ... existing per-position attribution ...
            if p.r_base is not None:
                    port_asset_money += p.asset_effect_money
                    port_currency_money += p.currency_effect_money
                    port_interaction_money += p.interaction_effect_money
                    port_start_value += v_start_base  # sum of starting values in CHF

        context["base_currency"] = base_ccy
        context["total_net_worth"] = total
        context["has_missing_values"] = any_missing

        port_total_money = port_asset_money + port_currency_money + port_interaction_money
        context["port_asset_money"] = port_asset_money
        context["port_currency_money"] = port_currency_money
        context["port_interaction_money"] = port_interaction_money
        context["port_total_money"] = port_total_money

        if port_start_value > 0:
            context["port_asset_pct"] = port_asset_money / port_start_value * 100
            context["port_currency_pct"] = port_currency_money / port_start_value * 100
            context["port_interaction_pct"] = port_interaction_money / port_start_value * 100
            context["port_total_pct"] = port_total_money / port_start_value * 100
        else:
            context["port_asset_pct"] = None  # nothing attributable yet


        return context

    def post(self, request, *args, **kwargs):
        position_id = request.POST.get("position_id")

        if not position_id:
            return redirect("home")

        position = get_object_or_404(
            Position,
            pk=position_id,
            portfolio=get_portfolio(request.user),
        )

        position.delete()

        return redirect("home")


@login_required
def add_position(request):
    if request.method == "POST":
        form = PositionForm(request.POST)

        if form.is_valid():
            position = form.save(commit=False)
            position.portfolio = get_portfolio(request.user)
            position.save()

            # backfill history for a newly-added ticker
            if position.asset_type in {"listed_equity", "etf"} and position.ticker:
                from marketdata.mdfiller import populateHistoricAssetPriceTable
                try:
                    populateHistoricAssetPriceTable({position.ticker.strip().upper()})
                except Exception:
                    logger.warning("Backfill failed for %s", position.ticker, exc_info=True)

            return redirect("/")

    else:
        form = PositionForm()

    return render(request, "addposition.html", {
        "form": form
    })


def get_price_on_or_before(ticker, date):
    row = (AssetPrice.objects
           .filter(ticker=ticker, date__lte=date)
           .order_by("-date")
           .first())
    return row.price if row else None

def get_rate_on_or_before(base, quote, date):
    row = (FxRate.objects
           .filter(base=base, quote=quote, date__lte=date)
           .order_by("-date")
           .first())
    return row.rate if row else None


@login_required
def position_detail(request, pk):
    """
    Detail page for one holding, with a Monte Carlo projection.

    The user can adjust the lookback window (how much history to sample from)
    and the horizon (how far forward to project) via query params.
    """
    # ownership scoping same pattern as the delete handler
    position = get_object_or_404(
        Position,
        pk=pk,
        portfolio=get_portfolio(request.user),
    )

    base_ccy = get_portfolio(request.user).base_currency

    # --- user-chosen simulation parameters (with sane defaults + clamping) ---
    try:
        lookback_years = int(request.GET.get("lookback", 5))
    except (TypeError, ValueError):
        lookback_years = 5

    try:
        horizon_years = int(request.GET.get("horizon", 1))
    except (TypeError, ValueError):
        horizon_years = 1

    lookback_years = max(2, min(lookback_years, 10))  # 2..10
    horizon_years = max(1, min(horizon_years, 5))  # 1..5

    # --- current price and FX rate ---
    price_lookup = getLatestStoredPrices({position.ticker} if position.ticker else set())
    current_price = price_lookup.get(position.ticker)

    latest_date = (
        FxRate.objects.order_by("-date")
            .values_list("date", flat=True)
            .first()
    )
    rate_lookup = {}
    if latest_date:
        rate_lookup = {
            f"{r.base}/{r.quote}": r.rate
            for r in FxRate.objects.filter(date=latest_date)
        }

    current_fx_rate = (
        None if position.currency == base_ccy
        else rate_lookup.get(f"{position.currency}/{base_ccy}")
    )

    # --- run the simulation ---
    # --- run the simulation (unless the currency label is wrong) ---
    stored_ccy = (
        getStoredPriceCurrencies({position.ticker}).get(position.ticker)
        if position.ticker else None
    )
    if stored_ccy and stored_ccy != position.currency:
        sim = {
            "simulatable": False,
            "reason": (
                f"Currency mismatch: position says {position.currency}, "
                f"stored prices are in {stored_ccy}. Edit the position to fix."
            ),
        }
    else:
        sim = simulate_position(
            position,
            current_price=current_price,
            current_fx_rate=current_fx_rate,
            base_ccy=base_ccy,
            lookback_years=lookback_years,
            horizon_years=horizon_years,
            n_paths=10000,
            keep_paths=True,
        )

    context = {
        "position": position,
        "base_currency": base_ccy,
        "sim": sim,
        "lookback_years": lookback_years,
        "horizon_years": horizon_years,
        "lookback_options": [2, 3, 5, 7, 10],
        "horizon_options": [1, 2, 3, 5],
    }

    # --- chart data: collapse 5000 paths into percentile bands ---
    if sim.get("simulatable"):
        paths = sim["paths"]
        bands = percentile_bands(paths, levels=(5, 25, 50, 75, 95))
        n_days = paths.shape[1]

        # sample a handful of real paths to draw faintly behind the bands
        sample_idx = list(range(0, paths.shape[0], max(1, paths.shape[0] // 60)))[:60]
        sample_paths = [[float(v) for v in paths[i]] for i in sample_idx]

        context["chart_data"] = json.dumps({
            "days": list(range(n_days)),
            "p5": bands[5],
            "p25": bands[25],
            "p50": bands[50],
            "p75": bands[75],
            "p95": bands[95],
            "samples": sample_paths,
            "start": sim["start_value"],
            "currency": base_ccy,
        })

        # histogram of ending values
        endings = sim["endings"]
        import numpy as np
        counts, edges = np.histogram(endings, bins=40)
        context["hist_data"] = json.dumps({
            "counts": [int(c) for c in counts],
            "edges": [float(e) for e in edges],
            "start": sim["start_value"],
        })

    return render(request, "position_detail.html", context)


@login_required
def portfolio_projection(request):
    """
    Full-portfolio Monte Carlo projection.

    Every holding and every currency is simulated from the SAME drawn historical
    days, so real co-movement is preserved. Sampling each asset independently
    would let one asset's bad day cancel another's good day -- overstating
    diversification and understating portfolio risk.
    """
    portfolio = get_portfolio(request.user)
    base_ccy = portfolio.base_currency
    positions = list(portfolio.position_set.all())

    # --- user-chosen parameters, clamped to sane bounds ---
    try:
        lookback_years = int(request.GET.get("lookback", 5))
    except (TypeError, ValueError):
        lookback_years = 5

    try:
        horizon_years = int(request.GET.get("horizon", 1))
    except (TypeError, ValueError):
        horizon_years = 1

    lookback_years = max(2, min(lookback_years, 10))
    horizon_years = max(1, min(horizon_years, 5))

    # --- current prices and FX rates ---
    ticker_based = {"listed_equity", "etf"}
    tickers = {p.ticker for p in positions if p.asset_type in ticker_based and p.ticker}
    price_lookup = getLatestStoredPrices(tickers)
    price_ccy = getStoredPriceCurrencies(tickers)

    latest_date = (
        FxRate.objects.order_by("-date")
            .values_list("date", flat=True)
            .first()
    )
    rate_lookup = {}
    if latest_date:
        rate_lookup = {
            f"{r.base}/{r.quote}": r.rate
            for r in FxRate.objects.filter(date=latest_date)
        }

    # --- run the simulation ---
    sim = simulate_portfolio(
        positions,
        price_lookup=price_lookup,
        price_ccy=price_ccy,
        rate_lookup=rate_lookup,
        base_ccy=base_ccy,
        lookback_years=lookback_years,
        horizon_years=horizon_years,
        n_paths=10000,
        keep_paths=True,
    )

    context = {
        "sim": sim,
        "base_currency": base_ccy,
        "lookback_years": lookback_years,
        "horizon_years": horizon_years,
        "lookback_options": [2, 3, 5, 7, 10],
        "horizon_options": [1, 2, 3, 5],
    }

    if sim.get("simulatable"):
        paths = sim["paths"]
        bands = percentile_bands(paths, levels=(5, 25, 50, 75, 95))
        n_days = paths.shape[1]

        # thin sample of real paths, drawn faintly behind the bands
        step = max(1, paths.shape[0] // 60)
        sample_idx = list(range(0, paths.shape[0], step))[:60]
        sample_paths = [[float(v) for v in paths[i]] for i in sample_idx]

        context["chart_data"] = json.dumps({
            "days": list(range(n_days)),
            "p5": bands[5],
            "p25": bands[25],
            "p50": bands[50],
            "p75": bands[75],
            "p95": bands[95],
            "samples": sample_paths,
            "start": sim["start_value"],
            "currency": base_ccy,
        })

        counts, edges = np.histogram(sim["endings"], bins=44)
        context["hist_data"] = json.dumps({
            "counts": [int(c) for c in counts],
            "edges": [float(e) for e in edges],
            "start": sim["start_value"],
            "currency": base_ccy,
        })

    return render(request, "portfolio_projection.html", context)

@login_required
def edit_position(request, pk):
    position = get_object_or_404(
        Position, pk=pk, portfolio=get_portfolio(request.user)
    )
    if request.method == "POST":
        form = PositionForm(request.POST, instance=position)
        if form.is_valid():
            form.save()
            return redirect("position_detail", pk=position.pk)
    else:
        form = PositionForm(instance=position)

    return render(request, "addposition.html", {"form": form, "editing": True})