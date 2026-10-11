"""
Statistical experimentation engine for A/B testing, hypothesis testing,
multiple testing correction (FDR), power analysis, and longitudinal behavior evaluation.
Zero AI utilization. 100% mathematically rigorous.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


# Standard normal critical values
Z_ALPHA_005 = 1.959963984540054  # Two-tailed alpha = 0.05
Z_POWER_080 = 0.841621233572914  # Beta = 0.20 -> 80% power


def norm_cdf(z: float) -> float:
    """Cumulative distribution function for standard normal distribution N(0, 1)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_sf(z: float) -> float:
    """Survival function (1 - CDF) for standard normal distribution."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def norm_ppf(p: float) -> float:
    """
    Percent point function (quantile / inverse CDF) for N(0, 1).
    Acklam's approximation with high precision (error < 1.15e-9).
    """
    if p <= 0.0 or p >= 1.0:
        raise ValueError("p must be strictly between 0 and 1")

    # Coefficients in rational approximations
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)

    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5])*q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)


@dataclass
class HypothesisTestResult:
    """Complete results of a two-sample proportion hypothesis test."""
    skill_name: str
    n_group1: int  # e.g. Applied total
    x_group1: int  # e.g. Applied with skill
    p_group1: float
    n_group2: int  # e.g. Skipped total
    x_group2: int  # e.g. Skipped with skill
    p_group2: float
    diff: float  # p_group1 - p_group2 (or p_skipped - p_applied depending on perspective)
    ci_lower: float
    ci_upper: float
    z_stat: float
    p_value: float
    p_adjusted_fdr: float = 1.0
    cohens_h: float = 0.0
    statistical_power: float = 0.0
    mde_abs: float = 0.0
    is_significant_raw: bool = False
    is_significant_fdr: bool = False
    interpretation: str = "Parity"


class ABTestEngine:
    """
    Engine for conducting hypothesis testing, multiple testing corrections,
    power analysis, and behavioral experimentation across skills data.
    """

    def __init__(self, alpha: float = 0.05, target_power: float = 0.80):
        self.alpha = alpha
        self.target_power = target_power
        self.z_alpha = norm_ppf(1.0 - alpha / 2.0)
        self.z_beta = norm_ppf(target_power)

    def two_proportion_z_test(
        self,
        skill_name: str,
        x1: int,
        n1: int,
        x2: int,
        n2: int,
        label1: str = "Applied",
        label2: str = "Skipped",
    ) -> HypothesisTestResult:
        """
        Run a two-sample hypothesis test comparing proportion in group 1 vs group 2.
        H0: p1 == p2
        H1: p1 != p2
        """
        if n1 <= 0 or n2 <= 0:
            raise ValueError("Sample sizes n1 and n2 must be positive")

        p1 = x1 / n1
        p2 = x2 / n2
        diff = p1 - p2

        # Pooled proportion under H0
        p_pool = (x1 + x2) / (n1 + n2)

        # Standard errors
        se_pool = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n1 + 1.0 / n2)) if 0.0 < p_pool < 1.0 else 0.0
        se_diff = math.sqrt((p1 * (1.0 - p1) / n1) + (p2 * (1.0 - p2) / n2))

        # Z-statistic
        z_stat = (diff / se_pool) if se_pool > 0.0 else 0.0

        # Two-tailed p-value
        p_val = 2.0 * norm_sf(abs(z_stat)) if se_pool > 0.0 else 1.0
        p_val = min(1.0, max(0.0, p_val))

        # 95% Confidence Interval for difference
        ci_lower = diff - self.z_alpha * se_diff
        ci_upper = diff + self.z_alpha * se_diff

        # Cohen's h effect size: 2 * (arcsin(sqrt(p1)) - arcsin(sqrt(p2)))
        h = 2.0 * (math.asin(math.sqrt(max(0.0, min(1.0, p1)))) - math.asin(math.sqrt(max(0.0, min(1.0, p2)))))

        # Power calculation for observed difference
        power = 0.0
        if se_diff > 0.0 and abs(diff) > 0.0:
            z_score_power = (abs(diff) / se_diff) - self.z_alpha
            power = norm_cdf(z_score_power)

        # Minimum Detectable Effect at target power (80%)
        mde_abs = (self.z_alpha + self.z_beta) * se_diff if se_diff > 0.0 else 0.0

        is_sig_raw = p_val < self.alpha

        # Interpretation
        if is_sig_raw:
            if diff < 0.0:
                interp = f"Statistically Significant Lag ({label2} > {label1})"
            else:
                interp = f"Statistically Significant Strength ({label1} > {label2})"
        else:
            if abs(diff) >= 0.02:
                interp = "Directional Difference (Underpowered)"
            else:
                interp = "Statistical Parity"

        return HypothesisTestResult(
            skill_name=skill_name,
            n_group1=n1,
            x_group1=x1,
            p_group1=p1,
            n_group2=n2,
            x_group2=x2,
            p_group2=p2,
            diff=diff,
            ci_lower=ci_lower,
            ci_upper=ci_upper,
            z_stat=z_stat,
            p_value=p_val,
            p_adjusted_fdr=p_val,
            cohens_h=h,
            statistical_power=power,
            mde_abs=mde_abs,
            is_significant_raw=is_sig_raw,
            is_significant_fdr=is_sig_raw,
            interpretation=interp,
        )

    def apply_benjamini_hochberg(
        self, results: List[HypothesisTestResult]
    ) -> List[HypothesisTestResult]:
        """
        Apply the Benjamini-Hochberg (BH) procedure to control False Discovery Rate (FDR).
        Adjusts p-values and sets is_significant_fdr.
        """
        if not results:
            return []

        m = len(results)
        # Sort results by raw p-value ascending
        indexed_results = sorted(enumerate(results), key=lambda x: x[1].p_value)

        # Calculate adjusted p-values
        # q_(i) = min(1, min_{j >= i} (m / j * p_(j)))
        raw_pvals = [res.p_value for _, res in indexed_results]
        q_vals = [0.0] * m

        running_min = 1.0
        for i in range(m - 1, -1, -1):
            rank = i + 1
            adjusted = (m / rank) * raw_pvals[i]
            running_min = min(running_min, adjusted)
            q_vals[i] = min(1.0, max(0.0, running_min))

        # Update each result
        updated_results = list(results)
        for sorted_idx, (orig_idx, _) in enumerate(indexed_results):
            res = updated_results[orig_idx]
            q = q_vals[sorted_idx]
            res.p_adjusted_fdr = q
            res.is_significant_fdr = q < self.alpha
            if res.is_significant_fdr:
                if res.diff < 0.0:
                    res.interpretation = "Statistically Significant Lag (FDR Controlled)"
                else:
                    res.interpretation = "Statistically Significant Strength (FDR Controlled)"
            elif res.is_significant_raw:
                res.interpretation = "Significant Raw (FDR Rejected / Potential False Positive)"

        return updated_results

    def test_cohort_shift(
        self,
        skill_name: str,
        pre_applied: int,
        pre_total: int,
        post_applied: int,
        post_total: int,
        cohort_pre_name: str = "Cohort Pre",
        cohort_post_name: str = "Cohort Post",
    ) -> Tuple[HypothesisTestResult, float]:
        """
        Test whether the candidate's application rate to roles requiring a specific skill
        shifted significantly between two time periods.
        Returns (test_result, relative_uplift).
        """
        res = self.two_proportion_z_test(
            skill_name=skill_name,
            x1=post_applied,
            n1=post_total,
            x2=pre_applied,
            n2=pre_total,
            label1=cohort_post_name,
            label2=cohort_pre_name,
        )

        p_pre = pre_applied / pre_total if pre_total > 0 else 0.0
        p_post = post_applied / post_total if post_total > 0 else 0.0
        relative_uplift = ((p_post - p_pre) / p_pre) * 100.0 if p_pre > 0.0 else 0.0

        return res, relative_uplift
