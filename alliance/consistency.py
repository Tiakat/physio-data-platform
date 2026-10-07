#!/usr/bin/env python3
"""
consistency.py — Cross-variable physiological consistency checks.

These are deterministic, interpretable rules that catch parser errors,
unit mismatches, and device problems. They run AFTER individual QC
and BEFORE any ML model.

Key relationships (from K's Dräger research):
  MV ≈ VT × RR / 1000          (minute ventilation from tidal volume)
  PIP ≥ PPLAT ≥ PEEP            (airway pressure ordering)
  E ≈ 1 / Cdyn                  (elastance vs compliance, unit-harmonized)
  RR ≈ RRMAND + RRSPON          (total = mandatory + spontaneous)
"""

import pandas as pd
import numpy as np


def check_mv_consistency(df: pd.DataFrame, vt_col="VT", rr_col="RR",
                         mve_col="MVE", tol: float = 0.30) -> pd.Series:
    """
    MV_expected = VT(mL) × RR(/min) / 1000 → L/min
    Returns boolean Series: True = consistent, False = violation.
    Tolerance: 30% relative error (devices use different averaging windows).
    """
    if not all(c in df.columns for c in (vt_col, rr_col, mve_col)):
        return pd.Series(True, index=df.index)

    vt = df[vt_col]
    rr = df[rr_col]
    mve = df[mve_col]

    expected = vt * rr / 1000.0
    # Only check where all three are valid and positive
    valid = (vt > 0) & (rr > 0) & (mve > 0) & expected.notna() & mve.notna()
    rel_err = (mve - expected).abs() / mve.clip(lower=1e-6)

    result = pd.Series(True, index=df.index)
    result[valid & (rel_err > tol)] = False
    return result


def check_pressure_ordering(df: pd.DataFrame, pip_col="PIP",
                            pplat_col="PPLAT", peep_col="PEEP") -> pd.DataFrame:
    """
    PIP ≥ PPLAT ≥ PEEP (soft constraints — flag, don't delete).
    Returns DataFrame with violation flags.
    """
    out = pd.DataFrame(index=df.index)
    out["pip_ge_pplat"] = True
    out["pplat_ge_peep"] = True

    cols = [c for c in (pip_col, pplat_col, peep_col) if c in df.columns]
    if len(cols) < 2:
        return out

    if pip_col in df.columns and pplat_col in df.columns:
        valid = df[pip_col].notna() & df[pplat_col].notna()
        out.loc[valid, "pip_ge_pplat"] = df.loc[valid, pip_col] >= df.loc[valid, pplat_col]

    if pplat_col in df.columns and peep_col in df.columns:
        valid = df[pplat_col].notna() & df[peep_col].notna()
        out.loc[valid, "pplat_ge_peep"] = df.loc[valid, pplat_col] >= df.loc[valid, peep_col]

    return out


def check_elastance_compliance(df: pd.DataFrame, e_col="ELASTANCE",
                               cdyn_col="CDYN", tol: float = 0.50) -> pd.Series:
    """
    E × Cdyn ≈ 1 (after unit harmonization to hPa/L and L/hPa).
    Note: assumes E in hPa/L and Cdyn in mL/hPa → convert Cdyn to L/hPa.
    Returns boolean: True = consistent.
    """
    if not all(c in df.columns for c in (e_col, cdyn_col)):
        return pd.Series(True, index=df.index)

    e = df[e_col]
    cdyn_l_per_hpa = df[cdyn_col] / 1000.0  # mL/hPa → L/hPa

    valid = (e > 0) & (cdyn_l_per_hpa > 0) & e.notna() & cdyn_l_per_hpa.notna()
    product = e * cdyn_l_per_hpa
    # product should be ≈ 1
    rel_err = (product - 1.0).abs()

    result = pd.Series(True, index=df.index)
    result[valid & (rel_err > tol)] = False
    return result


def check_rr_components(df: pd.DataFrame, rr_col="RR",
                        rrmand_col="RRMAND", rrspon_col="RRSPON",
                        tol: float = 0.25) -> pd.Series:
    """
    RR ≈ RRMAND + RRSPON (device breath classification may differ slightly).
    """
    if not all(c in df.columns for c in (rr_col, rrmand_col, rrspon_col)):
        return pd.Series(True, index=df.index)

    rr = df[rr_col]
    expected = df[rrmand_col] + df[rrspon_col]

    valid = rr.notna() & expected.notna() & (rr > 0)
    rel_err = (rr - expected).abs() / rr.clip(lower=1e-6)

    result = pd.Series(True, index=df.index)
    result[valid & (rel_err > tol)] = False
    return result


def run_all_consistency(df: pd.DataFrame) -> pd.DataFrame:
    """Run all available consistency checks. Returns flags DataFrame."""
    out = pd.DataFrame(index=df.index)
    out["mv_consistent"] = check_mv_consistency(df)
    out["elastance_consistent"] = check_elastance_compliance(df)
    out["rr_consistent"] = check_rr_components(df)

    pressure = check_pressure_ordering(df)
    out = pd.concat([out, pressure], axis=1)

    # Summary: any violation?
    check_cols = [c for c in out.columns]
    out["any_violation"] = ~out[check_cols].all(axis=1)
    return out


if __name__ == "__main__":
    # Self-test with realistic values
    df = pd.DataFrame({
        "VT":  [500, 500, 500, 450],
        "RR":  [12,  12,  12,  14],
        "MVE":  [6.0, 6.1, 25.0, 6.3],  # row 2 is inconsistent
        "PIP":  [18,  20,  15,  22],
        "PPLAT": [14, 15,  18,  16],    # row 2: PPLAT > PIP (violation)
        "PEEP": [5,   5,   5,   5],
    })
    result = run_all_consistency(df)
    print(result.to_string())
    print("\nSelf-test: row 2 should show mv_consistent=False, pip_ge_pplat=False")
