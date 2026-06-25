#!/usr/bin/env python3
"""
Nibe Smart Control — Home Assistant Addon v1.2.0
Served via HA Ingress. All config stored in /data/config.json (edited via web UI).
"""

import asyncio, json, logging, math, os, sys, time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

import aiohttp
from aiohttp import web

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
DATA_DIR     = Path("/data")
CONFIG_FILE  = DATA_DIR / "config.json"
STATE_FILE   = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.json"

MAX_HISTORY      = 500
MIN_WRITE_INTERVAL = 10   # minutes, default

LOG_LEVEL_MAP = {"debug": logging.DEBUG, "info": logging.INFO,
                 "warning": logging.WARNING, "error": logging.ERROR}

DEFAULT_CONFIG = {
    # Entities
    "weather_entity": "",
    "electricity_price_entity": "",
    "outdoor_temp_entity": "",
    "indoor_temp_entity": "",
    "indoor_setpoint_entity": "",
    "heat_curve_entity": "",
    "curve_offset_entity": "",
    # Weather
    "forecast_hours": 6,
    "weather_enabled": False,
    "weather_enable_up": True,
    "weather_enable_down": True,
    "weather_adjust_factor": 0.0,
    # Indoor
    "indoor_enabled": False,
    "indoor_target_temp": 21.0,
    "indoor_factor": 10.0,
    # Price
    "price_enabled": False,
    "price_very_cheap": 2.0,
    "price_cheap": 1.0,
    "price_normal": 0.0,
    "price_expensive": -1.0,
    "price_very_expensive": -2.0,
    "price_very_cheap_threshold": 0.05,
    "price_cheap_threshold": 0.08,
    "price_expensive_threshold": 0.14,
    "price_very_expensive_threshold": 0.20,
    # Rate limiting
    "min_write_interval_min": MIN_WRITE_INTERVAL,
    "log_level": "info",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(level_str: str) -> logging.Logger:
    level = LOG_LEVEL_MAP.get(level_str.lower(), logging.INFO)
    logging.basicConfig(level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S", stream=sys.stdout)
    return logging.getLogger("nibe")

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load_json(path: Path, default) -> Any:
    if path.exists():
        try:
            with open(path) as f: return json.load(f)
        except Exception: pass
    return default() if callable(default) else default

def save_json(path: Path, data: Any):
    try:
        with open(path, "w") as f: json.dump(data, f, indent=2)
    except Exception as e:
        logging.getLogger("nibe").error(f"save_json({path}): {e}")

def load_config() -> dict:
    saved = load_json(CONFIG_FILE, {})
    return {**DEFAULT_CONFIG, **saved}

def load_state() -> dict:
    return load_json(STATE_FILE, {
        "weather_offset": 0.0, "indoor_offset": 0.0, "price_offset": 0.0,
        "last_combined_offset": None, "last_write_ts": 0,
        "last_price_level": "UNKNOWN", "last_forecast_temp": None,
        "last_outdoor_temp": None, "last_indoor_temp": None,
        "last_indoor_setpoint": None, "last_price": None,
    })

# ---------------------------------------------------------------------------
# HA API client — uses Supervisor token, talks to core API
# ---------------------------------------------------------------------------
class HAClient:
    def __init__(self, session: aiohttp.ClientSession, logger: logging.Logger):
        self.session = session
        self.logger  = logger
        self.base    = "http://supervisor/core/api"
        token        = os.environ.get("SUPERVISOR_TOKEN", "")
        self.headers = {"Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"}

    async def get_state(self, entity_id: str) -> Optional[dict]:
        if not entity_id: return None
        url = f"{self.base}/states/{entity_id}"
        try:
            async with self.session.get(url, headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json() if r.status == 200 else None
        except Exception as e:
            self.logger.debug(f"get_state({entity_id}): {e}")
            return None

    async def get_float(self, entity_id: str) -> Optional[float]:
        s = await self.get_state(entity_id)
        if not s: return None
        raw = s.get("state")
        if raw in (None, "unavailable", "unknown", ""): return None
        try: return float(raw)
        except ValueError: return None

    async def set_number(self, entity_id: str, value: float) -> bool:
        url = f"{self.base}/services/number/set_value"
        try:
            async with self.session.post(url, headers=self.headers,
                    json={"entity_id": entity_id, "value": str(round(value, 1))},
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status in (200, 201)
        except Exception as e:
            self.logger.error(f"set_number({entity_id}, {value}): {e}")
            return False

    async def get_weather_forecast(self, entity_id: str) -> Optional[list]:
        url = f"{self.base}/services/weather/get_forecasts"
        try:
            async with self.session.post(url, headers=self.headers,
                    json={"entity_id": entity_id, "type": "hourly"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status in (200, 201):
                    data = await r.json()
                    if isinstance(data, list):
                        for item in data:
                            fc = (item.get("response", {}) if isinstance(item, dict) else {}).get(entity_id, {}).get("forecast")
                            if fc: return sorted(fc, key=lambda x: x.get("datetime", ""))
                    if isinstance(data, dict):
                        fc = data.get(entity_id, {}).get("forecast")
                        if fc: return sorted(fc, key=lambda x: x.get("datetime", ""))
        except Exception as e:
            self.logger.debug(f"get_forecasts: {e}")
        s = await self.get_state(entity_id)
        if s:
            fc = s.get("attributes", {}).get("forecast", [])
            if fc: return sorted(fc, key=lambda x: x.get("datetime", ""))
        return None

    async def list_entities(self, domain: str = "") -> list:
        url = f"{self.base}/states"
        try:
            async with self.session.get(url, headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    states = await r.json()
                    if domain:
                        states = [s for s in states if s["entity_id"].startswith(domain + ".")]
                    return [{"entity_id": s["entity_id"],
                             "friendly_name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
                             "state": s.get("state"),
                             "unit": s.get("attributes", {}).get("unit_of_measurement", "")}
                            for s in sorted(states, key=lambda x: x["entity_id"])]
        except Exception as e:
            self.logger.error(f"list_entities: {e}")
        return []

# ---------------------------------------------------------------------------
# Algorithms (NibePi port)
# ---------------------------------------------------------------------------
def classify_price(price: float, cfg: dict) -> str:
    t = [cfg.get("price_very_cheap_threshold", 0.05),
         cfg.get("price_cheap_threshold", 0.08),
         cfg.get("price_expensive_threshold", 0.14),
         cfg.get("price_very_expensive_threshold", 0.20)]
    if price <= t[0]: return "VERY_CHEAP"
    if price <= t[1]: return "CHEAP"
    if price <  t[2]: return "NORMAL"
    if price <  t[3]: return "EXPENSIVE"
    return "VERY_EXPENSIVE"

def price_to_offset(level: str, cfg: dict) -> float:
    return float({"VERY_CHEAP": cfg.get("price_very_cheap", 2.0),
                  "CHEAP":      cfg.get("price_cheap", 1.0),
                  "NORMAL":     cfg.get("price_normal", 0.0),
                  "EXPENSIVE":  cfg.get("price_expensive", -1.0),
                  "VERY_EXPENSIVE": cfg.get("price_very_expensive", -2.0)}.get(level, 0.0))

def calc_weather_offset(outdoor, forecast, curve, sun_factor=0.0,
                        enable_up=True, enable_down=True) -> float:
    if curve == 0: return 0.0
    raw = (outdoor - forecast - sun_factor) * (curve * 1.2 / 10) / ((curve / 10) + 1)
    raw = round(raw, 2)
    if raw > 0 and not enable_up:  raw = 0.0
    if raw < 0 and not enable_down: raw = 0.0
    return max(-10.0, min(10.0, raw))

def calc_indoor_offset(setpoint, actual, factor) -> float:
    return max(-10.0, min(10.0, round((setpoint - actual) * factor, 2)))

def forecast_at_hours(forecasts: list, hours: int) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    target = now + timedelta(hours=hours)
    best, best_d = None, None
    for fc in forecasts:
        dt_str = fc.get("datetime")
        if not dt_str: continue
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            d = abs((dt - target).total_seconds())
            if best_d is None or d < best_d:
                best_d, best = d, fc
        except Exception: continue
    return best

# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
class NibeController:
    def __init__(self, logger: logging.Logger):
        self.logger  = logger
        self.cfg     = load_config()
        self.state   = load_state()
        self.history: List[dict] = load_json(HISTORY_FILE, [])
        self.ha: Optional[HAClient] = None
        self._live: dict = {}

    def reload_config(self):
        self.cfg = load_config()

    async def run(self, session: aiohttp.ClientSession):
        self.ha = HAClient(session, self.logger)
        self.logger.info("Controller started")
        await asyncio.gather(
            self._weather_loop(),
            self._indoor_loop(),
            self._price_loop(),
            self._apply_loop(),
        )

    # ── loops ──────────────────────────────────────────────────────────────
    async def _weather_loop(self):
        while True:
            try: await self._run_weather()
            except Exception as e: self.logger.error(f"weather: {e}")
            await asyncio.sleep(15 * 60)

    async def _indoor_loop(self):
        await asyncio.sleep(20)
        while True:
            try: await self._run_indoor()
            except Exception as e: self.logger.error(f"indoor: {e}")
            await asyncio.sleep(5 * 60)

    async def _price_loop(self):
        await asyncio.sleep(40)
        while True:
            try: await self._run_price()
            except Exception as e: self.logger.error(f"price: {e}")
            await asyncio.sleep(5 * 60)

    async def _apply_loop(self):
        await asyncio.sleep(60)
        while True:
            try: await self._apply()
            except Exception as e: self.logger.error(f"apply: {e}")
            await asyncio.sleep(60)

    # ── weather ────────────────────────────────────────────────────────────
    async def _run_weather(self):
        cfg = self.cfg
        if not cfg.get("weather_enabled"):
            self.state["weather_offset"] = 0.0; return
        outdoor = await self.ha.get_float(cfg.get("outdoor_temp_entity", ""))
        curve   = await self.ha.get_float(cfg.get("heat_curve_entity", ""))
        if outdoor is None or curve is None or curve == 0:
            self.logger.debug("Weather: missing outdoor/curve data"); return
        forecasts = await self.ha.get_weather_forecast(cfg.get("weather_entity", ""))
        if not forecasts:
            self.logger.warning("Weather: no forecast data"); return
        hours    = int(cfg.get("forecast_hours", 6))
        fc_now   = forecast_at_hours(forecasts, 0)
        fc_then  = forecast_at_hours(forecasts, hours)
        if not fc_then: return
        forecast_raw = float(fc_then.get("temperature", outdoor))
        if fc_now:
            t_now = float(fc_now.get("temperature", outdoor))
            forecast_corrected = round((outdoor - t_now) + forecast_raw, 2)
        else:
            forecast_corrected = forecast_raw
        sun_factor = float(cfg.get("weather_adjust_factor", 0.0))
        offset = calc_weather_offset(outdoor, forecast_corrected, curve, sun_factor,
                                     cfg.get("weather_enable_up", True),
                                     cfg.get("weather_enable_down", True))
        self.state.update({"weather_offset": offset, "last_outdoor_temp": outdoor,
                           "last_forecast_temp": forecast_corrected})
        self._live.update({"outdoor_temp": outdoor, "forecast_temp": forecast_corrected,
                           "heat_curve": curve, "forecast_condition": fc_then.get("condition", "")})
        self.logger.info(f"Weather: outdoor={outdoor} forecast@{hours}h={forecast_corrected} curve={curve} → {offset:+.2f}°C")

    # ── indoor ─────────────────────────────────────────────────────────────
    async def _run_indoor(self):
        cfg = self.cfg
        if not cfg.get("indoor_enabled"):
            self.state["indoor_offset"] = 0.0; return
        setpoint_entity = cfg.get("indoor_setpoint_entity", "")
        setpoint = (await self.ha.get_float(setpoint_entity)
                    if setpoint_entity else float(cfg.get("indoor_target_temp", 21.0)))
        actual   = await self.ha.get_float(cfg.get("indoor_temp_entity", ""))
        if actual is None or setpoint is None: return
        if actual < 4:
            self.logger.warning(f"Indoor temp {actual}°C looks like sensor fault"); return
        factor = float(cfg.get("indoor_factor", 10.0))
        offset = calc_indoor_offset(setpoint, actual, factor)
        self.state.update({"indoor_offset": offset, "last_indoor_temp": actual,
                           "last_indoor_setpoint": setpoint})
        self._live.update({"indoor_temp": actual, "indoor_setpoint": setpoint})
        self.logger.info(f"Indoor: actual={actual} setpoint={setpoint} factor={factor} → {offset:+.2f}°C")

    # ── price ──────────────────────────────────────────────────────────────
    async def _run_price(self):
        cfg = self.cfg
        if not cfg.get("price_enabled"):
            self.state["price_offset"] = 0.0; return
        price = await self.ha.get_float(cfg.get("electricity_price_entity", ""))
        if price is None: return
        level  = classify_price(price, cfg)
        offset = price_to_offset(level, cfg)
        self.state.update({"price_offset": offset, "last_price_level": level, "last_price": price})
        self._live.update({"price": price, "price_level": level})
        self.logger.info(f"Price: {price:.4f} → {level} → {offset:+.1f}°C")

    # ── apply ──────────────────────────────────────────────────────────────
    async def _apply(self):
        cfg  = self.cfg
        s    = self.state
        w    = float(s.get("weather_offset") or 0)
        ind  = float(s.get("indoor_offset")  or 0)
        p    = float(s.get("price_offset")   or 0)
        combined = max(-10.0, min(10.0, round(w + ind + p, 1)))

        min_interval = float(cfg.get("min_write_interval_min", MIN_WRITE_INTERVAL)) * 60
        elapsed      = time.time() - (s.get("last_write_ts") or 0)
        last         = s.get("last_combined_offset")
        delta        = abs(combined - last) if last is not None else 999

        if delta < 0.2: return
        if delta < 0.5 and elapsed < min_interval:
            self.logger.debug(f"Apply skipped: delta={delta:.2f} elapsed={elapsed/60:.1f}min"); return

        offset_entity = cfg.get("curve_offset_entity", "")
        if not offset_entity:
            self.logger.debug("Apply skipped: no curve_offset_entity configured"); return

        reasons = self._build_reasons(w, ind, p, cfg, s)
        self.logger.info(f"Applying {combined:+.1f}°C → {offset_entity} | {' | '.join(reasons)}")

        if await self.ha.set_number(offset_entity, combined):
            s.update({"last_combined_offset": combined, "last_write_ts": int(time.time())})
            save_json(STATE_FILE, s)
            entry = {"ts": int(time.time()), "combined": combined,
                     "weather": round(w, 2), "indoor": round(ind, 2), "price": round(p, 2),
                     "price_level": s.get("last_price_level", "UNKNOWN"),
                     "outdoor_temp": s.get("last_outdoor_temp"),
                     "indoor_temp": s.get("last_indoor_temp"),
                     "indoor_setpoint": s.get("last_indoor_setpoint"),
                     "forecast_temp": s.get("last_forecast_temp"),
                     "price_value": s.get("last_price"),
                     "reasons": reasons}
            self.history.append(entry)
            save_json(HISTORY_FILE, self.history[-MAX_HISTORY:])
            self._live["current_offset"] = combined

    def _build_reasons(self, w, ind, p, cfg, s) -> List[str]:
        out = []
        if w != 0 and cfg.get("weather_enabled"):
            f = s.get("last_forecast_temp"); o = s.get("last_outdoor_temp")
            if f is not None and o is not None:
                diff = round(abs(o - f), 1)
                direction = "colder" if f < o else "warmer"
                out.append(f"Forecast {diff}°C {direction} in {cfg.get('forecast_hours',6)}h → {w:+.2f}°C")
            else:
                out.append(f"Weather → {w:+.2f}°C")
        if ind != 0 and cfg.get("indoor_enabled"):
            a = s.get("last_indoor_temp"); sp = s.get("last_indoor_setpoint")
            if a is not None and sp is not None:
                diff = round(sp - a, 1)
                out.append(f"Indoor {a}°C is {abs(diff)}°C {'below' if diff > 0 else 'above'} {sp}°C setpoint → {ind:+.2f}°C")
            else:
                out.append(f"Indoor → {ind:+.2f}°C")
        if p != 0 and cfg.get("price_enabled"):
            lv = s.get("last_price_level", ""); pv = s.get("last_price")
            out.append(f"Electricity {lv} ({pv:.4f if pv else '?'}) → {p:+.1f}°C")
        if not out:
            out.append("Offset unchanged (all factors zero)")
        return out

    def get_status(self) -> dict:
        s = self.state
        return {
            "weather_offset":  round(float(s.get("weather_offset")  or 0), 2),
            "indoor_offset":   round(float(s.get("indoor_offset")   or 0), 2),
            "price_offset":    round(float(s.get("price_offset")    or 0), 2),
            "combined_offset": round(float(s.get("last_combined_offset") or 0), 2),
            "last_write_ts":   s.get("last_write_ts", 0),
            "price_level":     s.get("last_price_level", "UNKNOWN"),
            **{k: s.get(k) for k in ["last_outdoor_temp","last_indoor_temp",
               "last_indoor_setpoint","last_forecast_temp","last_price"]},
            **self._live,
        }

# ---------------------------------------------------------------------------
# Web application — ingress-aware
# ---------------------------------------------------------------------------
class WebApp:
    def __init__(self, ctrl: NibeController, logger: logging.Logger):
        self.ctrl   = ctrl
        self.logger = logger

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self._ingress_middleware])
        app.router.add_get("/",               self._index)
        app.router.add_get("/api/status",     self._status)
        app.router.add_get("/api/history",    self._history)
        app.router.add_get("/api/config",     self._config_get)
        app.router.add_post("/api/config",    self._config_post)
        app.router.add_get("/api/entities",   self._entities)
        return app

    @web.middleware
    async def _ingress_middleware(self, request: web.Request, handler):
        # HA Ingress passes the base path in X-Ingress-Path so the frontend
        # can prefix all fetch() and WebSocket URLs correctly.
        resp = await handler(request)
        return resp

    async def _index(self, req: web.Request) -> web.Response:
        ingress_path = req.headers.get("X-Ingress-Path", "").rstrip("/")
        html = FRONTEND.replace("__INGRESS_PATH__", ingress_path)
        return web.Response(text=html, content_type="text/html")

    async def _status(self, req: web.Request) -> web.Response:
        return web.json_response(self.ctrl.get_status())

    async def _history(self, req: web.Request) -> web.Response:
        n = int(req.rel_url.query.get("n", 200))
        return web.json_response(self.ctrl.history[-n:])

    async def _config_get(self, req: web.Request) -> web.Response:
        return web.json_response(self.ctrl.cfg)

    async def _config_post(self, req: web.Request) -> web.Response:
        try:
            body = await req.json()
            floats = ["weather_adjust_factor","indoor_target_temp","indoor_factor",
                      "price_very_cheap","price_cheap","price_normal","price_expensive",
                      "price_very_expensive","price_very_cheap_threshold","price_cheap_threshold",
                      "price_expensive_threshold","price_very_expensive_threshold",
                      "min_write_interval_min","forecast_hours"]
            bools  = ["weather_enabled","weather_enable_up","weather_enable_down",
                      "indoor_enabled","price_enabled"]
            for k in floats:
                if k in body: body[k] = float(body[k])
            for k in bools:
                if k in body: body[k] = bool(body[k])
            self.ctrl.cfg.update(body)
            save_json(CONFIG_FILE, self.ctrl.cfg)
            self.logger.info("Config saved via web UI")
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _entities(self, req: web.Request) -> web.Response:
        domain = req.rel_url.query.get("domain", "")
        return web.json_response(await self.ctrl.ha.list_entities(domain))

    async def start(self, port: int = 8099):
        app    = self.build()
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        self.logger.info(f"Web server on :{port}")

# ---------------------------------------------------------------------------
# Frontend (injected with ingress path at request time)
# ---------------------------------------------------------------------------
FRONTEND = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nibe Smart Control</title>
<script>window.__INGRESS_PATH__ = "__INGRESS_PATH__";</script>
<style>
:root{
  --bg:#0f1117;--sur:#181c27;--sur2:#1f2436;--bdr:#2a3050;
  --txt:#e4e9f7;--mut:#7b87a8;--acc:#e05c2a;--cold:#3a82f7;
  --warm:#f7953a;--grn:#2ec27e;--ylw:#f6d24a;
  --mono:"JetBrains Mono","Fira Mono",ui-monospace,monospace;
  --body:"Inter","Segoe UI",system-ui,sans-serif;
  --r:8px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--body);font-size:14px;line-height:1.5;min-height:100vh}
/* ── Layout ── */
header{background:var(--sur);border-bottom:1px solid var(--bdr);padding:14px 20px;display:flex;align-items:center;gap:12px}
header h1{font-size:16px;font-weight:600;letter-spacing:.02em}
.live{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;color:var(--mut)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--grn);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
nav{background:var(--sur);border-bottom:1px solid var(--bdr);padding:0 20px;display:flex;gap:2px}
nav button{background:none;border:none;color:var(--mut);padding:10px 14px;cursor:pointer;font:14px var(--body);border-bottom:2px solid transparent;margin-bottom:-1px;transition:all .15s}
nav button.on{color:var(--txt);border-bottom-color:var(--acc)}
main{padding:20px;max-width:1080px}
.tab{display:none}.tab.on{display:block}
/* ── Cards ── */
.card{background:var(--sur);border:1px solid var(--bdr);border-radius:var(--r);padding:18px;margin-bottom:14px}
.card h2{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin-bottom:14px}
.g3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:14px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:680px){.g3,.g2{grid-template-columns:1fr}}
/* ── Stats ── */
.stat .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin-bottom:4px}
.stat .val{font-family:var(--mono);font-size:22px;font-weight:700}
.stat .note{font-size:11px;color:var(--mut);margin-top:3px}
.pos{color:var(--warm)}.neg{color:var(--cold)}.zer{color:var(--mut)}
/* ── Price badge ── */
.badge{display:inline-flex;align-items:center;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600}
.VERY_CHEAP{background:#2ec27e22;color:var(--grn);border:1px solid #2ec27e55}
.CHEAP{background:#a3e63522;color:#a3e635;border:1px solid #a3e63555}
.NORMAL{background:#7b87a822;color:var(--mut);border:1px solid #7b87a844}
.EXPENSIVE{background:#f6d24a22;color:var(--ylw);border:1px solid #f6d24a55}
.VERY_EXPENSIVE{background:#e05c2a22;color:var(--acc);border:1px solid #e05c2a55}
/* ── Decomp bar ── */
.bar-wrap{margin:16px 0 6px}
.bar-lbl{font-size:10px;color:var(--mut);display:flex;justify-content:space-between;margin-bottom:5px}
.bar{position:relative;height:34px;background:var(--sur2);border-radius:6px;overflow:hidden;border:1px solid var(--bdr)}
.zero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--bdr);z-index:2}
.seg{position:absolute;border-radius:3px;transition:left .5s,width .5s}
.seg.W{background:#3a82f7aa;border:1px solid #3a82f7}
.seg.I{background:#2ec27eaa;border:1px solid #2ec27e}
.seg.P{background:#e05c2aaa;border:1px solid #e05c2a}
.legend{display:flex;gap:14px;margin-top:8px}
.legend span{font-size:11px;color:var(--mut);display:flex;align-items:center;gap:5px}
.ldot{width:8px;height:8px;border-radius:2px}
.ldot.W{background:#3a82f7}.ldot.I{background:#2ec27e}.ldot.P{background:#e05c2a}
/* ── History ── */
.hlist{display:flex;flex-direction:column}
.hrow{display:grid;grid-template-columns:120px 72px 1fr;gap:10px;padding:11px 0;border-bottom:1px solid var(--bdr);align-items:start}
.hrow:last-child{border:none}
.hts{font-family:var(--mono);font-size:11px;color:var(--mut)}
.hoff{font-family:var(--mono);font-size:15px;font-weight:700}
.hrsn{font-size:12px;color:var(--mut);line-height:1.7}
.tag{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;font-weight:600;margin-right:4px}
.wt{color:#3a82f7;border:1px solid #3a82f744;background:#3a82f711}
.it{color:#2ec27e;border:1px solid #2ec27e44;background:#2ec27e11}
.pt{color:#e05c2a;border:1px solid #e05c2a44;background:#e05c2a11}
/* ── Settings form ── */
.fsec{margin-bottom:24px}
.fsec h3{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--acc);margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--bdr)}
.fg{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:680px){.fg{grid-template-columns:1fr}}
.fld{display:flex;flex-direction:column;gap:5px}
.fld label{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut)}
.fld input,.fld select{background:var(--sur2);border:1px solid var(--bdr);color:var(--txt);padding:8px 10px;border-radius:6px;font:13px var(--body);width:100%;transition:border-color .15s}
.fld input:focus,.fld select:focus{outline:none;border-color:var(--acc)}
.fld .hint{font-size:11px;color:var(--mut)}
/* Entity picker with dropdown */
.epick{position:relative}
.edrop{display:none;position:absolute;top:100%;left:0;right:0;z-index:200;background:var(--sur2);border:1px solid var(--acc);border-top:none;border-radius:0 0 6px 6px;max-height:180px;overflow-y:auto}
.edrop.open{display:block}
.eopt{padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--bdr);font-size:12px}
.eopt:hover{background:rgba(224,92,42,.1)}
.eopt .eid{font-size:10px;color:var(--mut)}
/* Toggle */
.trow{display:flex;align-items:center;gap:10px}
.tog{position:relative;width:36px;height:20px;flex-shrink:0}
.tog input{opacity:0;width:0;height:0;position:absolute}
.sldr{position:absolute;inset:0;background:var(--bdr);border-radius:20px;cursor:pointer;transition:.2s}
.sldr:before{content:"";position:absolute;height:14px;width:14px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
.tog input:checked+.sldr{background:var(--acc)}
.tog input:checked+.sldr:before{transform:translateX(16px)}
/* Buttons */
.sbtn{background:var(--acc);color:#fff;border:none;border-radius:6px;padding:10px 26px;font:600 14px var(--body);cursor:pointer;transition:opacity .15s}
.sbtn:hover{opacity:.85}
.smsg{font-size:12px;color:var(--grn);margin-left:12px;opacity:0;transition:opacity .3s}
.smsg.show{opacity:1}
</style>
</head>
<body>
<header>
  <svg width="26" height="26" viewBox="0 0 26 26" fill="none">
    <circle cx="13" cy="13" r="12" stroke="#e05c2a" stroke-width="1.5"/>
    <path d="M13 21C13 21 7.5 16.5 7.5 11.5A5.5 5.5 0 0 1 18.5 11.5C18.5 16.5 13 21 13 21Z" fill="#e05c2a33" stroke="#e05c2a" stroke-width="1.2"/>
    <circle cx="13" cy="11.5" r="2.2" fill="#e05c2a"/>
  </svg>
  <h1>Nibe Smart Control</h1>
  <div class="live"><div class="dot" id="dot"></div><span id="liveTs">—</span></div>
</header>
<nav>
  <button class="on" onclick="showTab('db',this)">Dashboard</button>
  <button onclick="showTab('hist',this)">History</button>
  <button onclick="showTab('charts',this)">Charts</button>
  <button onclick="showTab('cfg',this)">Settings</button>
</nav>
<main>

<!-- DASHBOARD -->
<div id="tab-db" class="tab on">
  <div class="g3">
    <div class="card"><h2>Combined offset</h2><div class="stat"><div class="lbl">Written to heat pump</div><div class="val zer" id="dCombined">—</div><div class="note" id="dLastWrite">—</div></div></div>
    <div class="card"><h2>Outdoor</h2><div class="stat"><div class="lbl">Current</div><div class="val" id="dOutdoor">—</div><div class="note" id="dForecast">—</div></div></div>
    <div class="card"><h2>Electricity</h2><div class="stat"><div class="lbl">Current price</div><div class="val" id="dPrice">—</div><div class="note" id="dLevel"></div></div></div>
  </div>
  <div class="card">
    <h2>Offset decomposition</h2>
    <div class="bar-wrap">
      <div class="bar-lbl"><span>−10°C</span><span>0</span><span>+10°C</span></div>
      <div class="bar"><div class="zero"></div><div class="seg W" id="sW"></div><div class="seg I" id="sI"></div><div class="seg P" id="sP"></div></div>
    </div>
    <div class="legend">
      <span><div class="ldot W"></div>Weather <b id="lW">—</b></span>
      <span><div class="ldot I"></div>Indoor <b id="lI">—</b></span>
      <span><div class="ldot P"></div>Price <b id="lP">—</b></span>
    </div>
  </div>
  <div class="g2">
    <div class="card"><h2>Indoor</h2><div class="stat"><div class="lbl">Actual</div><div class="val" id="dIndoor">—</div><div class="note" id="dIndoorSet">—</div></div></div>
    <div class="card"><h2>Next write</h2><div class="stat"><div class="lbl">Rate limiting</div><div class="val" id="dNextWrite" style="font-size:16px">—</div><div class="note" id="dNextWriteSub">—</div></div></div>
  </div>
</div>

<!-- HISTORY -->
<div id="tab-hist" class="tab">
  <div class="card"><h2>Change log</h2><div class="hlist" id="histList"><div style="color:var(--mut)">Loading…</div></div></div>
</div>

<!-- CHARTS -->
<div id="tab-charts" class="tab">
  <div class="card"><h2>Offset over time</h2><canvas id="cOffset" height="200"></canvas></div>
  <div class="card"><h2>Temperatures</h2><canvas id="cTemps" height="200"></canvas></div>
  <div class="card"><h2>Electricity price</h2><canvas id="cPrice" height="160"></canvas></div>
</div>

<!-- SETTINGS -->
<div id="tab-cfg" class="tab">
  <div class="card">
    <h2>Configuration — all changes saved immediately</h2>
    <form id="cfgForm">

      <div class="fsec">
        <h3>Heat pump entities (NibeGW / ESPHome)</h3>
        <div class="fg">
          <div class="fld"><label>Outdoor temperature sensor</label><div class="epick"><input name="outdoor_temp_entity" autocomplete="off" placeholder="sensor.nibe_outdoor_temperature" oninput="suggest(this,'sensor')"><div class="edrop" id="dd_outdoor_temp_entity"></div></div></div>
          <div class="fld"><label>Heat curve (read-only)</label><div class="epick"><input name="heat_curve_entity" autocomplete="off" placeholder="number.nibe_heat_curve_s1" oninput="suggest(this,'number')"><div class="edrop" id="dd_heat_curve_entity"></div></div></div>
          <div class="fld"><label>Curve offset entity (addon writes here)</label><div class="epick"><input name="curve_offset_entity" autocomplete="off" placeholder="number.nibe_heat_offset_s1" oninput="suggest(this,'number')"><div class="edrop" id="dd_curve_offset_entity"></div></div></div>
        </div>
      </div>

      <div class="fsec">
        <h3>Weather forecast</h3>
        <div class="fg">
          <div class="fld"><label>Weather entity</label><div class="epick"><input name="weather_entity" autocomplete="off" placeholder="weather.forecast_home" oninput="suggest(this,'weather')"><div class="edrop" id="dd_weather_entity"></div></div></div>
          <div class="fld"><label>Forecast lookahead (hours)</label><input type="number" name="forecast_hours" min="1" max="24" step="1"></div>
          <div class="fld"><label>Static bias (°C)</label><input type="number" name="weather_adjust_factor" min="-2" max="2" step="0.5"><span class="hint">Fine-tune if pump over/undershoots</span></div>
        </div>
        <div style="display:flex;gap:20px;flex-wrap:wrap;margin-top:10px">
          <div class="fld"><div class="trow"><label class="tog"><input type="checkbox" name="weather_enabled"><span class="sldr"></span></label><span>Enable weather control</span></div></div>
          <div class="fld"><div class="trow"><label class="tog"><input type="checkbox" name="weather_enable_up"><span class="sldr"></span></label><span>Allow raising offset</span></div></div>
          <div class="fld"><div class="trow"><label class="tog"><input type="checkbox" name="weather_enable_down"><span class="sldr"></span></label><span>Allow lowering offset</span></div></div>
        </div>
      </div>

      <div class="fsec">
        <h3>Indoor temperature</h3>
        <div class="fg">
          <div class="fld"><label>Indoor temperature sensor</label><div class="epick"><input name="indoor_temp_entity" autocomplete="off" placeholder="sensor.living_room_temperature" oninput="suggest(this,'sensor')"><div class="edrop" id="dd_indoor_temp_entity"></div></div></div>
          <div class="fld"><label>Setpoint entity (optional)</label><div class="epick"><input name="indoor_setpoint_entity" autocomplete="off" placeholder="Leave blank → use target below" oninput="suggest(this,'sensor,number,input_number')"><div class="edrop" id="dd_indoor_setpoint_entity"></div></div></div>
          <div class="fld"><label>Target indoor temp (°C)</label><input type="number" name="indoor_target_temp" min="10" max="28" step="0.5"><span class="hint">Used when no setpoint entity</span></div>
          <div class="fld"><label>P-factor</label><input type="number" name="indoor_factor" min="1" max="50" step="1"><span class="hint">offset = (setpoint − actual) × factor. Default: 10</span></div>
        </div>
        <div style="margin-top:10px">
          <div class="fld"><div class="trow"><label class="tog"><input type="checkbox" name="indoor_enabled"><span class="sldr"></span></label><span>Enable indoor temperature control</span></div></div>
        </div>
      </div>

      <div class="fsec">
        <h3>Electricity price</h3>
        <div class="fg">
          <div class="fld"><label>Price sensor</label><div class="epick"><input name="electricity_price_entity" autocomplete="off" placeholder="sensor.nordpool_kwh_lt_eur_3_10_025" oninput="suggest(this,'sensor')"><div class="edrop" id="dd_electricity_price_entity"></div></div></div>
        </div>
        <div style="margin:10px 0">
          <div class="fld"><div class="trow"><label class="tog"><input type="checkbox" name="price_enabled"><span class="sldr"></span></label><span>Enable price control</span></div></div>
        </div>
        <p style="font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Curve offsets per price level (°C)</p>
        <div class="fg">
          <div class="fld"><label>Very Cheap</label><input type="number" name="price_very_cheap" min="-5" max="5" step="0.5"></div>
          <div class="fld"><label>Cheap</label><input type="number" name="price_cheap" min="-5" max="5" step="0.5"></div>
          <div class="fld"><label>Normal</label><input type="number" name="price_normal" min="-5" max="5" step="0.5"></div>
          <div class="fld"><label>Expensive</label><input type="number" name="price_expensive" min="-5" max="5" step="0.5"></div>
          <div class="fld"><label>Very Expensive</label><input type="number" name="price_very_expensive" min="-5" max="5" step="0.5"></div>
        </div>
        <p style="font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin:12px 0 8px">Thresholds (same unit as your price sensor, e.g. EUR/kWh)</p>
        <div class="fg">
          <div class="fld"><label>Very Cheap ≤</label><input type="number" name="price_very_cheap_threshold" min="0" step="0.01"></div>
          <div class="fld"><label>Cheap ≤</label><input type="number" name="price_cheap_threshold" min="0" step="0.01"></div>
          <div class="fld"><label>Expensive ≥</label><input type="number" name="price_expensive_threshold" min="0" step="0.01"></div>
          <div class="fld"><label>Very Expensive ≥</label><input type="number" name="price_very_expensive_threshold" min="0" step="0.01"></div>
        </div>
      </div>

      <div class="fsec">
        <h3>Rate limiting</h3>
        <div class="fg">
          <div class="fld"><label>Min minutes between writes</label><input type="number" name="min_write_interval_min" min="5" max="120" step="5"><span class="hint">Protects the heat pump compressor</span></div>
        </div>
      </div>

      <div style="display:flex;align-items:center">
        <button type="submit" class="sbtn">Save</button>
        <span class="smsg" id="smsg">Saved ✓</span>
      </div>
    </form>
  </div>
</div>

</main>
<script>
// ── Ingress-aware fetch ────────────────────────────────────────────────────
const BASE = (window.__INGRESS_PATH__ || '').replace(/\/+$/, '');
const api = async (path, opts={}) => {
  const url = BASE + '/' + path.replace(/^\/+/,'');
  const r = await fetch(url, {headers:{'Content-Type':'application/json'}, ...opts});
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
};
const GET  = p => api(p);
const POST = (p,d) => api(p,{method:'POST',body:JSON.stringify(d)});

// ── Tabs ──────────────────────────────────────────────────────────────────
function showTab(id, btn) {
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
  document.querySelectorAll('nav button').forEach(b=>b.classList.remove('on'));
  document.getElementById('tab-'+id).classList.add('on');
  btn.classList.add('on');
  if (id==='hist')   loadHistory();
  if (id==='charts') loadCharts();
  if (id==='cfg')    loadConfig();
}

// ── Helpers ───────────────────────────────────────────────────────────────
const fmt = (v,u='°C') => v==null ? '—' : (v>0?'+':'')+Number(v).toFixed(1)+u;
const fmtTs = ts => ts ? new Date(ts*1000).toLocaleString('en-GB',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const cc = v => Number(v)>0.05?'pos':Number(v)<-0.05?'neg':'zer';
const pct = v => 50+(v/10)*50;

// ── Decomp bar ────────────────────────────────────────────────────────────
function renderBar(w,i,p){
  [{id:'sW',v:w},{id:'sI',v:i},{id:'sP',v:p}].forEach((s,idx)=>{
    const el=document.getElementById(s.id);
    const l=Math.min(50,pct(s.v)), r=Math.max(50,pct(s.v));
    const rh=Math.floor((34-8)/3);
    el.style.left=l+'%'; el.style.width=(r-l)+'%';
    el.style.top=(4+idx*rh)+'px'; el.style.height=(rh-2)+'px'; el.style.bottom='auto';
  });
}

// ── Dashboard ─────────────────────────────────────────────────────────────
async function updateDashboard(){
  let d;
  try { d=await GET('api/status'); } catch(e){ document.getElementById('dot').style.background='#e05c2a'; return; }
  document.getElementById('dot').style.background='#2ec27e';
  document.getElementById('liveTs').textContent=new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});

  const el=document.getElementById('dCombined');
  el.textContent=fmt(d.combined_offset); el.className='val '+cc(d.combined_offset);

  document.getElementById('dOutdoor').textContent=d.last_outdoor_temp!=null?d.last_outdoor_temp.toFixed(1)+'°C':'—';
  document.getElementById('dForecast').textContent=d.last_forecast_temp!=null?'Forecast → '+d.last_forecast_temp.toFixed(1)+'°C':'';
  document.getElementById('dPrice').textContent=d.last_price!=null?d.last_price.toFixed(4):'—';
  const lv=d.price_level||'UNKNOWN';
  document.getElementById('dLevel').innerHTML=`<span class="badge ${lv}">${lv.replace('_',' ')}</span>`;
  document.getElementById('dIndoor').textContent=d.last_indoor_temp!=null?d.last_indoor_temp.toFixed(1)+'°C':'—';
  document.getElementById('dIndoorSet').textContent=d.last_indoor_setpoint!=null?'Setpoint → '+d.last_indoor_setpoint.toFixed(1)+'°C':'';

  // Last / next write
  document.getElementById('dNextWrite').textContent=fmtTs(d.last_write_ts);
  if(d.last_write_ts && window._cfg){
    const rem=Math.max(0,window._cfg.min_write_interval_min*60-(Date.now()/1000-d.last_write_ts));
    document.getElementById('dNextWriteSub').textContent=rem>0?`Next in ${Math.ceil(rem/60)} min`:'Ready';
  }

  // Decomp bar
  document.getElementById('lW').textContent=fmt(d.weather_offset);
  document.getElementById('lI').textContent=fmt(d.indoor_offset);
  document.getElementById('lP').textContent=fmt(d.price_offset);
  renderBar(d.weather_offset||0,d.indoor_offset||0,d.price_offset||0);
}

// ── History ───────────────────────────────────────────────────────────────
async function loadHistory(){
  const el=document.getElementById('histList');
  el.innerHTML='<div style="color:var(--mut)">Loading…</div>';
  let h; try{h=await GET('api/history');}catch(e){el.innerHTML='<div style="color:var(--acc)">Error</div>';return;}
  if(!h.length){el.innerHTML='<div style="color:var(--mut);font-size:13px">No changes recorded yet.</div>';return;}
  el.innerHTML='';
  [...h].reverse().forEach(e=>{
    const div=document.createElement('div'); div.className='hrow';
    const rhtml=(e.reasons||[]).map(r=>{
      let cls=''; if(r.includes('Forecast')||r.includes('Weather'))cls='wt';
      else if(r.includes('Indoor'))cls='it';
      else if(r.includes('Electricity')||r.includes('CHEAP')||r.includes('EXPENSIVE'))cls='pt';
      return `<span class="tag ${cls}">${cls==='wt'?'⛅':cls==='it'?'🌡':'⚡'}</span>${r}`;
    }).join('<br>');
    div.innerHTML=`<div class="hts">${fmtTs(e.ts)}</div><div class="hoff ${cc(e.combined)}">${fmt(e.combined)}</div><div class="hrsn">${rhtml}</div>`;
    el.appendChild(div);
  });
}

// ── Charts ────────────────────────────────────────────────────────────────
let charts={};
async function loadCharts(){
  if(!window.Chart){
    await new Promise((res,rej)=>{const s=document.createElement('script');s.src='https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js';s.onload=res;s.onerror=rej;document.head.appendChild(s);});
    Chart.defaults.color='#7b87a8'; Chart.defaults.borderColor='#2a3050';
  }
  let h; try{h=await GET('api/history?n=200');}catch(e){return;}
  if(!h.length) return;
  const lbl=h.map(e=>fmtTs(e.ts));
  const copts={responsive:true,animation:false,plugins:{legend:{labels:{usePointStyle:true,boxWidth:8,padding:14}},tooltip:{mode:'index',intersect:false}},scales:{x:{ticks:{maxTicksLimit:8,maxRotation:0},grid:{color:'#2a3050'}},y:{grid:{color:'#2a3050'}}}};

  const c1=document.getElementById('cOffset').getContext('2d');
  if(charts.o) charts.o.destroy();
  charts.o=new Chart(c1,{type:'line',data:{labels:lbl,datasets:[
    {label:'Combined',data:h.map(e=>e.combined),borderColor:'#e05c2a',backgroundColor:'#e05c2a22',fill:true,tension:.3,pointRadius:2},
    {label:'Weather', data:h.map(e=>e.weather), borderColor:'#3a82f7',backgroundColor:'transparent',tension:.3,pointRadius:2},
    {label:'Indoor',  data:h.map(e=>e.indoor),  borderColor:'#2ec27e',backgroundColor:'transparent',tension:.3,pointRadius:2},
    {label:'Price',   data:h.map(e=>e.price),   borderColor:'#f6d24a',backgroundColor:'transparent',tension:.3,pointRadius:2},
  ]},options:{...copts}});

  const c2=document.getElementById('cTemps').getContext('2d');
  if(charts.t) charts.t.destroy();
  charts.t=new Chart(c2,{type:'line',data:{labels:lbl,datasets:[
    {label:'Outdoor',   data:h.map(e=>e.outdoor_temp),    borderColor:'#7b87a8',tension:.3,pointRadius:2},
    {label:'Forecast',  data:h.map(e=>e.forecast_temp),   borderColor:'#3a82f7',borderDash:[4,3],tension:.3,pointRadius:2},
    {label:'Indoor',    data:h.map(e=>e.indoor_temp),     borderColor:'#2ec27e',tension:.3,pointRadius:2},
    {label:'Setpoint',  data:h.map(e=>e.indoor_setpoint), borderColor:'#2ec27e66',borderDash:[2,3],tension:.3,pointRadius:0},
  ]},options:{...copts}});

  const c3=document.getElementById('cPrice').getContext('2d');
  if(charts.p) charts.p.destroy();
  charts.p=new Chart(c3,{type:'bar',data:{labels:lbl,datasets:[{label:'Price',data:h.map(e=>e.price_value),backgroundColor:h.map(e=>{const lv=e.price_level;return lv==='VERY_CHEAP'?'#2ec27e88':lv==='CHEAP'?'#a3e63588':lv==='NORMAL'?'#7b87a888':lv==='EXPENSIVE'?'#f6d24a88':'#e05c2a88';}),borderWidth:0}]},options:{...copts,scales:{...copts.scales,y:{...copts.scales.y,min:0}}}});
}

// ── Entity autocomplete ───────────────────────────────────────────────────
let _entCache={};
async function fetchEntities(domain){
  if(_entCache[domain]) return _entCache[domain];
  try{ _entCache[domain]=await GET('api/entities?domain='+domain); }catch(e){ _entCache[domain]=[]; }
  return _entCache[domain];
}
async function suggest(input, domains){
  const q=input.value.toLowerCase();
  const ddId='dd_'+input.name;
  const dd=document.getElementById(ddId);
  if(!dd||q.length<2){if(dd)dd.classList.remove('open');return;}
  const all=[];
  for(const d of domains.split(',')){ const r=await fetchEntities(d.trim()); all.push(...r); }
  const hits=all.filter(e=>e.entity_id.toLowerCase().includes(q)||e.friendly_name.toLowerCase().includes(q)).slice(0,12);
  if(!hits.length){dd.classList.remove('open');return;}
  dd.innerHTML=hits.map(e=>`<div class="eopt" onclick="pickEntity('${input.name}','${e.entity_id}')"><div>${e.friendly_name}</div><div class="eid">${e.entity_id}${e.unit?' · '+e.unit:''}</div></div>`).join('');
  dd.classList.add('open');
}
function pickEntity(name, eid){
  const input=document.querySelector(`[name="${name}"]`);
  if(input){ input.value=eid; }
  const dd=document.getElementById('dd_'+name);
  if(dd) dd.classList.remove('open');
}
document.addEventListener('click', e=>{
  if(!e.target.closest('.epick')) document.querySelectorAll('.edrop').forEach(d=>d.classList.remove('open'));
});

// ── Config load / save ────────────────────────────────────────────────────
async function loadConfig(){
  const cfg=await GET('api/config');
  window._cfg=cfg;
  const form=document.getElementById('cfgForm');
  Object.entries(cfg).forEach(([k,v])=>{
    const el=form.elements[k];
    if(!el) return;
    if(el.type==='checkbox') el.checked=!!v;
    else el.value=v;
  });
}
document.getElementById('cfgForm').addEventListener('submit', async e=>{
  e.preventDefault();
  const form=e.target, data={};
  form.querySelectorAll('input:not([type=checkbox]),select').forEach(el=>{ if(el.name) data[el.name]=el.type==='number'?Number(el.value):el.value; });
  form.querySelectorAll('input[type=checkbox]').forEach(el=>{ if(el.name) data[el.name]=el.checked; });
  const r=await POST('api/config',data).catch(e=>({ok:false,error:e.message}));
  const msg=document.getElementById('smsg');
  msg.textContent=r.ok?'Saved ✓':'Error: '+r.error;
  msg.style.color=r.ok?'var(--grn)':'var(--acc)';
  msg.classList.add('show');
  setTimeout(()=>msg.classList.remove('show'),3000);
  if(r.ok) window._cfg=data;
});

// ── Init ──────────────────────────────────────────────────────────────────
(async()=>{
  await updateDashboard();
  GET('api/config').then(cfg=>{ window._cfg=cfg; });
  setInterval(updateDashboard, 30000);
})();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Read log level from addon options (/data/options.json written by Supervisor)
    options_path = DATA_DIR / "options.json"
    options = load_json(options_path, {"log_level": "info"})
    log_level = options.get("log_level", "info")

    logger = setup_logging(log_level)
    logger.info("Nibe Smart Control v1.2.0 starting (ingress mode)")

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        ctrl   = NibeController(logger)
        webapp = WebApp(ctrl, logger)
        await webapp.start(port=8099)
        logger.info(f"Config: weather={ctrl.cfg.get('weather_enabled')} indoor={ctrl.cfg.get('indoor_enabled')} price={ctrl.cfg.get('price_enabled')}")
        await ctrl.run(session)

if __name__ == "__main__":
    asyncio.run(main())
