#!/usr/bin/env python3
"""Forecast heater telemetry with Datadog's Toto 2.0 foundation model.

Read-only toward the heater: loads history from telemetry.parquet, forecasts
a numeric column, writes the prediction to forecast.parquet, and prints
quantiles.

It also ships a small set of *Toto* metrics to Datadog (`rheem.toto.*`),
stamped at the current time -- a summary of the forecast and the run itself,
NOT the future-dated series (the metrics intake rejects future timestamps).
This is a no-op if DD_API_KEY is unset. Credentials/site come from the
environment or ~/.config/rheem/env (override the dir with $XDG_CONFIG_HOME).

Run with the dedicated 3.12 venv (Toto's deps don't build on 3.14):
    .venv-toto/bin/python toto_forecast.py [column] [--horizon N]

Default column: tank_hot_water_availability  (steps are 15-min samples).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd
import torch
from toto2 import Toto2Model

PARQUET = Path(__file__).with_name("telemetry.parquet")
OUT = Path(__file__).with_name("forecast.parquet")
MODEL = "Datadog/Toto-2.0-22m"


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


def send_toto_metrics(metrics: dict[str, float], tags: list[str]) -> str:
    """Submit rheem.toto.* gauges to Datadog at the current time via the v2
    metrics intake (urllib, no extra deps -- Toto's venv is minimal). No-op
    (returns the reason) when DD_API_KEY is unset, so forecasting still works
    without Datadog."""
    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        return "skipped (no DD_API_KEY)"
    site = os.environ.get("DD_SITE", "datadoghq.com")
    now = int(time.time())
    series = [
        {
            "metric": name,
            "type": 3,  # 3 = gauge in the v2 intake
            "points": [{"timestamp": now, "value": float(value)}],
            "tags": tags,
        }
        for name, value in metrics.items()
    ]
    req = urllib.request.Request(
        f"https://api.{site}/api/v2/series",
        data=json.dumps({"series": series}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "DD-API-KEY": api_key},
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()
    return f"sent {len(series)} gauges"


def main() -> None:
    args = [a for a in sys.argv[1:]]
    horizon = 96  # 96 * 15min = 24h ahead
    if "--horizon" in args:
        i = args.index("--horizon")
        horizon = int(args[i + 1])
        del args[i:i + 2]
    column = args[0] if args else "tank_hot_water_availability"

    load_env()
    started = time.monotonic()

    if not PARQUET.exists():
        sys.exit(f"No history yet at {PARQUET.name} -- run `rheem.py log` first.")
    df = pd.read_parquet(PARQUET).sort_values("timestamp")
    if column not in df.columns:
        sys.exit(f"Column '{column}' not in {list(df.columns)}")
    s = pd.to_numeric(df[column], errors="coerce").dropna().astype("float32")
    PATCH = 32  # Toto 2.0 requires context length to be a multiple of 32
    if len(s) < PATCH:
        sys.exit(f"Only {len(s)} usable points for '{column}'; Toto needs "
                 f">= {PATCH} (~{PATCH * 15 / 60:.0f}h of 15-min data). "
                 f"Let the collector run longer.")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Loading {MODEL} on {device} ... ({len(s)} history points)")
    model = Toto2Model.from_pretrained(MODEL).to(device).eval()

    # Left-pad up to a multiple of PATCH and mask the pad as missing, so we
    # keep every real observation (no trimming) and the patch math is valid.
    vals = s.values
    pad = (-len(vals)) % PATCH
    padded = torch.zeros(1, 1, len(vals) + pad, device=device)
    padded[0, 0, pad:] = torch.tensor(vals, device=device)
    target_mask = torch.zeros_like(padded, dtype=torch.bool)
    target_mask[0, 0, pad:] = True
    series_ids = torch.zeros(1, 1, dtype=torch.long, device=device)

    with torch.no_grad():
        quantiles = model.forecast(
            {"target": padded, "target_mask": target_mask,
             "series_ids": series_ids},
            horizon=horizon,
            decode_block_size=768,
            has_missing_values=bool(pad),
        )

    # quantiles: (n_quantiles, batch, variables, horizon). Take the median
    # quantile (middle index) plus the outer band for uncertainty.
    arr = quantiles.detach().cpu().float().numpy()
    nq = arr.shape[0]
    median = arr[nq // 2, 0, 0, :]
    low = arr[0, 0, 0, :]
    high = arr[-1, 0, 0, :]
    last_ts = pd.to_datetime(df["timestamp"].iloc[-1])
    idx = pd.date_range(last_ts, periods=len(median) + 1, freq="15min")[1:]
    out = pd.DataFrame({"timestamp": idx, "column": column,
                        "forecast": median, "lo": low, "hi": high})
    out.to_parquet(OUT, index=False)
    print(f"\nForecast for '{column}', {len(out)} steps "
          f"({len(out) * 15 / 60:.1f}h) -> {OUT.name}")
    print(out.head(8).to_string(index=False))
    print("...")
    print(out.tail(3).to_string(index=False))
    # Ship a Toto run/forecast summary to Datadog, stamped *now* (the raw
    # future-dated series still isn't pushed -- the intake rejects it).
    run_seconds = time.monotonic() - started
    toto_metrics = {
        "rheem.toto.run": 1,
        "rheem.toto.run_seconds": run_seconds,
        "rheem.toto.history_points": float(len(s)),
        "rheem.toto.horizon_steps": float(horizon),
        "rheem.toto.horizon_hours": len(out) * 15 / 60,
        "rheem.toto.forecast_next": float(median[0]),
        "rheem.toto.forecast_horizon_end": float(median[-1]),
        "rheem.toto.forecast_lo_end": float(low[-1]),
        "rheem.toto.forecast_hi_end": float(high[-1]),
        "rheem.toto.forecast_min": float(median.min()),
        "rheem.toto.forecast_max": float(median.max()),
        "rheem.toto.forecast_mean": float(median.mean()),
        "rheem.toto.forecast_band_end": float(high[-1] - low[-1]),
    }
    model_tag = MODEL.split("/")[-1]
    tags = [f"column:{column}", f"model:{model_tag}"]
    try:
        dd_status = send_toto_metrics(toto_metrics, tags)
    except Exception as e:  # never let a Datadog failure fail the forecast
        dd_status = f"datadog error: {e}"
    print(f"\nDatadog (rheem.toto.*): {dd_status}")
    print("Note: the future-dated forecast series is intentionally NOT pushed "
          "to Datadog (the metrics intake rejects future timestamps). Only the "
          "current-time rheem.toto.* summary above is sent; the parquet holds "
          "the full prediction.")


if __name__ == "__main__":
    main()
