"""Probability and expected-value engine."""
from fractions import Fraction

import pytest

from atlas.analytics.probability import (MIN_VALIDATION, PredictionLog, breakeven_bp, brier, calibration,
                                         expected_value, log_loss, wilson)
from atlas.money import Money, kalshi_taker_fee

M = Money.parse


def test_expected_value_is_exact_and_rounded_down():
    fee = kalshi_taker_fee(2, M("0.88"))                       # $0.02
    ev = expected_value(9_190, M("0.88"), 2, fee)                # 0.919*2 - (1.76 + 0.02)
    assert ev == M("0.058")
    # exact rational check, floor never overstates
    exact = Fraction(9_190, 10_000) * 2 - Fraction(178, 100)
    assert Fraction(ev.units, 1_000_000) <= exact < Fraction(ev.units + 1, 1_000_000)


def test_expected_value_counts_slippage_and_can_be_negative():
    assert expected_value(8_800, M("0.88"), 1, M("0.01"), slippage=M("0.01")) == M("-0.02")


def test_breakeven_probability():
    fee = kalshi_taker_fee(2, M("0.88"))
    be = breakeven_bp(M("0.88"), 2, fee)
    assert be == 8_900                                            # 1.78 / 2 = 0.89
    assert expected_value(be, M("0.88"), 2, fee).units >= 0
    assert expected_value(be - 1, M("0.88"), 2, fee).units < 0


def test_scores():
    pairs = [(9000, 1), (9000, 1), (1000, 0), (5000, 1)]
    assert brier(pairs) == pytest.approx((0.01 + 0.01 + 0.01 + 0.25) / 4)
    assert log_loss([(5000, 1)]) == pytest.approx(0.6931, abs=1e-4)


def test_wilson_interval():
    lo, hi = wilson(70, 100)
    assert 0.60 < lo < 0.62 and 0.78 < hi < 0.79
    assert wilson(0, 0) == (0.0, 1.0)


def test_calibration_buckets_flag_inconsistency():
    pairs = [(9500, 1)] * 50 + [(9500, 0)] * 50                  # predicted 95%, happened 50%
    (b,) = calibration(pairs)
    assert b.n == 100 and not b.consistent


def test_prediction_log_scores_and_validation(tmp_path):
    log = PredictionLog(tmp_path / "p.db", clock=lambda: 1.0)
    with pytest.raises(ValueError):
        log.record(strategy="s", model_version="v1", market="M0", market_type="btc15m", side="yes",
                   prob_bp=5000, market_prob_bp=5000, sources=[])
    for i in range(10):
        log.record(strategy="s", model_version="v1", market=f"M{i}", market_type="btc15m", side="yes",
                   prob_bp=8000, market_prob_bp=7000, sources=["kalshi:orderbook", "btc:median"])
        log.resolve(f"M{i}", "yes" if i < 8 else "no")
    sc = log.scorecard(strategy="s")
    assert sc.n == 10 and sc.brier == pytest.approx(0.2 * 0.2 * 0.8 + 0.8 * 0.8 * 0.2)
    assert not sc.validated and "UNVALIDATED" in sc.summary() and MIN_VALIDATION > 10
    assert sc.market_brier is not None
