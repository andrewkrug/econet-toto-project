#!/usr/bin/env python3
"""Standalone telemetry + control for a Rheem EcoNet heat-pump water heater.

The heater (e.g. 172.16.31.41) exposes no local API -- it is cloud-tethered to
Rheem's ClearBlade MQTT broker. This talks to that cloud via the open-source
`pyeconet` library using your EcoNet mobile-app credentials.

Usage:
    python rheem.py status          # one-shot telemetry dump
    python rheem.py watch           # live stream (MQTT push updates)
    python rheem.py energy          # recent energy-usage history
    python rheem.py set-temp 120    # set tank setpoint (degrees, app units)
    python rheem.py set-mode HEAT_PUMP_ONLY
    python rheem.py away on|off

Credentials come from the environment or, if unset there, from
~/.config/rheem/env (override the dir with $XDG_CONFIG_HOME):
    ECONET_EMAIL, ECONET_PASSWORD
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from pyeconet import EcoNetApiInterface
from pyeconet.equipment import EquipmentType
from pyeconet.equipment.water_heater import WaterHeaterOperationMode

# Newline-delimited JSON log the Datadog Agent tails (see conf.d/rheem.d).
JSONL_LOG = Path(__file__).with_name("rheem.jsonl")


def log_json(level: str = "info", **fields) -> None:
    """Emit one structured JSON line to stdout and append it to rheem.jsonl
    so the Datadog log Agent can ship it. `service`/`source`/`status` follow
    Datadog's standard log attributes."""
    import json
    from datetime import datetime, timezone

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": level,
        "service": "rheem-water-heater",
        "source": "rheem",
        **fields,
    }
    line = json.dumps(record, default=str)
    print(line)
    try:
        with JSONL_LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # logging must never break collection


def config_env_path() -> Path:
    """Location of the credentials file: $XDG_CONFIG_HOME/rheem/env, or
    ~/.config/rheem/env when XDG_CONFIG_HOME is unset."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "rheem" / "env"


def load_env() -> tuple[str, str]:
    """Populate os.environ from the user config file without overriding
    values already set in the real environment (env wins over the file)."""
    env_file = config_env_path()
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())
    email = os.environ.get("ECONET_EMAIL")
    password = os.environ.get("ECONET_PASSWORD")
    if not email or not password:
        sys.exit(f"Missing ECONET_EMAIL / ECONET_PASSWORD -- set them in the "
                 f"environment or {env_file} (see env.example)")
    return email, password


async def get_heater(api: EcoNetApiInterface):
    equipment = await api.get_equipment_by_type([EquipmentType.WATER_HEATER])
    heaters = equipment.get(EquipmentType.WATER_HEATER, [])
    if not heaters:
        sys.exit("No water heater found on this EcoNet account.")
    return heaters[0]


def dump(wh) -> None:
    print(f"  Device:        {wh.device_name}  (id={wh.device_id})")
    print(f"  Serial:        {wh.serial_number}")
    print(f"  Connected:     {wh.connected}")
    print(f"  Running:       {wh.running}  state={wh.running_state}")
    print(f"  Enabled:       {wh.enabled}")
    mode_name = getattr(wh.mode, "name", wh.mode)
    avail = sorted({getattr(m, "name", str(m)) for m in (wh.modes or [])})
    print(f"  Mode:          {mode_name}   (available: {', '.join(avail)})")
    print(f"  Setpoint:      {wh.set_point}  limits={wh.set_point_limits}")
    avail = wh.tank_hot_water_availability
    gallons = _gallons(avail)
    print(f"  Hot water avail:{avail}  (~{gallons} gal usable; "
          f"{FULL_INDEX:g}=full -> {USABLE_GALLONS:g} gal)")
    print(f"  Tank health:   {wh.tank_health}")
    print(f"  Compressor:    {wh.compressor_health}")
    print(f"  Energy type:   {wh.energy_type}")
    print(f"  Today energy:  {wh.todays_energy_usage}")
    print(f"  Today water:   {wh.todays_water_usage}")
    print(f"  Away/Vacation: away={wh.away} vacation={wh.vacation}")
    print(f"  Alerts:        {wh.alert_count}")
    print(f"  WiFi signal:   {wh.wifi_signal}")


async def cmd_status(api):
    print("== Rheem EcoNet water heater ==")
    dump(await get_heater(api))


async def cmd_watch(api):
    wh = await get_heater(api)
    api.subscribe()

    def on_update():
        print("\n-- update --")
        dump(wh)

    wh.set_update_callback(on_update)
    print("Subscribed. Live updates below (Ctrl-C to stop).")
    dump(wh)
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


async def cmd_energy(api):
    wh = await get_heater(api)
    usage = await wh.get_energy_usage()
    print("Energy usage:")
    print(usage)


PARQUET_PATH = Path(__file__).with_name("telemetry.parquet")

# Physical tank size, used to extrapolate gallons of usable hot water from
# the heater's 0-100 "hot water availability" index. Override for a
# different unit:  RHEEM_TANK_GALLONS=50
TANK_GALLONS = float(os.environ.get("RHEEM_TANK_GALLONS", "80"))

# tank_hot_water_availability is a coarse quantized index (this unit only
# reports ~0/33/66/100). In ENERGY_SAVING mode 66 is its normal "full and
# ready" state -- the EcoNet app treats that as full -- so we calibrate
# 66 -> USABLE_GALLONS rather than the naive index%/100 * tank size. 100
# then reads as a boosted reserve (>USABLE_GALLONS), which is real extra
# capacity, not an error.
FULL_INDEX = float(os.environ.get("RHEEM_FULL_INDEX", "66"))
USABLE_GALLONS = float(os.environ.get("RHEEM_USABLE_GALLONS", "80"))


def _gallons(avail):
    """Usable hot-water gallons from the availability index (66 = full)."""
    return (None if avail is None
            else round(float(avail) / FULL_INDEX * USABLE_GALLONS, 1))


def _num(x):
    """Coerce a pyeconet value to float, or None if not numeric."""
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


# This unit reports no native kWh, so energy is *estimated* from run state
# using typical 80-gal hybrid HPWH ratings (override per your spec plate):
#   heat-pump compressor ~0.45 kW, resistive elements ~4.5 kW, idle 0.
HP_KW = float(os.environ.get("RHEEM_HP_KW", "0.45"))
ELEMENT_KW = float(os.environ.get("RHEEM_ELEMENT_KW", "4.5"))


def _est_power_kw(running, running_state) -> float:
    """Estimated instantaneous draw (kW) from the reported run state."""
    if not running:
        return 0.0
    st = str(running_state or "").lower()
    if "element" in st or "electric" in st or "resist" in st:
        return ELEMENT_KW
    return HP_KW  # compressor / heat-pump running


def _energy_today_kwh(df) -> float:
    """Trapezoidal integral of estimated power over the current local day,
    recomputed from each row's running/running_state (historical rows have
    no power column). Per-step gap capped at 1h to bound downtime error."""
    import pandas as pd

    if df.empty:
        return 0.0
    d = df.copy()
    d["_ts"] = pd.to_datetime(d["timestamp"], utc=True).dt.tz_convert(
        _local_tz())
    today = d["_ts"].iloc[-1].date()
    d = d[d["_ts"].dt.date == today].sort_values("_ts")
    if len(d) < 2:
        return 0.0
    p = [_est_power_kw(r.running, r.running_state)
         for r in d.itertuples()]
    t = d["_ts"].tolist()
    kwh = 0.0
    for i in range(1, len(t)):
        hrs = min((t[i] - t[i - 1]).total_seconds() / 3600.0, 1.0)
        kwh += (p[i] + p[i - 1]) / 2.0 * hrs
    return round(kwh, 3)


def _local_tz():
    from datetime import datetime
    return datetime.now().astimezone().tzinfo


# Outdoor temp matters for a heat-pump WH (COP falls as ambient drops).
# Defaults to ZIP 97501 (Medford, OR); override with RHEEM_LAT/RHEEM_LON.
WEATHER_LAT = os.environ.get("RHEEM_LAT", "42.3265")
WEATHER_LON = os.environ.get("RHEEM_LON", "-122.8756")


def _outdoor_temp_f():
    """Current outdoor temperature (F) from Open-Meteo (no API key).
    Best-effort: returns None on any failure so a weather outage never
    blocks telemetry collection."""
    if os.environ.get("RHEEM_DISABLE_WEATHER"):
        return None
    import json as _json
    import urllib.request

    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
           "&current=temperature_2m&temperature_unit=fahrenheit")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = _json.load(r)
        return _num(data.get("current", {}).get("temperature_2m"))
    except Exception:
        return None

# (datadog metric name, attribute, numeric coercion) -- only numeric/bool
# fields are shipped as gauges; strings stay in the parquet store only.
DD_METRICS = [
    ("rheem.running", "running", float),
    ("rheem.enabled", "enabled", float),
    ("rheem.connected", "connected", float),
    ("rheem.set_point", "set_point", float),
    ("rheem.tank_hot_water_availability", "tank_hot_water_availability", float),
    ("rheem.tank_gallons_available", "tank_gallons_available", float),
    ("rheem.energy_today_kwh", "energy_today_kwh", float),
    ("rheem.water_today_gal", "water_today_gal", float),
    ("rheem.power_est_kw", "power_est_kw", float),
    ("rheem.energy_est_kwh_today", "energy_est_kwh_today", float),
    ("rheem.outdoor_temp_f", "outdoor_temp_f", float),
    ("rheem.tank_health", "tank_health", float),
    ("rheem.compressor_health", "compressor_health", float),
    ("rheem.alert_count", "alert_count", float),
    ("rheem.away", "away", float),
    ("rheem.vacation", "vacation", float),
]


def _send_datadog(row: dict) -> str:
    """Submit numeric telemetry to Datadog as gauges. No-op (returns reason)
    if DD_API_KEY isn't set, so the collector still works without Datadog."""
    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        return "skipped (no DD_API_KEY)"
    from datetime import datetime, timezone
    from datadog_api_client import Configuration, ApiClient
    from datadog_api_client.v2.api.metrics_api import MetricsApi
    from datadog_api_client.v2.model.metric_payload import MetricPayload
    from datadog_api_client.v2.model.metric_series import MetricSeries
    from datadog_api_client.v2.model.metric_point import MetricPoint
    from datadog_api_client.v2.model.metric_intake_type import MetricIntakeType
    from datadog_api_client.v2.model.metric_resource import MetricResource

    ts = int(datetime.now(timezone.utc).timestamp())
    tags = [f"device:{row['device_id']}", f"mode:{row['mode']}",
            f"running_state:{row['running_state']}"]
    resources = [MetricResource(name=str(row["device_id"]), type="host")]
    series = []
    for name, attr, coerce in DD_METRICS:
        val = row.get(attr)
        if val is None:
            continue
        series.append(MetricSeries(
            metric=name,
            type=MetricIntakeType.GAUGE,
            points=[MetricPoint(timestamp=ts, value=coerce(val))],
            tags=tags,
            resources=resources,
        ))
    cfg = Configuration()
    cfg.api_key["apiKeyAuth"] = api_key
    if os.environ.get("DD_SITE"):
        cfg.server_variables["site"] = os.environ["DD_SITE"]
    with ApiClient(cfg) as client:
        MetricsApi(client).submit_metrics(body=MetricPayload(series=series))
    return f"sent {len(series)} gauges"


async def cmd_log(api):
    """Append one telemetry row to telemetry.parquet and (if DD_API_KEY set)
    ship numeric metrics to Datadog. Read-only toward the heater; intended to
    be driven by launchd/cron every 15 minutes."""
    import pandas as pd
    from datetime import datetime, timezone

    wh = await get_heater(api)

    # Pull the usage report (same data the EcoNet app shows). This is what
    # populates wh.todays_energy_usage / energy_type / water_usage -- they
    # are None until this dynamic-action call is awaited. Best-effort: a
    # failure here must not lose the telemetry row.
    for fn in (wh.get_energy_usage, wh.get_water_usage):
        try:
            await fn()
        except Exception as e:  # noqa: BLE001
            log_json(level="warning", event="usage.fetch_failed",
                     call=fn.__name__, error=str(e))

    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "device_id": wh.device_id,
        "connected": bool(wh.connected),
        "running": bool(wh.running),
        "running_state": str(wh.running_state),
        "enabled": bool(wh.enabled),
        "mode": getattr(wh.mode, "name", str(wh.mode)),
        "set_point": wh.set_point,
        "tank_hot_water_availability": wh.tank_hot_water_availability,
        # Usable hot water, calibrated so the index's normal "ready" step
        # (66) maps to USABLE_GALLONS (see _gallons). None-safe.
        "tank_gallons_available": _gallons(wh.tank_hot_water_availability),
        "tank_health": wh.tank_health,
        "compressor_health": wh.compressor_health,
        "away": bool(wh.away),
        "vacation": bool(wh.vacation),
        "alert_count": wh.alert_count,
        "wifi_signal": wh.wifi_signal,
        # Energy: today's cumulative kWh the unit reports (resets daily).
        # Some EcoNet units return None here -- kept None-safe; the
        # Datadog push skips None values so the gauge just won't appear.
        "energy_type": (None if wh.energy_type is None
                        else str(getattr(wh.energy_type, "name",
                                         wh.energy_type))),
        "energy_today_kwh": _num(wh.todays_energy_usage),
        "water_today_gal": _num(wh.todays_water_usage),
        # Independent estimate from run state (cross-check / fallback for
        # when the usage report is unavailable). See _est_power_kw.
        "power_est_kw": _est_power_kw(wh.running, wh.running_state),
        # Outdoor temp for heat-pump efficiency correlation.
        "outdoor_temp_f": _outdoor_temp_f(),
    }
    df_new = pd.DataFrame([row])
    if PARQUET_PATH.exists():
        df = pd.concat([pd.read_parquet(PARQUET_PATH), df_new],
                       ignore_index=True)
    else:
        df = df_new
    # Estimated kWh so far today (integral of estimated power). Computed
    # over the full frame so it's consistent across restarts.
    row["energy_est_kwh_today"] = _energy_today_kwh(df)
    df.loc[df.index[-1], "energy_est_kwh_today"] = row["energy_est_kwh_today"]
    df.to_parquet(PARQUET_PATH, index=False)

    try:
        dd_status = _send_datadog(row)
        level = "info"
    except Exception as e:  # never let Datadog failure lose the parquet row
        dd_status = f"datadog error: {e}"
        level = "warning"
    log_json(level=level, event="telemetry.collected",
             datadog=dd_status, rows=len(df), **row)


async def cmd_set_temp(api, value: str):
    wh = await get_heater(api)
    target = int(value)
    lo, hi = (wh.set_point_limits or (None, None))
    if lo is not None and not (lo <= target <= hi):
        sys.exit(f"Setpoint {target} outside allowed range {lo}-{hi}")
    print(f"Setting setpoint {wh.set_point} -> {target}")
    await wh.set_set_point(target)
    print("Sent.")


async def cmd_set_mode(api, mode_name: str):
    try:
        mode = WaterHeaterOperationMode[mode_name.upper()]
    except KeyError:
        valid = ", ".join(m.name for m in WaterHeaterOperationMode)
        sys.exit(f"Unknown mode '{mode_name}'. Valid: {valid}")
    wh = await get_heater(api)
    if mode not in (wh.modes or []):
        print(f"WARNING: {mode.name} not in device-reported modes {wh.modes}")
    print(f"Setting mode {wh.mode} -> {mode.name}")
    await wh.set_mode(mode)
    print("Sent.")


async def cmd_away(api, state: str):
    wh = await get_heater(api)
    on = state.lower() in ("on", "true", "1", "yes")
    print(f"Setting away mode -> {on}")
    await wh.set_away_mode(on)
    print("Sent.")


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, *args = sys.argv[1:]
    email, password = load_env()
    api = await EcoNetApiInterface.login(email, password)
    try:
        if cmd == "status":
            await cmd_status(api)
        elif cmd == "watch":
            await cmd_watch(api)
        elif cmd == "energy":
            await cmd_energy(api)
        elif cmd == "log":
            await cmd_log(api)
        elif cmd == "set-temp":
            await cmd_set_temp(api, args[0])
        elif cmd == "set-mode":
            await cmd_set_mode(api, args[0])
        elif cmd == "away":
            await cmd_away(api, args[0])
        else:
            sys.exit(f"Unknown command: {cmd}\n{__doc__}")
    finally:
        try:
            api.unsubscribe()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
