#!/usr/bin/env python3
"""Tiny dependency-free hot-water status page.

Reads the rheem collector's newline-JSON log (rheem.jsonl, written every
15 min by `rheem.py log`) and answers two questions with red/green widgets:

  * Can I shower?        (>= HOTWATER_SHOWER_GALLONS of usable hot water)
  * Can I fill a bath?   (>= HOTWATER_BATH_GALLONS; a bath = 40 gal)

Stdlib only -- runs on a bare python:slim image, no build step. Behind the
existing nginx, mounted at /hotwater (nginx strips that prefix, so routes
here are "/" and "/api").

Env:
  HOTWATER_JSONL           path to rheem.jsonl   (default /data/rheem.jsonl)
  HOTWATER_TANK_GALLONS    tank size for the %->gal fallback (default 80)
  HOTWATER_SHOWER_GALLONS  shower threshold, gal (default 15)
  HOTWATER_BATH_GALLONS    bath threshold, gal   (default 40)
  HOTWATER_STALE_MINUTES   mark data stale after this many min (default 40)
  HOTWATER_PORT            listen port           (default 8000)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JSONL = os.environ.get("HOTWATER_JSONL", "/data/rheem.jsonl")
TANK_GALLONS = float(os.environ.get("HOTWATER_TANK_GALLONS", "80"))
SHOWER_GAL = float(os.environ.get("HOTWATER_SHOWER_GALLONS", "15"))
BATH_GAL = float(os.environ.get("HOTWATER_BATH_GALLONS", "40"))
STALE_MIN = float(os.environ.get("HOTWATER_STALE_MINUTES", "40"))
PORT = int(os.environ.get("HOTWATER_PORT", "8000"))
# Calibration fallback (must match rheem.py): the availability index is
# quantized and 66 is the unit's normal "full" step, so 66 -> USABLE_GAL.
# Normally unused -- tank_gallons_available in rheem.jsonl is already
# calibrated; this only applies if that field is ever missing.
FULL_INDEX = float(os.environ.get("HOTWATER_FULL_INDEX", "66"))
USABLE_GAL = float(os.environ.get("HOTWATER_USABLE_GALLONS", "80"))


def latest_reading() -> dict | None:
    """Last valid JSON object in the collector log, or None."""
    try:
        with open(JSONL, "rb") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return None
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            return json.loads(raw)
        except ValueError:
            continue
    return None


def recent_series(limit: int = 24) -> list[tuple[datetime, float]]:
    """Recent (timestamp, gallons) points from the collector log, oldest
    first -- used to estimate the recovery rate."""
    try:
        with open(JSONL, "rb") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    out: list[tuple[datetime, float]] = []
    for raw in lines[-limit:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        g = r.get("tank_gallons_available")
        a = r.get("tank_hot_water_availability")
        if g is None and a is not None:
            g = a / FULL_INDEX * USABLE_GAL
        ts = r.get("timestamp")
        if g is None or not ts:
            continue
        try:
            out.append((datetime.fromisoformat(ts), float(g)))
        except ValueError:
            continue
    return out


def recovery_rate(series: list[tuple[datetime, float]]) -> float | None:
    """Gallons-per-minute the tank is currently *recovering*, from a
    least-squares fit over the last ~90 min. None if it isn't rising
    (heater idle/drawing) -- then an ETA can't be reasonably given."""
    s = series[-6:]
    if len(s) < 2:
        return None
    t0 = s[0][0]
    xs = [(d - t0).total_seconds() / 60.0 for d, _ in s]
    ys = [g for _, g in s]
    n = len(s)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den
    return slope if slope > 0.01 else None


def status() -> dict:
    r = latest_reading()
    if r is None:
        return {"ok": False, "error": "no telemetry yet",
                "shower_threshold_gal": SHOWER_GAL,
                "bath_threshold_gal": BATH_GAL}

    avail = r.get("tank_hot_water_availability")
    gallons = r.get("tank_gallons_available")
    if gallons is None and avail is not None:
        gallons = round(avail / FULL_INDEX * USABLE_GAL, 1)
    gallons = float(gallons or 0.0)

    ts = r.get("timestamp")
    age_min = None
    stale = False
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
            stale = age_min > STALE_MIN
        except ValueError:
            pass

    can_shower = gallons >= SHOWER_GAL
    can_bath = gallons >= BATH_GAL

    # When the answer is NO, estimate minutes until the tank recovers to
    # the threshold, from the recent recovery rate. None if not recovering.
    rate = recovery_rate(recent_series())

    def eta(threshold: float, ready: bool) -> int | None:
        if ready or not rate:
            return None
        return max(1, int(round((threshold - gallons) / rate)))

    return {
        "ok": True,
        "gallons_available": round(gallons, 1),
        "availability_pct": avail,
        "can_shower": can_shower,
        "can_bath": can_bath,
        "shower_threshold_gal": SHOWER_GAL,
        "bath_threshold_gal": BATH_GAL,
        "tank_gallons": TANK_GALLONS,
        "usable_full_gallons": USABLE_GAL,
        "full_index": FULL_INDEX,
        "recovery_gal_per_min": None if rate is None else round(rate, 3),
        "shower_eta_min": eta(SHOWER_GAL, can_shower),
        "bath_eta_min": eta(BATH_GAL, can_bath),
        "set_point_f": r.get("set_point"),
        "mode": r.get("mode"),
        "running_state": r.get("running_state"),
        "as_of": ts,
        "age_minutes": None if age_min is None else round(age_min, 1),
        "stale": stale,
    }


def widget(title: str, ok: bool, sub: str, eta: str = "") -> str:
    color = "#1f9d55" if ok else "#cc1f1a"
    glyph = "&#10003;" if ok else "&#10007;"   # check / cross
    answer = "YES" if ok else "NO"
    eta_html = f'<div class="eta">{eta}</div>' if eta else ""
    return f"""
      <div class="card" style="background:{color}">
        <div class="title">{title}</div>
        <div class="answer">{glyph} {answer}</div>
        <div class="sub">{sub}</div>
        {eta_html}
      </div>"""


def eta_text(eta_min, ready: bool, rate) -> str:
    """Human ETA line shown on a red widget."""
    if ready:
        return ""
    if eta_min is None:
        return "not recovering &mdash; ETA unknown"
    if eta_min >= 60:
        h, m = divmod(int(eta_min), 60)
        when = f"~{h}h {m:02d}m"
    else:
        when = f"~{int(eta_min)} min"
    r = "" if not rate else f" ({rate:g} gal/min)"
    return f"ready in {when}{r}"


def page() -> bytes:
    s = status()
    if not s["ok"]:
        body = ('<div class="card" style="background:#777">'
                '<div class="title">Hot Water</div>'
                '<div class="answer">no data</div>'
                '<div class="sub">collector has not logged yet</div></div>')
        meta = ""
    else:
        g = s["gallons_available"]
        rate = s.get("recovery_gal_per_min")
        body = (widget("Can I shower?", s["can_shower"],
                       f"{g:g} gal available &middot; need &ge; "
                       f"{SHOWER_GAL:g} gal",
                       eta_text(s["shower_eta_min"], s["can_shower"], rate))
                + widget("Can I fill a bath?", s["can_bath"],
                         f"{g:g} gal available &middot; a bath = "
                         f"{BATH_GAL:g} gal",
                         eta_text(s["bath_eta_min"], s["can_bath"], rate)))
        stale = (" &middot; <span class='stale'>STALE</span>"
                 if s["stale"] else "")
        age = "" if s["age_minutes"] is None else \
            f" ({s['age_minutes']:g} min ago)"
        meta = (f"<p class='meta'>{g:g} gal usable hot water "
                f"&middot; setpoint {s.get('set_point_f')}&deg;F "
                f"&middot; {s.get('mode')}<br>"
                f"as of {s.get('as_of')}{age}{stale}</p>")

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hot Water</title>
<meta http-equiv="refresh" content="60">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #11151a; color: #eee; min-height: 100vh;
         display: flex; flex-direction: column; align-items: center;
         justify-content: center; padding: clamp(16px, 5vw, 32px); }}
  h1 {{ font-weight: 600; margin: 0 0 clamp(16px, 4vw, 28px);
        font-size: clamp(1.15rem, 4vw, 1.5rem); text-align: center; }}
  .cards {{ display: flex; gap: clamp(14px, 3vw, 22px); flex-wrap: wrap;
           justify-content: center; width: 100%;
           max-width: 600px; }}
  .card {{ flex: 1 1 240px; max-width: 280px; min-width: 0;
          border-radius: 16px; padding: clamp(20px, 5vw, 30px) 20px;
          text-align: center; box-shadow: 0 6px 20px rgba(0,0,0,.4); }}
  .title {{ font-size: clamp(.95rem, 3vw, 1.1rem); opacity: .9; }}
  .answer {{ font-size: clamp(2rem, 9vw, 2.7rem); font-weight: 700;
            margin: 10px 0 6px; }}
  .sub {{ font-size: clamp(.78rem, 2.6vw, .88rem); opacity: .9; }}
  .eta {{ margin-top: 10px; font-size: clamp(.82rem, 2.8vw, .95rem);
         font-weight: 600; background: rgba(0,0,0,.22);
         border-radius: 8px; padding: 6px 10px; }}
  .meta {{ margin-top: clamp(18px, 5vw, 26px); font-size: .8rem;
          color: #9aa4af; text-align: center; line-height: 1.6;
          padding: 0 12px; word-break: break-word; }}
  .stale {{ color: #f0a500; font-weight: 700; }}
  a {{ color: #6cb6ff; }}
  @media (max-width: 480px) {{
    .cards {{ flex-direction: column; align-items: stretch; }}
    .card {{ max-width: none; }}
  }}
</style>
</head>
<body>
  <h1>&#128704; Hot Water Status</h1>
  <div class="cards">{body}</div>
  {meta}
  <p class="meta">auto-refreshes every 60s &middot;
     <a href="api">JSON</a></p>
</body>
</html>"""
    return html.encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/index.html"):
            self._send(200, page(), "text/html; charset=utf-8")
        elif path == "/api":
            self._send(200, json.dumps(status()).encode(),
                       "application/json")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET

    def log_message(self, fmt: str, *args) -> None:
        # One concise line to stdout so the Datadog agent can ship it.
        print("hotwater %s - %s" % (self.address_string(), fmt % args),
              flush=True)


if __name__ == "__main__":
    print(f"hotwater: serving on :{PORT}, reading {JSONL} "
          f"(shower>={SHOWER_GAL}gal, bath>={BATH_GAL}gal)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
