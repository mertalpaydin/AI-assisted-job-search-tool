"""
Unit tests for the A/B testing and statistical experimentation engine.
Verifies normal distribution math, two-proportion z-tests, Benjamini-Hochberg FDR,
power analysis, and cohort behavioral tests.
"""
import math
import pytest

from job_search.skills.experimentation import (
    ABTestEngine,
    norm_cdf,
    norm_ppf,
    norm_sf,
)


def test_normal_distribution_approximations():
    # CDF tests
    assert math.isclose(norm_cdf(0.0), 0.5, abs_tol=1e-7)
    assert math.isclose(norm_cdf(1.959964), 0.975, abs_tol=1e-4)
    assert math.isclose(norm_cdf(-1.959964), 0.025, abs_tol=1e-4)

    # Inverse CDF (Quantile) tests
    assert math.isclose(norm_ppf(0.5), 0.0, abs_tol=1e-7)
    assert math.isclose(norm_ppf(0.975), 1.95996, abs_tol=1e-4)
    assert math.isclose(norm_ppf(0.025), -1.95996, abs_tol=1e-4)
    assert math.isclose(norm_ppf(0.80), 0.84162, abs_tol=1e-4)


def test_two_proportion_z_test_significant_difference():
    engine = ABTestEngine(alpha=0.05)
    # Group 1 (Applied): 100 / 1000 = 10%
    # Group 2 (Skipped): 180 / 1000 = 18%
    res = engine.two_proportion_z_test(
        skill_name="SQL",
        x1=100, n1=1000,
        x2=180, n2=1000,
    )

    assert res.p_group1 == 0.10
    assert res.p_group2 == 0.18
    assert math.isclose(res.diff, -0.08, abs_tol=1e-5)
    assert res.z_stat < -4.0  # Strongly negative z-score
    assert res.p_value < 0.0001
    assert res.is_significant_raw
    assert "Lag" in res.interpretation
    assert res.ci_lower < res.diff < res.ci_upper
    assert res.statistical_power > 0.99  # High power


def test_two_proportion_z_test_parity():
    engine = ABTestEngine(alpha=0.05)
    # Identical proportions
    res = engine.two_proportion_z_test(
        skill_name="Python",
        x1=250, n1=1000,
        x2=250, n2=1000,
    )

    assert math.isclose(res.diff, 0.0, abs_tol=1e-7)
    assert math.isclose(res.z_stat, 0.0, abs_tol=1e-7)
    assert math.isclose(res.p_value, 1.0, abs_tol=1e-7)
    assert not res.is_significant_raw
    assert res.interpretation == "Statistical Parity"


def test_cohens_h_effect_size():
    engine = ABTestEngine()
    res = engine.two_proportion_z_test(
        skill_name="TestSkill",
        x1=200, n1=1000,  # 20%
        x2=100, n2=1000,  # 10%
    )
    # Cohen's h for 20% vs 10% is ~0.284
    assert res.cohens_h > 0.25
    assert res.cohens_h < 0.35


def test_benjamini_hochberg_fdr_correction():
    engine = ABTestEngine(alpha=0.05)

    # Create 5 hypothesis tests with varied p-values
    tests = [
        engine.two_proportion_z_test("Skill A", x1=50, n1=1000, x2=150, n2=1000),   # Very small p (e.g. ~1e-12)
        engine.two_proportion_z_test("Skill B", x1=80, n1=1000, x2=120, n2=1000),   # Moderate p (~0.003)
        engine.two_proportion_z_test("Skill C", x1=90, n1=1000, x2=115, n2=1000),   # Borderline p (~0.065)
        engine.two_proportion_z_test("Skill D", x1=95, n1=1000, x2=100, n2=1000),   # Non-significant
        engine.two_proportion_z_test("Skill E", x1=100, n1=1000, x2=100, n2=1000),  # p = 1.0
    ]

    adjusted = engine.apply_benjamini_hochberg(tests)

    # Verify that FDR-adjusted p-values are >= raw p-values
    for r in adjusted:
        assert r.p_adjusted_fdr >= r.p_value or math.isclose(r.p_adjusted_fdr, r.p_value, abs_tol=1e-7)

    # Skill A and B should remain significant after FDR
    assert adjusted[0].is_significant_fdr
    assert adjusted[1].is_significant_fdr
    # Skill D and E should remain non-significant
    assert not adjusted[3].is_significant_fdr
    assert not adjusted[4].is_significant_fdr


def test_cohort_shift_uplift():
    engine = ABTestEngine(alpha=0.05)
    # Pre-period (e.g. May): 1 / 100 = 1%
    # Post-period (e.g. Oct): 8 / 100 = 8%
    res, uplift = engine.test_cohort_shift(
        skill_name="A/B Testing",
        pre_applied=1, pre_total=100,
        post_applied=8, post_total=100,
        cohort_pre_name="May 2026",
        cohort_post_name="October 2026",
    )

    # 1% to 8% is a 700% relative uplift
    assert math.isclose(uplift, 700.0, abs_tol=1e-2)
    assert res.diff > 0.05
    assert res.z_stat > 2.0
    assert res.p_value < 0.05
    assert res.is_significant_raw
