"""
Tests for the marketdata app: stored-data helpers and the Monte Carlo engine.

No network access. All price/FX history is seeded into the test database
with deterministic values so every expected number can be derived by hand:

  * constant daily growth factor g  ->  every bootstrap draw returns g,
    so after n days EVERY path ends at exactly  start * g**n,
    regardless of which random dates were drawn. This turns the whole
    stochastic engine into something we can assert to machine precision.

Run with:  python manage.py test marketdata
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import numpy as np
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from marketdata import mcsimulation as mc
from marketdata.mdfiller import getLatestStoredPrices
from marketdata.models import AssetPrice, FxRate
from tracker.models import Portfolio, Position


TODAY = timezone.now().date()


def seed_constant_growth_prices(ticker, n_days, g=1.001, start_price=100.0,
                                currency="USD", end=TODAY):
    """Seed n_days+1 daily prices where price[i+1]/price[i] == g exactly-ish."""
    objs = []
    price = start_price
    for i in range(n_days, -1, -1):
        objs.append(AssetPrice(
            ticker=ticker, currency=currency, source="yfinance",
            price=Decimal(str(round(price, 6))),
            date=end - timedelta(days=i),
        ))
        price *= g
    # reverse order of price accumulation: fix by recomputing forward
    AssetPrice.objects.bulk_create(objs, ignore_conflicts=True)


def seed_flat_fx(base, quote, n_days, rate=0.9, end=TODAY):
    objs = [
        FxRate(base=base, quote=quote, rate=Decimal(str(rate)),
               date=end - timedelta(days=i), source="test")
        for i in range(n_days + 1)
    ]
    FxRate.objects.bulk_create(objs, ignore_conflicts=True)


def make_position(user, ticker, currency="USD", qty="10",
                  asset_type="listed_equity"):
    with patch("marketdata.pyfinancedata.getTickerName", return_value=""):
        return Position.objects.create(
            portfolio=user.portfolio, asset_type=asset_type,
            ticker=ticker, currency=currency, quantity=Decimal(qty))


class LatestStoredPricesTests(TestCase):
    def test_returns_latest_by_date(self):
        AssetPrice.objects.create(ticker="AAPL", currency="USD",
                                  price=Decimal("100"), date=TODAY - timedelta(days=2))
        AssetPrice.objects.create(ticker="AAPL", currency="USD",
                                  price=Decimal("110"), date=TODAY)
        self.assertEqual(getLatestStoredPrices({"AAPL"}),
                         {"AAPL": Decimal("110")})

    def test_unknown_ticker_omitted(self):
        self.assertEqual(getLatestStoredPrices({"NOPE"}), {})

    def test_empty_input(self):
        self.assertEqual(getLatestStoredPrices(set()), {})


class GrowthMatrixTests(TestCase):
    def test_shape_and_factor_names(self):
        seed_constant_growth_prices("T1", 600)
        seed_flat_fx("USD", "CHF", 600)
        m, names, err = mc.build_aligned_growth_matrix(
            {"T1"}, {("USD", "CHF")}, lookback_years=5)
        self.assertIsNone(err)
        self.assertEqual(set(names), {"T1", "USD/CHF"})
        # 601 common dates -> 600 growth rows, 2 factors
        self.assertEqual(m.shape, (600, 2))

    def test_growth_values_are_daily_ratios(self):
        seed_constant_growth_prices("T1", 600, g=1.001)
        m, names, err = mc.build_aligned_growth_matrix({"T1"}, set(), 5)
        self.assertIsNone(err)
        np.testing.assert_allclose(m[:, 0], 1.001, rtol=1e-4)

    def test_insufficient_history_is_rejected(self):
        seed_constant_growth_prices("THIN", 50)  # far below 500
        m, names, err = mc.build_aligned_growth_matrix({"THIN"}, set(), 5)
        self.assertIsNone(m)
        self.assertIn("Insufficient overlapping history", err)

    def test_missing_ticker_is_reported(self):
        m, names, err = mc.build_aligned_growth_matrix({"GHOST"}, set(), 5)
        self.assertIsNone(m)
        self.assertIn("GHOST", err)

    def test_intersection_shrinks_to_common_dates(self):
        # T1 every day for 601 days; T2 only every other day
        seed_constant_growth_prices("T1", 1200)
        for i in range(0, 1201, 2):
            AssetPrice.objects.create(
                ticker="T2", currency="USD", price=Decimal("50"),
                date=TODAY - timedelta(days=i))
        m, names, err = mc.build_aligned_growth_matrix({"T1", "T2"}, set(), 5)
        self.assertIsNone(err)
        # ~601 common dates (every other day of 1201) -> ~600 growth rows
        self.assertLess(m.shape[0], 700)
        self.assertGreaterEqual(m.shape[0], mc.MIN_TRADING_DAYS)


class SimulatePositionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="x")
        Portfolio.objects.create(user=self.user, base_currency="CHF")

    def test_deterministic_growth_same_currency(self):
        g = 1.001
        seed_constant_growth_prices("N", 600, g=g, currency="CHF")
        pos = make_position(self.user, "N", currency="CHF", qty="10")

        sim = mc.simulate_position(
            pos, current_price=Decimal("200"), current_fx_rate=None,
            base_ccy="CHF", horizon_years=1, n_paths=50, seed=42)

        self.assertTrue(sim["simulatable"])
        v_now = 200 * 10
        expected_end = v_now * g ** 252
        # constant growth -> every path identical to machine-ish precision
        np.testing.assert_allclose(sim["endings"], expected_end, rtol=1e-6)
        self.assertAlmostEqual(sim["median"], expected_end, delta=expected_end * 1e-6)
        self.assertEqual(sim["prob_loss"], 0.0)
        self.assertEqual(sim["start_value"], float(v_now))
        # paths include day 0 = today
        self.assertEqual(sim["paths"].shape, (50, 253))
        self.assertTrue(np.all(sim["paths"][:, 0] == v_now))

    def test_fx_factor_is_applied(self):
        g_asset, g_fx = 1.001, 0.9995
        seed_constant_growth_prices("U", 600, g=g_asset, currency="USD")
        # FX with constant daily growth factor
        rate = 0.9
        objs = []
        for i in range(600, -1, -1):
            objs.append(FxRate(base="USD", quote="CHF",
                               rate=Decimal(str(round(rate, 8))),
                               date=TODAY - timedelta(days=i), source="test"))
            rate *= g_fx
        FxRate.objects.bulk_create(objs, ignore_conflicts=True)

        pos = make_position(self.user, "U", currency="USD", qty="1")
        sim = mc.simulate_position(
            pos, current_price=Decimal("100"),
            current_fx_rate=Decimal("0.95"),
            base_ccy="CHF", horizon_years=1, n_paths=20, seed=1)

        self.assertTrue(sim["simulatable"])
        v_now = 100 * 1 * 0.95
        expected_end = v_now * (g_asset ** 252) * (g_fx ** 252)
        np.testing.assert_allclose(sim["endings"], expected_end, rtol=1e-5)

    def test_manual_asset_not_simulatable(self):
        pos = Position.objects.create(
            portfolio=self.user.portfolio, asset_type="cash",
            currency="CHF", manual_current_value=Decimal("100"))
        sim = mc.simulate_position(pos, Decimal("1"), None, "CHF")
        self.assertFalse(sim["simulatable"])

    def test_missing_price_not_simulatable(self):
        pos = make_position(self.user, "X", currency="CHF")
        sim = mc.simulate_position(pos, None, None, "CHF")
        self.assertFalse(sim["simulatable"])
        self.assertIn("current price", sim["reason"].lower())

    def test_missing_fx_not_simulatable(self):
        pos = make_position(self.user, "X", currency="USD")
        sim = mc.simulate_position(pos, Decimal("100"), None, "CHF")
        self.assertFalse(sim["simulatable"])
        self.assertIn("USD/CHF", sim["reason"])


class SimulatePortfolioTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="x")
        Portfolio.objects.create(user=self.user, base_currency="CHF")

    def test_thin_history_ticker_is_excluded_not_poisoning(self):
        """The SPCX case: a 19-day IPO must not collapse everyone's window."""
        g = 1.001
        seed_constant_growth_prices("GOOD", 600, g=g, currency="CHF")
        seed_constant_growth_prices("IPO", 19, g=g, currency="CHF")

        good = make_position(self.user, "GOOD", currency="CHF", qty="1")
        ipo = make_position(self.user, "IPO", currency="CHF", qty="1")

        sim = mc.simulate_portfolio(
            [good, ipo],
            price_lookup={"GOOD": Decimal("100"), "IPO": Decimal("10")},
            rate_lookup={}, base_ccy="CHF",
            n_paths=20, seed=7)

        self.assertTrue(sim["simulatable"])
        excluded_labels = {e["label"] for e in sim["excluded"]}
        self.assertIn("IPO", excluded_labels)
        # start value covers ONLY the simulatable position
        self.assertEqual(sim["start_value"], 100.0)
        # and 500+ days of history survived
        self.assertGreaterEqual(sim["days_of_history"], mc.MIN_TRADING_DAYS)

    def test_portfolio_is_sum_of_positions_per_path(self):
        g = 1.001
        seed_constant_growth_prices("A", 600, g=g, currency="CHF")
        seed_constant_growth_prices("B", 600, g=g, currency="CHF")
        a = make_position(self.user, "A", currency="CHF", qty="1")
        b = make_position(self.user, "B", currency="CHF", qty="2")

        sim = mc.simulate_portfolio(
            [a, b],
            price_lookup={"A": Decimal("100"), "B": Decimal("50")},
            rate_lookup={}, base_ccy="CHF",
            n_paths=30, seed=3, keep_paths=True)

        self.assertTrue(sim["simulatable"])
        start = 100 * 1 + 50 * 2
        self.assertEqual(sim["start_value"], float(start))
        expected_end = start * g ** 252
        np.testing.assert_allclose(sim["endings"], expected_end, rtol=1e-6)
        # weights add to 100
        total_weight = sum(p["weight_pct"] for p in sim["per_position"])
        self.assertAlmostEqual(total_weight, 100.0, places=6)
        # paths: day 0 is today's start value
        self.assertTrue(np.all(sim["paths"][:, 0] == start))

    def test_all_positions_excluded_returns_reason(self):
        cash = Position.objects.create(
            portfolio=self.user.portfolio, asset_type="cash",
            currency="CHF", manual_current_value=Decimal("100"))
        sim = mc.simulate_portfolio(
            [cash], price_lookup={}, rate_lookup={}, base_ccy="CHF")
        self.assertFalse(sim["simulatable"])
        self.assertEqual(len(sim["excluded"]), 1)

    def test_missing_fx_rate_excludes_position(self):
        seed_constant_growth_prices("U", 600, currency="USD")
        pos = make_position(self.user, "U", currency="USD", qty="1")
        sim = mc.simulate_portfolio(
            [pos], price_lookup={"U": Decimal("100")},
            rate_lookup={},  # no USD/CHF today
            base_ccy="CHF")
        self.assertFalse(sim["simulatable"])
        self.assertIn("USD/CHF", sim["excluded"][0]["reason"])


class PercentileBandTests(TestCase):
    def test_bands_are_monotone_and_correct_length(self):
        rng = np.random.default_rng(0)
        paths = rng.lognormal(mean=0, sigma=0.1, size=(500, 40)).cumprod(axis=1)
        bands = mc.percentile_bands(paths, levels=(5, 50, 95))
        self.assertEqual(len(bands[50]), 40)
        for day in range(40):
            self.assertLessEqual(bands[5][day], bands[50][day])
            self.assertLessEqual(bands[50][day], bands[95][day])