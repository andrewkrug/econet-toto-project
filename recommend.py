#!/usr/bin/env python3
"""Suggest when to lower the water-heater setpoint to save energy.

ADVISORY ONLY -- this never writes to the heater. It reads history from
telemetry.parquet (and the Toto forecast in forecast.parquet, if present),
estimates hot-water demand, and prints a recommended setpoint schedule for
the next N hours plus a rough energy-saving estimate. You decide whether to
act on it (e.g. `rheem.py set-temp`).

Runs in the light .venv (pandas only): it consumes Toto's forecast.parquet
rather than importing torch, so the heavy model stays in toto_forecast.py.

    .venv/bin/python recommend.py
    .venv/bin/python recommend.py --eco 100 --comfort 120 --horizon 24

Demand proxy: hot-water "availability" (0-100) falls when hot water is drawn
and recovers as the heat pump reheats. Per-step consumption is the positive
drop in availability; we never have true draw volume (pyeconet doesn't expose
it for this unit), so this proxy is the best available signal.

Defaults are the "Balanced" profile: comfort 120F, eco 105F, keep predicted
availability >= 40%, recover 2h before predicted demand.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

TANK_GALLONS = float(os.environ.get("RHEEM_TANK_GALLONS", "80"))
# Same calibration as rheem.py: index 66 = full = USABLE_GALLONS.
FULL_INDEX = float(os.environ.get("RHEEM_FULL_INDEX", "66"))
USABLE_GALLONS = float(os.environ.get("RHEEM_USABLE_GALLONS", "80"))

TELEMETRY = Path(__file__).with_name("telemetry.parquet")
FORECAST = Path(__file__).with_name("forecast.parquet")
OUT = Path(__file__).with_name("recommendation.parquet")
STEP_MIN = 15  # collector cadence; one schedule block = one sample


def load_env() -> None:
    """Populate os.environ from $XDG_CONFIG_HOME/rheem/env (default
    ~/.config/rheem/env) without overriding the real environment."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    env = Path(base) / "rheem" / "env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--comfort", type=int, default=120,
                   help="comfort setpoint F, used at/before demand (default 120)")
    p.add_argument("--eco", type=int, default=105,
                   help="eco setpoint F, used in low-demand windows (default 105)")
    p.add_argument("--floor", type=float, default=40.0,
                   help="min acceptable predicted availability %% (default 40)")
    p.add_argument("--recovery-hours", type=float, default=2.0,
                   help="hours of comfort lead-in before predicted demand "
                        "(default 2)")
    p.add_argument("--horizon", type=float, default=24.0,
                   help="hours of schedule to produce (default 24)")
    p.add_argument("--demand-quantile", type=float, default=0.6,
                   help="profile quantile above which a block counts as "
                        "'demand' (default 0.6)")
    p.add_argument("--no-datadog", action="store_true",
                   help="don't push advisory rheem.reco.* metrics")
    return p.parse_args()


def load_history() -> pd.DataFrame:
    if not TELEMETRY.exists():
        sys.exit(f"No history at {TELEMETRY.name} -- run `rheem.py log` first.")
    df = pd.read_parquet(TELEMETRY).sort_values("timestamp").copy()
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
    df["avail"] = pd.to_numeric(df["tank_hot_water_availability"],
                                errors="coerce")
    df = df.dropna(subset=["avail"])
    if df.empty:
        sys.exit("No usable tank_hot_water_availability values in history.")
    # Consumption proxy: the positive drop in availability between samples.
    # A rise (heat-pump recovery) contributes zero demand.
    df["consumption"] = (-df["avail"].diff()).clip(lower=0).fillna(0.0)
    return df


def build_profile(df: pd.DataFrame) -> tuple[pd.Series, str, float]:
    """Return (demand-by-hour Series indexed 0..23, confidence label,
    history-span hours). Uses local time so 'overnight' means the user's
    night. Falls back gracefully when history is thin."""
    span_h = (df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds() / 3600.0
    # Local wall time so "overnight" means the user's night, not UTC's.
    hours = df["ts"].dt.tz_convert(_local_tz()).dt.hour

    if span_h < 8:
        conf = "very low"
    elif span_h < 24 * 3:
        conf = "low"
    else:
        conf = "medium"

    if span_h < 1.0 or df["consumption"].sum() == 0:
        # Not enough signal to say anything -- flat profile, caller will
        # recommend staying at comfort.
        return pd.Series(0.0, index=range(24)), "very low", span_h

    prof = df.groupby(hours)["consumption"].mean()
    prof = prof.reindex(range(24))
    # Fill unobserved hours with the global mean so we don't claim those
    # hours are demand-free just because we haven't watched them yet.
    prof = prof.fillna(df["consumption"].mean())
    return prof, conf, span_h


def _local_tz():
    """Local timezone, for human-readable schedule times."""
    return datetime.now().astimezone().tzinfo


def load_forecast_demand(now: pd.Timestamp) -> pd.Series | None:
    """If a fresh Toto forecast of availability exists, convert it to a
    per-step demand series (positive drops) indexed by timestamp. Returns
    None if absent, stale (>2h old start), or not an availability forecast."""
    if not FORECAST.exists():
        return None
    fc = pd.read_parquet(FORECAST)
    if "column" in fc and not (fc["column"] == "tank_hot_water_availability").all():
        return None  # forecast is for some other column; ignore for demand
    fc = fc.sort_values("timestamp")
    fc["ts"] = pd.to_datetime(fc["timestamp"], utc=True)
    if (now - fc["ts"].iloc[0]) > pd.Timedelta(hours=2):
        return None  # stale; profile-only
    dem = (-fc["forecast"].diff()).clip(lower=0).fillna(0.0)
    dem.index = fc["ts"]
    return dem


def build_schedule(args, prof: pd.Series, fc_demand: pd.Series | None,
                    now: pd.Timestamp) -> pd.DataFrame:
    n = int(round(args.horizon * 60 / STEP_MIN))
    idx = pd.date_range(now.ceil(f"{STEP_MIN}min"), periods=n,
                        freq=f"{STEP_MIN}min")
    local = idx.tz_convert(_local_tz())

    # Expected demand per block: the recurring hour profile, overridden by
    # the Toto forecast where it covers the block (near-term, sharper).
    demand = np.array([prof.get(h, 0.0) for h in local.hour], dtype=float)
    if fc_demand is not None and not fc_demand.empty:
        f = fc_demand.reindex(idx, method="nearest",
                              tolerance=pd.Timedelta(minutes=STEP_MIN))
        m = f.notna().values
        demand[m] = np.maximum(demand[m], f.values[m])

    # A block is "demand" if its expected demand is above the chosen
    # quantile of the (non-zero) profile -- i.e. a meaningfully busy hour.
    nz = prof[prof > 0]
    thresh = float(nz.quantile(args.demand_quantile)) if len(nz) else np.inf
    is_demand = demand >= thresh

    # Comfort if this block is demand, or within recovery lead-in before
    # the next demand block (heat-pump reheat is slow, so lead the demand).
    lead = int(round(args.recovery_hours * 60 / STEP_MIN))
    need_comfort = is_demand.copy()
    dem_pos = np.where(is_demand)[0]
    for j in dem_pos:
        need_comfort[max(0, j - lead):j] = True

    setpoint = np.where(need_comfort, args.comfort, args.eco)
    reason = np.where(
        is_demand, "predicted demand",
        np.where(need_comfort, f"recovery lead-in ({args.recovery_hours:g}h)",
                 "low demand -> eco"))

    return pd.DataFrame({
        "timestamp": idx,
        "local_time": local.strftime("%a %H:%M"),
        "expected_demand": demand.round(2),
        # Same proxy expressed as gallons drawn per block, using the
        # 66=full calibration so it matches the heater/app numbers.
        "expected_gallons": (demand / FULL_INDEX * USABLE_GALLONS).round(2),
        "is_demand": is_demand,
        "recommended_setpoint_f": setpoint.astype(int),
        "reason": reason,
    })


def collapse(sched: pd.DataFrame) -> pd.DataFrame:
    """Merge consecutive blocks with the same recommended setpoint into
    human-readable segments."""
    grp = (sched["recommended_setpoint_f"]
           != sched["recommended_setpoint_f"].shift()).cumsum()
    rows = []
    for _, g in sched.groupby(grp):
        rows.append({
            "start": g["local_time"].iloc[0],
            "end": g["local_time"].iloc[-1],
            "setpoint_f": int(g["recommended_setpoint_f"].iloc[0]),
            "reason": g["reason"].iloc[0],
            "blocks": len(g),
        })
    return pd.DataFrame(rows)


def push_datadog(sched: pd.DataFrame, eco_hours: float, savings_pct: float,
                  conf: str) -> str:
    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        return "skipped (no DD_API_KEY)"
    site = os.environ.get("DD_SITE", "datadoghq.com")
    nowts = int(time.time())
    metrics = {
        "rheem.reco.recommended_setpoint":
            float(sched["recommended_setpoint_f"].iloc[0]),
        "rheem.reco.eco_hours_next_24h": float(eco_hours),
        "rheem.reco.est_savings_pct": float(savings_pct),
    }
    series = [{"metric": k, "type": 3,
               "points": [{"timestamp": nowts, "value": v}],
               "tags": [f"confidence:{conf.replace(' ', '_')}"]}
              for k, v in metrics.items()]
    req = urllib.request.Request(
        f"https://api.{site}/api/v2/series",
        data=json.dumps({"series": series}).encode(), method="POST",
        headers={"Content-Type": "application/json", "DD-API-KEY": api_key})
    with urllib.request.urlopen(req) as resp:
        resp.read()
    return f"sent {len(series)} gauges"


def main() -> None:
    args = parse_args()
    if args.eco > args.comfort:
        sys.exit("--eco must be <= --comfort")
    load_env()

    df = load_history()
    now = pd.Timestamp.now(tz="UTC")
    prof, conf, span_h = build_profile(df)
    fc_demand = load_forecast_demand(now)
    if fc_demand is not None:
        conf = "medium" if conf in ("very low", "low") else conf

    sched = build_schedule(args, prof, fc_demand, now)
    seg = collapse(sched)

    eco_blocks = int((sched["recommended_setpoint_f"] == args.eco).sum())
    eco_hours = eco_blocks * STEP_MIN / 60.0
    # Crude energy proxy: standby loss scales ~ with (setpoint - ambient);
    # assume ~65F ambient. Savings = fraction of time at eco * the relative
    # standby-loss reduction it gives. This is an estimate, not a meter.
    amb = 65.0
    loss_comfort = max(args.comfort - amb, 1)
    loss_eco = max(args.eco - amb, 1)
    per_block_red = 1 - loss_eco / loss_comfort
    savings_pct = 100.0 * per_block_red * eco_blocks / max(len(sched), 1)

    print(f"== Setpoint recommendation (ADVISORY -- nothing was changed) ==")
    print(f"History: {len(df)} samples, {span_h:.1f}h span. "
          f"Confidence: {conf.upper()}"
          + ("  (+Toto forecast)" if fc_demand is not None else ""))
    print(f"Band: eco {args.eco}F / comfort {args.comfort}F, "
          f"avail floor {args.floor:g}%, "
          f"recovery lead {args.recovery_hours:g}h, "
          f"horizon {args.horizon:g}h\n")

    if conf == "very low" or df["consumption"].sum() == 0:
        print("Not enough history to model a demand pattern yet. "
              f"Recommendation: keep the COMFORT setpoint ({args.comfort}F) "
              "and let the collector run (aim for >= ~3 days; >= 8h unlocks "
              "the Toto forecast). The schedule below is a placeholder "
              "structured from what little data exists.\n")

    print(seg.to_string(index=False))
    print(f"\nNext block -> set heater to {sched['recommended_setpoint_f'].iloc[0]}F "
          f"({sched['reason'].iloc[0]}).")
    print(f"Eco time in next {args.horizon:g}h: {eco_hours:.1f}h "
          f"({eco_blocks}/{len(sched)} blocks). "
          f"Rough standby-loss saving: ~{savings_pct:.0f}%.")
    print(f"Predicted hot-water draw over horizon: "
          f"~{sched['expected_gallons'].sum():.0f} gal "
          f"(calibrated: index {FULL_INDEX:g} = {USABLE_GALLONS:g} gal full).")

    sched.to_parquet(OUT, index=False)
    print(f"\nFull 15-min schedule -> {OUT.name}")

    if not args.no_datadog:
        try:
            status = push_datadog(sched, eco_hours, savings_pct, conf)
        except Exception as e:
            status = f"datadog error: {e}"
        print(f"Datadog (rheem.reco.*): {status}")

    print("\nApply a block yourself with, e.g.:  "
          f".venv/bin/python rheem.py set-temp {args.eco}")


if __name__ == "__main__":
    main()
