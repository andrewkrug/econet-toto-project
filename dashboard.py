#!/usr/bin/env python3
"""Create/update the Datadog dashboard for the Rheem heater.

Uses the Datadog HTTP API directly (urllib) so it doesn't depend on a
particular datadog-api-client version. Needs DD_API_KEY + DD_APP_KEY in the
environment or ~/.config/rheem/env (override the dir with $XDG_CONFIG_HOME).
Idempotent-ish: pass an existing dashboard id to update it.

    .venv/bin/python dashboard.py            # create
    .venv/bin/python dashboard.py <dash_id>  # update existing
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


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


def ts_widget(title: str, query: str, display="line") -> dict:
    return {
        "definition": {
            "title": title,
            "type": "timeseries",
            "requests": [{
                "q": query,
                "display_type": display,
                "style": {"palette": "dog_classic"},
            }],
        }
    }


def ts_dual(title: str, left_q: str, right_q: str,
            left_display="bars", right_display="line") -> dict:
    """Timeseries with a second metric on the right y-axis (used to
    overlay outdoor temperature on the power/energy graph)."""
    return {
        "definition": {
            "title": title,
            "type": "timeseries",
            "requests": [
                {"q": left_q, "display_type": left_display,
                 "style": {"palette": "dog_classic"}},
                {"q": right_q, "display_type": right_display,
                 "style": {"palette": "warm"}, "on_right_yaxis": True},
            ],
            "yaxis": {"scale": "linear"},
            "right_yaxis": {"scale": "linear", "include_zero": False},
        }
    }


def query_value(title: str, query: str, formats=None,
                precision: int = 0, agg: str = "last") -> dict:
    req = {"q": query, "aggregator": agg}
    if formats:
        req["conditional_formats"] = formats
    return {
        "definition": {
            "title": title,
            "type": "query_value",
            "requests": [req],
            "autoscale": True,
            "precision": precision,
        }
    }


# Conditional color bands for the big-number widgets. Datadog applies the
# first matching format, so list them strict -> loose.
def hi(green: float, yellow: float) -> list:
    """Higher is better (e.g. gallons, savings)."""
    return [
        {"comparator": ">", "value": green, "palette": "white_on_green"},
        {"comparator": ">", "value": yellow, "palette": "white_on_yellow"},
        {"comparator": "<=", "value": yellow, "palette": "white_on_red"},
    ]


def lo(green: float, red: float) -> list:
    """Lower is better (e.g. energy use)."""
    return [
        {"comparator": "<", "value": green, "palette": "white_on_green"},
        {"comparator": "<", "value": red, "palette": "white_on_yellow"},
        {"comparator": ">=", "value": red, "palette": "white_on_red"},
    ]


def flag(threshold: float = 1) -> list:
    """0/low good, at-or-above threshold bad (e.g. alerts)."""
    return [
        {"comparator": "<", "value": threshold,
         "palette": "white_on_green"},
        {"comparator": ">=", "value": threshold,
         "palette": "white_on_red"},
    ]


def tint(bg: str) -> list:
    """Always-on background color for informational numbers."""
    return [{"comparator": ">", "value": -1e12,
             "palette": "custom_bg", "custom_bg_color": bg}]


def note_widget(content: str) -> dict:
    return {"definition": {"type": "note", "content": content,
                           "background_color": "blue", "font_size": "14",
                           "text_align": "left"}}


def build() -> dict:
    return {
        "title": "Rheem Heat Pump Water Heater",
        "description": "Telemetry collected from EcoNet via pyeconet "
                       "(15-min intervals). Read-only. Includes usable-gallons "
                       "extrapolation (80-gal tank) and a Toto 2.0 forecast "
                       "section (rheem.toto.* from toto_forecast.py).",
        "layout_type": "ordered",
        "widgets": [
            query_value("Hot Water Availability (last)",
                        "avg:rheem.tank_hot_water_availability{*}",
                        hi(60, 30)),
            query_value("Usable Hot Water (gal, last)",
                        "avg:rheem.tank_gallons_available{*}",
                        hi(40, 15)),   # >=40 bath, >=15 shower
            query_value("Energy Today (kWh, from app)",
                        "max:rheem.energy_today_kwh{*}",
                        lo(6, 12), precision=1),
            query_value("Active Alerts", "max:rheem.alert_count{*}",
                        flag(1)),
            query_value("Set Point °F", "avg:rheem.set_point{*}",
                        tint("#2b6cb0")),
            # energy_today_kwh comes from the EcoNet usage report (same
            # number the app shows) and resets daily, so the intraday
            # curve is the ramp and the daily max is that day's total.
            ts_widget("Energy Today — kWh ramp (from EcoNet app, resets daily)",
                      "max:rheem.energy_today_kwh{*}", "area"),
            ts_widget("Daily Energy Consumption (kWh/day, from app)",
                      "max:rheem.energy_today_kwh{*}.rollup(max, 86400)",
                      "bars"),
            ts_widget("Hot Water Used (gal/day) vs Energy (kWh/day)",
                      "max:rheem.water_today_gal{*}.rollup(max, 86400), "
                      "max:rheem.energy_today_kwh{*}.rollup(max, 86400)"),
            # Independent runtime-based estimate (cross-check / fallback).
            ts_widget("Estimated Power Draw (kW, from run state)",
                      "avg:rheem.power_est_kw{*}", "area"),
            ts_widget("Energy: app kWh vs runtime estimate (today)",
                      "max:rheem.energy_today_kwh{*}, "
                      "max:rheem.energy_est_kwh_today{*}"),
            # Heat-pump efficiency drops as it gets colder out, so overlay
            # outdoor temp (right axis) on the energy/power graphs.
            ts_dual("Daily Energy (kWh) vs Outdoor Temp (°F)",
                    "max:rheem.energy_today_kwh{*}.rollup(max, 86400)",
                    "avg:rheem.outdoor_temp_f{*}"),
            ts_dual("Estimated Power (kW) vs Outdoor Temp (°F)",
                    "avg:rheem.power_est_kw{*}",
                    "avg:rheem.outdoor_temp_f{*}", "area", "line"),
            ts_widget("Hot Water Availability",
                      "avg:rheem.tank_hot_water_availability{*}", "area"),
            ts_widget("Usable Hot Water (gal; index 66 = full = 80 gal)",
                      "avg:rheem.tank_gallons_available{*}", "area"),
            # Negative slope of stored gallons ~= gallons drawn per step
            # (recovery shows as zero; clamp drops the reheat upswing).
            ts_widget("Estimated Hot Water Used (gal / 15 min)",
                      "clamp_min(-derivative("
                      "avg:rheem.tank_gallons_available{*}), 0)", "bars"),
            ts_widget("Set Point °F", "avg:rheem.set_point{*}"),
            ts_widget("Running / Enabled (1=yes)",
                      "avg:rheem.running{*}, avg:rheem.enabled{*}"),
            ts_widget("Tank / Compressor Health",
                      "avg:rheem.tank_health{*}, "
                      "avg:rheem.compressor_health{*}"),
            ts_widget("Alert Count", "max:rheem.alert_count{*}", "bars"),

            # --- Toto 2.0 forecast (Datadog foundation model) ---
            note_widget(
                "## Toto 2.0 forecast\n"
                "Datadog's open-source time-series foundation model "
                "(`Datadog/Toto-2.0-22m`) run by `toto_forecast.py` over "
                "`telemetry.parquet`. The future-dated series is **not** "
                "pushed (the intake rejects future timestamps); these "
                "`rheem.toto.*` gauges summarize each run, stamped at run "
                "time. Split by `column:` / `model:` tags."),
            query_value("Toto: predicted value (next 15 min)",
                        "avg:rheem.toto.forecast_next{*}", hi(60, 30)),
            query_value("Toto: predicted at horizon end",
                        "avg:rheem.toto.forecast_horizon_end{*}",
                        hi(60, 30)),
            query_value("Toto: forecast horizon (h)",
                        "avg:rheem.toto.horizon_hours{*}",
                        tint("#2b6cb0")),
            query_value("Toto: history points used",
                        "avg:rheem.toto.history_points{*}",
                        hi(96, 32)),   # 32=min for Toto, 96=24h
            ts_widget("Toto: forecast band at horizon end "
                      "(lo / mean-end / hi)",
                      "avg:rheem.toto.forecast_lo_end{*}, "
                      "avg:rheem.toto.forecast_horizon_end{*}, "
                      "avg:rheem.toto.forecast_hi_end{*}", "area"),
            ts_widget("Toto: forecast min / mean / max across horizon",
                      "avg:rheem.toto.forecast_min{*}, "
                      "avg:rheem.toto.forecast_mean{*}, "
                      "avg:rheem.toto.forecast_max{*}"),
            ts_widget("Toto: prediction uncertainty (hi-lo band width)",
                      "avg:rheem.toto.forecast_band_end{*}", "area"),
            ts_widget("Toto: history points fed to model",
                      "avg:rheem.toto.history_points{*}"),
            ts_widget("Toto: model run latency (s)",
                      "avg:rheem.toto.run_seconds{*}", "bars"),
            ts_widget("Toto: run heartbeat (1 per forecast)",
                      "sum:rheem.toto.run{*}.as_count()", "bars"),

            # --- Energy-saving recommendation (advisory) ---
            note_widget(
                "## Setpoint recommendation (advisory)\n"
                "From `recommend.py`: a suggested setpoint schedule that "
                "drops to **eco** in predicted low-demand windows and "
                "recovers to **comfort** before demand. **Nothing writes to "
                "the heater** — apply with `rheem.py set-temp`. Split by "
                "`confidence:` tag."),
            query_value("Recommended setpoint now (°F)",
                        "avg:rheem.reco.recommended_setpoint{*}",
                        tint("#2b6cb0")),
            query_value("Eco hours / next 24h",
                        "avg:rheem.reco.eco_hours_next_24h{*}",
                        hi(6, 2), precision=1),
            query_value("Est. standby-loss saving (%)",
                        "avg:rheem.reco.est_savings_pct{*}",
                        hi(15, 5), precision=1),
            ts_widget("Recommended setpoint vs actual (°F)",
                      "avg:rheem.reco.recommended_setpoint{*}, "
                      "avg:rheem.set_point{*}"),
            ts_widget("Recommended eco hours & est. saving %",
                      "avg:rheem.reco.eco_hours_next_24h{*}, "
                      "avg:rheem.reco.est_savings_pct{*}"),
        ],
    }


def main() -> None:
    load_env()
    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    site = os.environ.get("DD_SITE", "datadoghq.com")
    if not api_key or not app_key:
        sys.exit("Need DD_API_KEY and DD_APP_KEY (see env.example).")

    dash_id = sys.argv[1] if len(sys.argv) > 1 else None
    url = f"https://api.{site}/api/v1/dashboard"
    method = "POST"
    if dash_id:
        url += f"/{dash_id}"
        method = "PUT"

    req = urllib.request.Request(
        url, data=json.dumps(build()).encode(), method=method,
        headers={
            "Content-Type": "application/json",
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
        },
    )
    with urllib.request.urlopen(req) as resp:
        body = json.load(resp)
    print(f"{'Updated' if dash_id else 'Created'} dashboard: "
          f"https://app.{site}/dashboard/{body.get('id')}")


if __name__ == "__main__":
    main()
