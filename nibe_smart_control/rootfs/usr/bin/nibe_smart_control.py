#!/usr/bin/env python3
"""
Nibe Smart Control - Home Assistant Addon
==========================================
Intelligent heat curve control for Nibe F-series heat pumps.
Runs a local web UI on port 8099.

Control loops:
  weather   — fetches HA weather forecast, adjusts curve based on predicted temp N hours ahead
  indoor    — proportional controller: (setpoint - actual) × factor → curve offset
  price     — classifies electricity price → curve offset per level
  apply     — combines all three, rate-limits writes, persists history
"""

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

import aiohttp
from aiohttp import web

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR      = Path("/data")
STATE_FILE    = DATA_DIR / "state.json"
HISTORY_FILE  = DATA_DIR / "history.json"
CONFIG_FILE   = DATA_DIR / "options.json"
STATIC_DIR    = Path("/usr/share/nibe_smart_control")

LOG_LEVEL_MAP = {"debug": logging.DEBUG, "info": logging.INFO,
                 "warning": logging.WARNING, "error": logging.ERROR}

MAX_HISTORY   = 500   # entries kept on disk
MAX_HISTORY_MEM = 200  # entries served to UI

# Rate limiting: minimum minutes between heat pump writes
MIN_WRITE_INTERVAL_MIN = 10

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(level_str: str) -> logging.Logger:
    level = LOG_LEVEL_MAP.get(level_str.lower(), logging.INFO)
    logging.basicConfig(level=level,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S", stream=sys.stdout)
    return logging.getLogger("nibe_smart_control")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "weather_entity": "weather.forecast_home",
    "electricity_price_entity": "sensor.nordpool_kwh_lt_eur_3_10_025",
    "outdoor_temp_entity": "sensor.nibe_outdoor_temperature",
    "indoor_temp_entity": "",
    "indoor_setpoint_entity": "",
    "indoor_factor": 10.0,
    "heat_curve_entity": "number.nibe_heat_curve_s1",
    "curve_offset_entity": "number.nibe_heat_offset_s1",
    "forecast_hours": 6,
    "weather_enabled": True,
    "weather_enable_up": True,
    "weather_enable_down": True,
    "weather_adjust_factor": 0.0,
    "indoor_enabled": False,
    "indoor_target_temp": 21.0,
    "price_enabled": True,
    "price_very_cheap": 2.0,
    "price_cheap": 1.0,
    "price_normal": 0.0,
    "price_expensive": -1.0,
    "price_very_expensive": -2.0,
    "price_very_cheap_threshold": 0.05,
    "price_cheap_threshold": 0.08,
    "price_expensive_threshold": 0.14,
    "price_very_expensive_threshold": 0.20,
    "min_write_interval_min": MIN_WRITE_INTERVAL_MIN,
    "log_level": "info",
}

def load_config() -> dict:
    path = CONFIG_FILE
    if not path.exists():
        path = Path(__file__).parent / "options.json"
    try:
        with open(path) as f:
            cfg = json.load(f)
        merged = {**DEFAULT_CONFIG, **cfg}
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# State & History persistence
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "weather_offset": 0.0, "indoor_offset": 0.0, "price_offset": 0.0,
        "last_combined_offset": None, "last_write_ts": 0,
        "last_price_level": "UNKNOWN", "last_forecast_temp": None,
        "last_outdoor_temp": None, "last_indoor_temp": None,
        "last_indoor_setpoint": None, "last_price": None,
    }

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

def load_history() -> List[dict]:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_history(history: List[dict]):
    # Keep last MAX_HISTORY entries
    trimmed = history[-MAX_HISTORY:]
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(trimmed, f)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# HA API client
# ---------------------------------------------------------------------------
class HAClient:
    def __init__(self, session: aiohttp.ClientSession, logger: logging.Logger):
        self.session = session
        self.logger = logger
        self.base = "http://supervisor/core/api"
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def get_state(self, entity_id: str) -> Optional[dict]:
        if not entity_id or entity_id.strip() == "":
            return None
        url = f"{self.base}/states/{entity_id}"
        try:
            async with self.session.get(url, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
                self.logger.debug(f"GET {entity_id} → {r.status}")
                return None
        except Exception as e:
            self.logger.error(f"get_state({entity_id}): {e}")
            return None

    async def get_float(self, entity_id: str) -> Optional[float]:
        s = await self.get_state(entity_id)
        if s is None:
            return None
        raw = s.get("state")
        if raw in (None, "unavailable", "unknown", ""):
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    async def set_number(self, entity_id: str, value: float) -> bool:
        url = f"{self.base}/services/number/set_value"
        payload = {"entity_id": entity_id, "value": str(round(value, 1))}
        try:
            async with self.session.post(url, headers=self.headers, json=payload,
                                         timeout=aiohttp.ClientTimeout(total=10)) as r:
                ok = r.status in (200, 201)
                if not ok:
                    body = await r.text()
                    self.logger.warning(f"set_number({entity_id}, {value}) → {r.status}: {body}")
                return ok
        except Exception as e:
            self.logger.error(f"set_number({entity_id}, {value}): {e}")
            return False

    async def get_weather_forecast(self, entity_id: str) -> Optional[list]:
        # New service API (HA 2024.x)
        url = f"{self.base}/services/weather/get_forecasts"
        try:
            async with self.session.post(url, headers=self.headers,
                                         json={"entity_id": entity_id, "type": "hourly"},
                                         timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status in (200, 201):
                    data = await r.json()
                    # Response can be list or dict
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                fc = item.get("response", {}).get(entity_id, {}).get("forecast")
                                if fc:
                                    return sorted(fc, key=lambda x: x.get("datetime", ""))
                    if isinstance(data, dict):
                        fc = data.get(entity_id, {}).get("forecast")
                        if fc:
                            return sorted(fc, key=lambda x: x.get("datetime", ""))
        except Exception as e:
            self.logger.debug(f"get_forecasts service error: {e}")
        # Fallback: attributes
        s = await self.get_state(entity_id)
        if s:
            fc = s.get("attributes", {}).get("forecast", [])
            if fc:
                return sorted(fc, key=lambda x: x.get("datetime", ""))
        return None

    async def fire_event(self, event_type: str, data: dict):
        url = f"{self.base}/events/{event_type}"
        try:
            async with self.session.post(url, headers=self.headers, json=data,
                                         timeout=aiohttp.ClientTimeout(total=5)):
                pass
        except Exception:
            pass

    async def list_entities(self, domain: str = "") -> List[dict]:
        """Return all entity states, optionally filtered by domain."""
        url = f"{self.base}/states"
        try:
            async with self.session.get(url, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    states = await r.json()
                    if domain:
                        states = [s for s in states if s["entity_id"].startswith(domain + ".")]
                    return [{"entity_id": s["entity_id"],
                             "state": s.get("state"),
                             "friendly_name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
                             "unit": s.get("attributes", {}).get("unit_of_measurement", "")}
                            for s in states]
        except Exception as e:
            self.logger.error(f"list_entities: {e}")
        return []

# ---------------------------------------------------------------------------
# Price helpers
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
    return float({
        "VERY_CHEAP":     cfg.get("price_very_cheap",     2.0),
        "CHEAP":          cfg.get("price_cheap",           1.0),
        "NORMAL":         cfg.get("price_normal",          0.0),
        "EXPENSIVE":      cfg.get("price_expensive",      -1.0),
        "VERY_EXPENSIVE": cfg.get("price_very_expensive", -2.0),
    }.get(level, 0.0))

# ---------------------------------------------------------------------------
# Weather formula (NibePi verbatim)
# ---------------------------------------------------------------------------
def calc_weather_offset(outdoor: float, forecast: float, curve: float,
                        sun_factor: float = 0.0,
                        enable_up: bool = True, enable_down: bool = True) -> float:
    if curve == 0:
        return 0.0
    raw = (outdoor - forecast - sun_factor) * (curve * 1.2 / 10) / ((curve / 10) + 1)
    raw = round(raw, 2)
    if raw > 0 and not enable_up:
        raw = 0.0
    if raw < 0 and not enable_down:
        raw = 0.0
    return max(-10.0, min(10.0, raw))

# ---------------------------------------------------------------------------
# Indoor formula (NibePi verbatim): (setpoint - actual) × factor
# ---------------------------------------------------------------------------
def calc_indoor_offset(setpoint: float, actual: float, factor: float) -> float:
    raw = round((setpoint - actual) * factor, 2)
    return max(-10.0, min(10.0, raw))

# ---------------------------------------------------------------------------
# Forecast helpers
# ---------------------------------------------------------------------------
def forecast_at_hours(forecasts: list, hours: int) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    target = now + timedelta(hours=hours)
    best, best_d = None, None
    for fc in forecasts:
        dt_str = fc.get("datetime")
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            d = abs((dt - target).total_seconds())
            if best_d is None or d < best_d:
                best_d, best = d, fc
        except Exception:
            continue
    return best

# ---------------------------------------------------------------------------
# History entry builder
# ---------------------------------------------------------------------------
def make_history_entry(combined: float, weather_offset: float, indoor_offset: float,
                       price_offset: float, state: dict, reasons: List[str]) -> dict:
    return {
        "ts": int(time.time()),
        "combined": round(combined, 2),
        "weather": round(weather_offset, 2),
        "indoor": round(indoor_offset, 2),
        "price": round(price_offset, 2),
        "price_level": state.get("last_price_level", "UNKNOWN"),
        "outdoor_temp": state.get("last_outdoor_temp"),
        "indoor_temp": state.get("last_indoor_temp"),
        "indoor_setpoint": state.get("last_indoor_setpoint"),
        "forecast_temp": state.get("last_forecast_temp"),
        "price": state.get("last_price"),
        "reasons": reasons,
    }

# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
class NibeController:

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.state = load_state()
        self.history: List[dict] = load_history()
        self.ha: Optional[HAClient] = None
        self._live: Dict[str, Any] = {}   # live sensor snapshot for UI

    # ---- loops ----
    async def run(self, session: aiohttp.ClientSession):
        self.ha = HAClient(session, self.logger)
        self.logger.info("Controller started.")
        await asyncio.gather(
            self._weather_loop(),
            self._indoor_loop(),
            self._price_loop(),
            self._apply_loop(),
        )

    async def _weather_loop(self):
        while True:
            try:
                await self._run_weather()
            except Exception as e:
                self.logger.error(f"weather loop: {e}")
            await asyncio.sleep(15 * 60)

    async def _indoor_loop(self):
        await asyncio.sleep(20)
        while True:
            try:
                await self._run_indoor()
            except Exception as e:
                self.logger.error(f"indoor loop: {e}")
            await asyncio.sleep(5 * 60)

    async def _price_loop(self):
        await asyncio.sleep(40)
        while True:
            try:
                await self._run_price()
            except Exception as e:
                self.logger.error(f"price loop: {e}")
            await asyncio.sleep(5 * 60)

    async def _apply_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                await self._apply()
            except Exception as e:
                self.logger.error(f"apply loop: {e}")
            await asyncio.sleep(60)

    # ---- weather ----
    async def _run_weather(self):
        cfg = self.cfg
        if not cfg.get("weather_enabled", True):
            self.state["weather_offset"] = 0.0
            return

        outdoor = await self.ha.get_float(cfg.get("outdoor_temp_entity", ""))
        if outdoor is None:
            self.logger.warning("Outdoor temp unavailable")
            return
        curve = await self.ha.get_float(cfg.get("heat_curve_entity", ""))
        if curve is None or curve == 0:
            self.logger.warning("Heat curve unavailable or 0")
            return

        hours = int(cfg.get("forecast_hours", 6))
        forecasts = await self.ha.get_weather_forecast(cfg.get("weather_entity", ""))
        if not forecasts:
            self.logger.warning("No forecast data")
            self.state["weather_offset"] = 0.0
            return

        fc_now  = forecast_at_hours(forecasts, 0)
        fc_then = forecast_at_hours(forecasts, hours)
        if not fc_then:
            return

        forecast_raw = float(fc_then.get("temperature", outdoor))
        # Bias correction (align model to actual sensor)
        if fc_now:
            t_now = float(fc_now.get("temperature", outdoor))
            forecast_corrected = round((outdoor - t_now) + forecast_raw, 2)
        else:
            forecast_corrected = forecast_raw

        sun_factor = float(cfg.get("weather_adjust_factor", 0.0))
        offset = calc_weather_offset(
            outdoor, forecast_corrected, curve, sun_factor,
            cfg.get("weather_enable_up", True),
            cfg.get("weather_enable_down", True))

        self.state["weather_offset"] = offset
        self.state["last_outdoor_temp"] = outdoor
        self.state["last_forecast_temp"] = forecast_corrected
        self._live["outdoor_temp"] = outdoor
        self._live["forecast_temp"] = forecast_corrected
        self._live["heat_curve"] = curve
        self._live["forecast_condition"] = fc_then.get("condition", "")
        self.logger.info(f"Weather: outdoor={outdoor}°C forecast@{hours}h={forecast_corrected}°C curve={curve} → offset={offset:+.2f}°C")

    # ---- indoor ----
    async def _run_indoor(self):
        cfg = self.cfg
        if not cfg.get("indoor_enabled", False):
            self.state["indoor_offset"] = 0.0
            return

        indoor_entity  = cfg.get("indoor_temp_entity", "")
        setpoint_entity = cfg.get("indoor_setpoint_entity", "")

        if setpoint_entity:
            setpoint = await self.ha.get_float(setpoint_entity)
        else:
            setpoint = float(cfg.get("indoor_target_temp", 21.0))

        actual = await self.ha.get_float(indoor_entity)

        if actual is None:
            self.logger.warning("Indoor temp unavailable")
            return
        if setpoint is None:
            self.logger.warning("Indoor setpoint unavailable")
            return

        # NibePi: ignore sensor readings < 4°C (fault/disconnected)
        if actual < 4:
            self.logger.warning(f"Indoor temp {actual}°C looks like a sensor fault, skipping")
            return

        factor = float(cfg.get("indoor_factor", 10.0))
        offset = calc_indoor_offset(setpoint, actual, factor)

        self.state["indoor_offset"] = offset
        self.state["last_indoor_temp"] = actual
        self.state["last_indoor_setpoint"] = setpoint
        self._live["indoor_temp"] = actual
        self._live["indoor_setpoint"] = setpoint
        self.logger.info(f"Indoor: actual={actual}°C setpoint={setpoint}°C factor={factor} → offset={offset:+.2f}°C")

    # ---- price ----
    async def _run_price(self):
        cfg = self.cfg
        if not cfg.get("price_enabled", True):
            self.state["price_offset"] = 0.0
            return

        price = await self.ha.get_float(cfg.get("electricity_price_entity", ""))
        if price is None:
            self.logger.warning("Electricity price unavailable")
            return

        level  = classify_price(price, cfg)
        offset = price_to_offset(level, cfg)

        self.state["price_offset"] = offset
        self.state["last_price_level"] = level
        self.state["last_price"] = price
        self._live["price"] = price
        self._live["price_level"] = level
        self.logger.info(f"Price: {price:.4f} → {level} → offset={offset:+.1f}°C")

    # ---- apply ----
    async def _apply(self):
        cfg = self.cfg
        s = self.state

        weather_offset = s.get("weather_offset", 0.0) or 0.0
        indoor_offset  = s.get("indoor_offset",  0.0) or 0.0
        price_offset   = s.get("price_offset",   0.0) or 0.0

        combined = max(-10.0, min(10.0, round(weather_offset + indoor_offset + price_offset, 1)))

        # Rate limiting: don't write more than every N minutes
        min_interval = float(cfg.get("min_write_interval_min", MIN_WRITE_INTERVAL_MIN)) * 60
        last_write = s.get("last_write_ts", 0) or 0
        elapsed = time.time() - last_write

        # Hysteresis: skip if change < 0.5°C AND not enough time
        last_combined = s.get("last_combined_offset")
        delta = abs(combined - last_combined) if last_combined is not None else 999

        if delta < 0.5 and elapsed < min_interval:
            self.logger.debug(f"Skipping apply: delta={delta:.2f}°C elapsed={elapsed/60:.1f}min")
            return

        if delta < 0.2:
            self.logger.debug(f"Skipping apply: delta={delta:.2f}°C (too small)")
            return

        # Build human-readable reasons
        reasons = self._build_reasons(weather_offset, indoor_offset, price_offset, cfg, s)

        offset_entity = cfg.get("curve_offset_entity", "")
        self.logger.info(f"Applying offset {combined:+.1f}°C to {offset_entity}  | {' | '.join(reasons)}")

        success = await self.ha.set_number(offset_entity, combined)
        if success:
            s["last_combined_offset"] = combined
            s["last_write_ts"] = int(time.time())
            save_state(s)

            entry = make_history_entry(combined, weather_offset, indoor_offset, price_offset, s, reasons)
            self.history.append(entry)
            save_history(self.history)

            self._live["last_write_ts"] = s["last_write_ts"]
            self._live["current_offset"] = combined

            await self.ha.fire_event("nibe_smart_control_offset_applied", {
                "combined_offset": combined,
                "weather_offset":  weather_offset,
                "indoor_offset":   indoor_offset,
                "price_offset":    price_offset,
                "reasons": reasons,
            })

    def _build_reasons(self, weather: float, indoor: float, price: float, cfg: dict, s: dict) -> List[str]:
        reasons = []
        if weather != 0 and cfg.get("weather_enabled"):
            f = s.get("last_forecast_temp")
            o = s.get("last_outdoor_temp")
            if f is not None and o is not None:
                direction = "colder" if f < o else "warmer"
                diff = round(abs(o - f), 1)
                reasons.append(f"Forecast {diff}°C {direction} in {cfg.get('forecast_hours',6)}h → {weather:+.2f}°C")
            else:
                reasons.append(f"Weather forecast → {weather:+.2f}°C")
        if indoor != 0 and cfg.get("indoor_enabled"):
            act = s.get("last_indoor_temp")
            sp  = s.get("last_indoor_setpoint")
            if act is not None and sp is not None:
                diff = round(sp - act, 1)
                direction = "below" if diff > 0 else "above"
                reasons.append(f"Indoor {act}°C is {abs(diff)}°C {direction} {sp}°C setpoint → {indoor:+.2f}°C")
            else:
                reasons.append(f"Indoor temp deviation → {indoor:+.2f}°C")
        if price != 0 and cfg.get("price_enabled"):
            level = s.get("last_price_level", "")
            p = s.get("last_price")
            pstr = f"{p:.4f}" if p is not None else "?"
            reasons.append(f"Electricity {level} ({pstr}) → {price:+.1f}°C")
        if not reasons:
            reasons.append("Offset unchanged (all factors at zero)")
        return reasons

    # ---- public accessors for web UI ----
    def get_status(self) -> dict:
        s = self.state
        return {
            "weather_offset":  round(s.get("weather_offset",  0.0) or 0.0, 2),
            "indoor_offset":   round(s.get("indoor_offset",   0.0) or 0.0, 2),
            "price_offset":    round(s.get("price_offset",    0.0) or 0.0, 2),
            "combined_offset": round(s.get("last_combined_offset") or 0.0, 2),
            "last_write_ts":   s.get("last_write_ts", 0),
            "price_level":     s.get("last_price_level", "UNKNOWN"),
            "outdoor_temp":    s.get("last_outdoor_temp"),
            "indoor_temp":     s.get("last_indoor_temp"),
            "indoor_setpoint": s.get("last_indoor_setpoint"),
            "forecast_temp":   s.get("last_forecast_temp"),
            "price":           s.get("last_price"),
            **self._live,
        }

    def get_history(self, n: int = MAX_HISTORY_MEM) -> List[dict]:
        return self.history[-n:]

# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------
class WebServer:

    def __init__(self, controller: NibeController, logger: logging.Logger):
        self.ctrl = controller
        self.logger = logger

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/",            self._index)
        app.router.add_get("/api/status",  self._api_status)
        app.router.add_get("/api/history", self._api_history)
        app.router.add_get("/api/config",  self._api_config_get)
        app.router.add_post("/api/config", self._api_config_post)
        app.router.add_get("/api/entities",self._api_entities)
        return app

    async def _index(self, request: web.Request) -> web.Response:
        html = self._render_html()
        return web.Response(text=html, content_type="text/html")

    async def _api_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.ctrl.get_status())

    async def _api_history(self, request: web.Request) -> web.Response:
        n = int(request.rel_url.query.get("n", MAX_HISTORY_MEM))
        return web.json_response(self.ctrl.get_history(n))

    async def _api_config_get(self, request: web.Request) -> web.Response:
        return web.json_response(self.ctrl.cfg)

    async def _api_config_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            # Validate types for numeric fields
            numeric = ["forecast_hours","weather_adjust_factor","indoor_target_temp",
                       "indoor_factor","price_very_cheap","price_cheap","price_normal",
                       "price_expensive","price_very_expensive",
                       "price_very_cheap_threshold","price_cheap_threshold",
                       "price_expensive_threshold","price_very_expensive_threshold",
                       "min_write_interval_min"]
            for k in numeric:
                if k in body:
                    body[k] = float(body[k])
            bools = ["weather_enabled","weather_enable_up","weather_enable_down",
                     "indoor_enabled","price_enabled"]
            for k in bools:
                if k in body:
                    body[k] = bool(body[k])
            self.ctrl.cfg.update(body)
            save_config(self.ctrl.cfg)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _api_entities(self, request: web.Request) -> web.Response:
        domain = request.rel_url.query.get("domain", "")
        entities = await self.ctrl.ha.list_entities(domain)
        return web.json_response(entities)

    def _render_html(self) -> str:
        return FRONTEND_HTML

    async def start(self, port: int = 8099):
        app = self.build_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        self.logger.info(f"Web UI available at http://0.0.0.0:{port}")

# ---------------------------------------------------------------------------
# Frontend HTML (single-file SPA)
# ---------------------------------------------------------------------------
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nibe Smart Control</title>
<style>
  /* ─── Design tokens ─────────────────────────────────────── */
  :root {
    --bg:       #0f1117;
    --surface:  #181c27;
    --surface2: #1f2436;
    --border:   #2a3050;
    --text:     #e4e9f7;
    --muted:    #7b87a8;
    --accent:   #e05c2a;        /* thermal orange */
    --cold:     #3a82f7;        /* forecast cold */
    --warm:     #f7953a;        /* forecast warm */
    --green:    #2ec27e;
    --yellow:   #f6d24a;
    --red:      #e05c2a;
    --font-mono: "JetBrains Mono", "Fira Mono", ui-monospace, monospace;
    --font-body: "Inter", "Segoe UI", system-ui, sans-serif;
    --radius:   8px;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    font-size: 14px;
    line-height: 1.5;
    min-height: 100vh;
  }

  /* ─── Layout ────────────────────────────────────────────── */
  header {
    padding: 18px 24px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 14px;
    background: var(--surface);
  }
  header svg { flex-shrink: 0; }
  header h1  { font-size: 17px; font-weight: 600; letter-spacing: .02em; }
  header span.sub { font-size: 12px; color: var(--muted); margin-left: auto; }

  nav {
    display: flex;
    gap: 2px;
    padding: 12px 24px 0;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  nav button {
    background: none;
    border: none;
    color: var(--muted);
    padding: 8px 16px;
    cursor: pointer;
    font: inherit;
    font-size: 13px;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    transition: color .15s, border-color .15s;
  }
  nav button.active { color: var(--text); border-bottom-color: var(--accent); }
  nav button:hover:not(.active) { color: var(--text); }

  main { padding: 24px; max-width: 1100px; }
  .tab { display: none; }
  .tab.active { display: block; }

  /* ─── Cards ─────────────────────────────────────────────── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    margin-bottom: 16px;
  }
  .card h2 { font-size: 13px; font-weight: 600; text-transform: uppercase;
             letter-spacing: .08em; color: var(--muted); margin-bottom: 16px; }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .grid-3 { display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; }
  @media(max-width:700px){ .grid-2,.grid-3 { grid-template-columns: 1fr; } }

  /* ─── Stat tiles ─────────────────────────────────────────── */
  .stat { display: flex; flex-direction: column; gap: 4px; }
  .stat .label { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); }
  .stat .value { font-family: var(--font-mono); font-size: 24px; font-weight: 700; color: var(--text); }
  .stat .value.pos { color: var(--warm); }
  .stat .value.neg { color: var(--cold); }
  .stat .value.zero { color: var(--muted); }
  .stat .note  { font-size: 11px; color: var(--muted); }

  /* ─── Offset decomposition bar — the signature element ───── */
  .decomp-wrap { margin: 20px 0 8px; }
  .decomp-label { font-size: 11px; color: var(--muted); margin-bottom: 6px;
                  display:flex; justify-content:space-between; }
  .decomp-bar {
    position: relative;
    height: 32px;
    background: var(--surface2);
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid var(--border);
  }
  .decomp-zero {
    position: absolute;
    left: 50%; top: 0; bottom: 0;
    width: 1px; background: var(--border);
    z-index: 2;
  }
  .decomp-seg {
    position: absolute;
    top: 4px; bottom: 4px;
    border-radius: 3px;
    transition: left .5s, width .5s;
  }
  .decomp-seg.weather { background: #3a82f7aa; border: 1px solid #3a82f7; }
  .decomp-seg.indoor  { background: #2ec27eaa; border: 1px solid #2ec27e; }
  .decomp-seg.price   { background: #e05c2aaa; border: 1px solid #e05c2a; }
  .decomp-legend { display: flex; gap: 16px; margin-top: 8px; }
  .decomp-legend span {
    font-size: 11px; color: var(--muted);
    display: flex; align-items: center; gap: 5px;
  }
  .decomp-legend .dot {
    width: 8px; height: 8px; border-radius: 2px; flex-shrink:0;
  }
  .dot.weather { background: #3a82f7; }
  .dot.indoor  { background: #2ec27e; }
  .dot.price   { background: #e05c2a; }

  /* ─── Price badge ────────────────────────────────────────── */
  .price-badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 600;
  }
  .price-badge.VERY_CHEAP     { background:#2ec27e22; color:#2ec27e; border:1px solid #2ec27e55; }
  .price-badge.CHEAP          { background:#a3e63522; color:#a3e635; border:1px solid #a3e63555; }
  .price-badge.NORMAL         { background:#7b87a822; color:#7b87a8; border:1px solid #7b87a844; }
  .price-badge.EXPENSIVE      { background:#f6d24a22; color:#f6d24a; border:1px solid #f6d24a55; }
  .price-badge.VERY_EXPENSIVE { background:#e05c2a22; color:#e05c2a; border:1px solid #e05c2a55; }
  .price-badge.UNKNOWN        { background:#2a305044; color:#7b87a8; border:1px solid #2a3050; }

  /* ─── Chart canvas ───────────────────────────────────────── */
  canvas { width:100% !important; }

  /* ─── History table ──────────────────────────────────────── */
  .history-list { display: flex; flex-direction: column; gap: 0; }
  .history-item {
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
    display: grid;
    grid-template-columns: 120px 80px 1fr;
    gap: 12px;
    align-items: start;
  }
  .history-item:last-child { border-bottom: none; }
  .history-ts { font-family: var(--font-mono); font-size: 11px; color: var(--muted); }
  .history-offset { font-family: var(--font-mono); font-size: 15px; font-weight: 700; }
  .history-offset.pos { color: var(--warm); }
  .history-offset.neg { color: var(--cold); }
  .history-offset.zero { color: var(--muted); }
  .history-reasons { font-size: 12px; color: var(--muted); line-height: 1.6; }
  .history-reasons .tag {
    display: inline-block; margin-right: 6px;
    padding: 1px 6px; border-radius: 3px;
    font-size: 10px; font-weight: 600; letter-spacing:.04em;
    background: var(--surface2); color: var(--muted); border: 1px solid var(--border);
  }
  .tag.weather-tag { color: #3a82f7; border-color: #3a82f744; }
  .tag.indoor-tag  { color: #2ec27e; border-color: #2ec27e44; }
  .tag.price-tag   { color: #e05c2a; border-color: #e05c2a44; }

  /* ─── Config form ────────────────────────────────────────── */
  .form-section { margin-bottom: 24px; }
  .form-section h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
                     color: var(--accent); margin-bottom: 14px;
                     padding-bottom: 6px; border-bottom: 1px solid var(--border); }
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media(max-width:700px){ .form-grid { grid-template-columns: 1fr; } }
  .field { display: flex; flex-direction: column; gap: 5px; }
  .field label { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); }
  .field input, .field select {
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); padding: 8px 10px; border-radius: 6px;
    font: inherit; font-size: 13px; width: 100%;
    transition: border-color .15s;
  }
  .field input:focus, .field select:focus {
    outline: none; border-color: var(--accent);
  }
  .field .hint { font-size: 11px; color: var(--muted); }
  .toggle-row { display: flex; align-items: center; gap: 10px; }
  .toggle { position: relative; width: 36px; height: 20px; flex-shrink:0; }
  .toggle input { opacity:0; width:0; height:0; }
  .slider {
    position: absolute; inset:0; background: var(--border);
    border-radius: 20px; cursor: pointer; transition: .2s;
  }
  .slider:before {
    content:""; position:absolute; height:14px; width:14px;
    left:3px; bottom:3px; background:#fff; border-radius:50%; transition:.2s;
  }
  .toggle input:checked + .slider { background: var(--accent); }
  .toggle input:checked + .slider:before { transform: translateX(16px); }
  .save-btn {
    background: var(--accent); color: #fff; border: none; border-radius: 6px;
    padding: 10px 28px; font: inherit; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: opacity .15s;
  }
  .save-btn:hover { opacity: .85; }
  .save-msg { font-size: 12px; color: var(--green); margin-left: 12px; opacity:0; transition: opacity .3s; }
  .save-msg.show { opacity:1; }

  /* ─── Entity picker ──────────────────────────────────────── */
  .entity-select-wrap { position: relative; }
  .entity-select-wrap datalist option { background: var(--surface2); color: var(--text); }

  /* ─── Tooltip ────────────────────────────────────────────── */
  .tooltip-wrap { position: relative; display: inline-block; }
  .tooltip-icon { color: var(--muted); cursor: help; font-size: 12px; }
  .tooltip-text {
    display: none; position: absolute; bottom: 120%; left: 50%;
    transform: translateX(-50%); white-space: nowrap;
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); font-size: 11px; padding: 5px 9px; border-radius: 4px; z-index:10;
  }
  .tooltip-wrap:hover .tooltip-text { display: block; }

  /* ─── Status dot ─────────────────────────────────────────── */
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%; display: inline-block;
    background: var(--green); box-shadow: 0 0 6px var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%,100% { opacity:1; } 50% { opacity:.4; }
  }
</style>
</head>
<body>

<header>
  <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
    <circle cx="14" cy="14" r="13" stroke="#e05c2a" stroke-width="1.5"/>
    <path d="M14 22 C14 22 8 17 8 12 A6 6 0 0 1 20 12 C20 17 14 22 14 22Z"
          fill="#e05c2a44" stroke="#e05c2a" stroke-width="1.2"/>
    <circle cx="14" cy="12" r="2.5" fill="#e05c2a"/>
    <line x1="14" y1="6" x2="14" y2="8.5" stroke="#e05c2a" stroke-width="1.5" stroke-linecap="round"/>
  </svg>
  <h1>Nibe Smart Control</h1>
  <span class="sub"><span class="status-dot" id="statusDot"></span>&nbsp;Live</span>
</header>

<nav>
  <button class="active" onclick="showTab('dashboard',this)">Dashboard</button>
  <button onclick="showTab('history',this)">History</button>
  <button onclick="showTab('charts',this)">Charts</button>
  <button onclick="showTab('settings',this)">Settings</button>
</nav>

<main>

<!-- ═══ DASHBOARD ════════════════════════════════════════════════ -->
<div id="tab-dashboard" class="tab active">

  <div class="grid-3">
    <div class="card">
      <h2>Current Offset</h2>
      <div class="stat">
        <div class="label">Combined heat curve offset</div>
        <div class="value" id="d-combined">—</div>
        <div class="note">Written to pump</div>
      </div>
    </div>
    <div class="card">
      <h2>Outdoor</h2>
      <div class="stat">
        <div class="label">Current</div>
        <div class="value" id="d-outdoor">—</div>
        <div class="note">Forecast <span id="d-forecast">—</span></div>
      </div>
    </div>
    <div class="card">
      <h2>Electricity</h2>
      <div class="stat">
        <div class="label">Current price</div>
        <div class="value" id="d-price">—</div>
        <div class="note" id="d-price-badge"></div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Offset Decomposition</h2>
    <div class="decomp-wrap">
      <div class="decomp-label">
        <span>−10°C</span><span>0</span><span>+10°C</span>
      </div>
      <div class="decomp-bar" id="decompBar">
        <div class="decomp-zero"></div>
        <div class="decomp-seg weather" id="segWeather"></div>
        <div class="decomp-seg indoor"  id="segIndoor"></div>
        <div class="decomp-seg price"   id="segPrice"></div>
      </div>
      <div class="decomp-legend">
        <span><div class="dot weather"></div>Weather <b id="l-weather">—</b></span>
        <span><div class="dot indoor"></div>Indoor <b id="l-indoor">—</b></span>
        <span><div class="dot price"></div>Price <b id="l-price">—</b></span>
      </div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>Indoor</h2>
      <div class="stat">
        <div class="label">Actual temperature</div>
        <div class="value" id="d-indoor">—</div>
        <div class="note">Setpoint <span id="d-indoor-set">—</span></div>
      </div>
    </div>
    <div class="card">
      <h2>Last Write</h2>
      <div class="stat">
        <div class="label">Last applied</div>
        <div class="value" id="d-last-write" style="font-size:16px;">—</div>
        <div class="note" id="d-next-write"></div>
      </div>
    </div>
  </div>

</div>

<!-- ═══ HISTORY ══════════════════════════════════════════════════ -->
<div id="tab-history" class="tab">
  <div class="card">
    <h2>Change Log</h2>
    <div class="history-list" id="historyList">
      <div style="color:var(--muted);font-size:13px;">Loading…</div>
    </div>
  </div>
</div>

<!-- ═══ CHARTS ════════════════════════════════════════════════════ -->
<div id="tab-charts" class="tab">
  <div class="card">
    <h2>Offset Over Time</h2>
    <canvas id="chartOffset" height="220"></canvas>
  </div>
  <div class="card">
    <h2>Temperature Context</h2>
    <canvas id="chartTemps" height="220"></canvas>
  </div>
  <div class="card">
    <h2>Price Level</h2>
    <canvas id="chartPrice" height="180"></canvas>
  </div>
</div>

<!-- ═══ SETTINGS ══════════════════════════════════════════════════ -->
<div id="tab-settings" class="tab">
  <div class="card">
    <h2>Entity Configuration</h2>
    <form id="configForm">

      <div class="form-section">
        <h3>Heat Pump Entities (NibeGW / ESPHome)</h3>
        <div class="form-grid">
          <div class="field">
            <label>Outdoor temperature sensor</label>
            <input type="text" name="outdoor_temp_entity" list="dl-sensor" placeholder="sensor.nibe_outdoor_temperature">
          </div>
          <div class="field">
            <label>Heat curve (read-only)</label>
            <input type="text" name="heat_curve_entity" list="dl-number" placeholder="number.nibe_heat_curve_s1">
          </div>
          <div class="field">
            <label>Curve offset (addon writes here)</label>
            <input type="text" name="curve_offset_entity" list="dl-number" placeholder="number.nibe_heat_offset_s1">
          </div>
        </div>
      </div>

      <div class="form-section">
        <h3>Weather Forecast</h3>
        <div class="form-grid">
          <div class="field">
            <label>Weather entity</label>
            <input type="text" name="weather_entity" list="dl-weather" placeholder="weather.forecast_home">
          </div>
          <div class="field">
            <label>Forecast lookahead (hours)</label>
            <input type="number" name="forecast_hours" min="1" max="24" step="1">
          </div>
          <div class="field">
            <label>Static bias offset (°C)</label>
            <input type="number" name="weather_adjust_factor" min="-2" max="2" step="0.5">
            <span class="hint">Adds a fixed ±°C to weather offset — useful if your pump tends to overshoot</span>
          </div>
        </div>
        <div style="display:flex;gap:24px;margin-top:12px;flex-wrap:wrap;">
          <div class="field">
            <div class="toggle-row">
              <label class="toggle"><input type="checkbox" name="weather_enabled"><span class="slider"></span></label>
              <span>Enable weather control</span>
            </div>
          </div>
          <div class="field">
            <div class="toggle-row">
              <label class="toggle"><input type="checkbox" name="weather_enable_up"><span class="slider"></span></label>
              <span>Allow raising offset</span>
            </div>
          </div>
          <div class="field">
            <div class="toggle-row">
              <label class="toggle"><input type="checkbox" name="weather_enable_down"><span class="slider"></span></label>
              <span>Allow lowering offset</span>
            </div>
          </div>
        </div>
      </div>

      <div class="form-section">
        <h3>Indoor Temperature Control</h3>
        <div class="form-grid">
          <div class="field">
            <label>Indoor temperature sensor</label>
            <input type="text" name="indoor_temp_entity" list="dl-sensor" placeholder="sensor.living_room_temperature">
          </div>
          <div class="field">
            <label>Setpoint entity (optional)</label>
            <input type="text" name="indoor_setpoint_entity" list="dl-sensor" placeholder="Leave blank to use Target Temp below">
          </div>
          <div class="field">
            <label>Target indoor temperature (°C)</label>
            <input type="number" name="indoor_target_temp" min="10" max="28" step="0.5">
            <span class="hint">Used only when no setpoint entity is specified</span>
          </div>
          <div class="field">
            <label>P-factor
              <span class="tooltip-wrap">
                <span class="tooltip-icon">ⓘ</span>
                <span class="tooltip-text">offset = (setpoint − actual) × factor — NibePi default: 10</span>
              </span>
            </label>
            <input type="number" name="indoor_factor" min="1" max="50" step="1">
          </div>
        </div>
        <div style="margin-top:12px;">
          <div class="field">
            <div class="toggle-row">
              <label class="toggle"><input type="checkbox" name="indoor_enabled"><span class="slider"></span></label>
              <span>Enable indoor temperature control</span>
            </div>
          </div>
        </div>
      </div>

      <div class="form-section">
        <h3>Electricity Price Control</h3>
        <div class="form-grid">
          <div class="field">
            <label>Price sensor</label>
            <input type="text" name="electricity_price_entity" list="dl-sensor" placeholder="sensor.nordpool_kwh_lt_eur_3_10_025">
          </div>
        </div>
        <div style="margin:12px 0 16px;">
          <div class="field">
            <div class="toggle-row">
              <label class="toggle"><input type="checkbox" name="price_enabled"><span class="slider"></span></label>
              <span>Enable price control</span>
            </div>
          </div>
        </div>
        <p style="font-size:11px;color:var(--muted);margin-bottom:10px;text-transform:uppercase;letter-spacing:.06em;">Curve offsets per price level (°C)</p>
        <div class="form-grid">
          <div class="field"><label>Very Cheap offset</label><input type="number" name="price_very_cheap" min="-5" max="5" step="0.5"></div>
          <div class="field"><label>Cheap offset</label><input type="number" name="price_cheap" min="-5" max="5" step="0.5"></div>
          <div class="field"><label>Normal offset</label><input type="number" name="price_normal" min="-5" max="5" step="0.5"></div>
          <div class="field"><label>Expensive offset</label><input type="number" name="price_expensive" min="-5" max="5" step="0.5"></div>
          <div class="field"><label>Very Expensive offset</label><input type="number" name="price_very_expensive" min="-5" max="5" step="0.5"></div>
        </div>
        <p style="font-size:11px;color:var(--muted);margin:16px 0 10px;text-transform:uppercase;letter-spacing:.06em;">Price thresholds (same unit as your sensor, e.g. EUR/kWh)</p>
        <div class="form-grid">
          <div class="field"><label>Very Cheap ≤</label><input type="number" name="price_very_cheap_threshold" min="0" step="0.01"></div>
          <div class="field"><label>Cheap ≤</label><input type="number" name="price_cheap_threshold" min="0" step="0.01"></div>
          <div class="field"><label>Expensive ≥</label><input type="number" name="price_expensive_threshold" min="0" step="0.01"></div>
          <div class="field"><label>Very Expensive ≥</label><input type="number" name="price_very_expensive_threshold" min="0" step="0.01"></div>
        </div>
      </div>

      <div class="form-section">
        <h3>Rate Limiting</h3>
        <div class="form-grid">
          <div class="field">
            <label>Min. minutes between writes</label>
            <input type="number" name="min_write_interval_min" min="5" max="120" step="5">
            <span class="hint">Protects the heat pump from too-frequent adjustments (min 5)</span>
          </div>
        </div>
      </div>

      <div style="display:flex;align-items:center;margin-top:8px;">
        <button type="submit" class="save-btn">Save Configuration</button>
        <span class="save-msg" id="saveMsg">Saved ✓</span>
      </div>

    </form>
  </div>
</div>

</main>

<!-- datalists for entity pickers -->
<datalist id="dl-sensor"></datalist>
<datalist id="dl-number"></datalist>
<datalist id="dl-weather"></datalist>

<script>
// ── Chart.js CDN ───────────────────────────────────────────────────────
const CHART_CDN = 'https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js';
let Chart = null;
async function ensureChart() {
  if (Chart) return;
  await new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = CHART_CDN; s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
  Chart = window.Chart;
  Chart.defaults.color = '#7b87a8';
  Chart.defaults.borderColor = '#2a3050';
  Chart.defaults.font.family = '"Inter","Segoe UI",system-ui,sans-serif';
}

// ── Tabs ───────────────────────────────────────────────────────────────
function showTab(id, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  btn.classList.add('active');
  if (id === 'history') loadHistory();
  if (id === 'charts')  loadCharts();
}

// ── Helpers ────────────────────────────────────────────────────────────
function fmt(v, unit='°C') {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  const s = (n > 0 ? '+' : '') + n.toFixed(1) + unit;
  return s;
}
function fmtTs(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('en-GB', {
    month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'
  });
}
function colorClass(v) {
  const n = Number(v);
  if (n > 0.05)  return 'pos';
  if (n < -0.05) return 'neg';
  return 'zero';
}

// ── Decomposition bar ──────────────────────────────────────────────────
// Maps a value in [-10, +10] to a [0,100]% position on the bar
// The bar centre (50%) = 0°C
function offsetToPct(v) { return 50 + (v / 10) * 50; }
// Given start-position and width (both in %), clamp to [0,100]
function clampSeg(left, width) {
  const right = left + width;
  const cl = Math.max(0, left);
  const cr = Math.min(100, right);
  return { left: cl, width: Math.max(0, cr - cl) };
}
function renderDecompBar(weather, indoor, price) {
  // Segments are placed as stacked contributions from zero.
  // weather is rendered from centre, indoor continues from weather, price from indoor.
  const total = (weather || 0) + (indoor || 0) + (price || 0);
  const clamped = Math.max(-10, Math.min(10, total));

  // Compute individual segment positions along the bar
  // They share the same "stack" visually but we show them as overlapping layers
  // for simplicity — colour-coded at different vertical positions.
  // We'll instead show them as 3 stacked mini-rows within the bar using height slots.
  const segs = [
    { id: 'segWeather', val: weather || 0, cls: 'weather' },
    { id: 'segIndoor',  val: indoor  || 0, cls: 'indoor'  },
    { id: 'segPrice',   val: price   || 0, cls: 'price'   },
  ];
  const barH = 32;
  const rowH = Math.floor((barH - 8) / segs.length); // px each row

  segs.forEach((seg, i) => {
    const el = document.getElementById(seg.id);
    if (!el) return;
    const pct = offsetToPct(seg.val);
    const left  = Math.min(50, pct);
    const right = Math.max(50, pct);
    const w = right - left;
    const top = 4 + i * rowH;
    el.style.left   = left + '%';
    el.style.width  = w + '%';
    el.style.top    = top + 'px';
    el.style.height = (rowH - 2) + 'px';
    el.style.bottom = 'auto';
    el.title = `${seg.cls}: ${fmt(seg.val)}`;
  });
}

// ── Dashboard update ───────────────────────────────────────────────────
async function updateDashboard() {
  let data;
  try {
    const r = await fetch('/api/status');
    data = await r.json();
  } catch(e) {
    document.getElementById('statusDot').style.background = '#e05c2a';
    return;
  }
  document.getElementById('statusDot').style.background = '#2ec27e';

  const combined = data.combined_offset ?? 0;
  const el = document.getElementById('d-combined');
  el.textContent = fmt(combined);
  el.className = 'value ' + colorClass(combined);

  const ot = document.getElementById('d-outdoor');
  ot.textContent = data.outdoor_temp != null ? data.outdoor_temp.toFixed(1) + '°C' : '—';
  document.getElementById('d-forecast').textContent =
    data.forecast_temp != null ? `→ ${data.forecast_temp.toFixed(1)}°C` : '';

  const pr = document.getElementById('d-price');
  pr.textContent = data.price != null ? data.price.toFixed(4) : '—';
  const lvl = data.price_level || 'UNKNOWN';
  document.getElementById('d-price-badge').innerHTML =
    `<span class="price-badge ${lvl}">${lvl.replace('_',' ')}</span>`;

  const ind = document.getElementById('d-indoor');
  ind.textContent = data.indoor_temp != null ? data.indoor_temp.toFixed(1) + '°C' : '—';
  document.getElementById('d-indoor-set').textContent =
    data.indoor_setpoint != null ? `→ ${data.indoor_setpoint.toFixed(1)}°C` : '';

  // Last write
  document.getElementById('d-last-write').textContent = fmtTs(data.last_write_ts);
  const minWrite = (window._cfg && window._cfg.min_write_interval_min) || 10;
  if (data.last_write_ts) {
    const elapsed = Date.now()/1000 - data.last_write_ts;
    const rem = Math.max(0, minWrite * 60 - elapsed);
    document.getElementById('d-next-write').textContent =
      rem > 0 ? `Next write in ${Math.ceil(rem/60)} min` : 'Ready to write';
  }

  // Decomp bar
  document.getElementById('l-weather').textContent = fmt(data.weather_offset);
  document.getElementById('l-indoor').textContent  = fmt(data.indoor_offset);
  document.getElementById('l-price').textContent   = fmt(data.price_offset);
  renderDecompBar(data.weather_offset, data.indoor_offset, data.price_offset);
}

// ── History ────────────────────────────────────────────────────────────
async function loadHistory() {
  const list = document.getElementById('historyList');
  list.innerHTML = '<div style="color:var(--muted);font-size:13px;">Loading…</div>';
  let entries;
  try {
    const r = await fetch('/api/history');
    entries = await r.json();
  } catch(e) { list.innerHTML='<div style="color:var(--red)">Failed to load history</div>'; return; }

  if (!entries.length) {
    list.innerHTML='<div style="color:var(--muted);font-size:13px;">No changes recorded yet.</div>';
    return;
  }

  list.innerHTML = '';
  [...entries].reverse().forEach(e => {
    const div = document.createElement('div');
    div.className = 'history-item';

    const reasonsHtml = (e.reasons || []).map(r => {
      let cls = '';
      if (r.toLowerCase().includes('forecast') || r.toLowerCase().includes('weather')) cls = 'weather-tag';
      else if (r.toLowerCase().includes('indoor')) cls = 'indoor-tag';
      else if (r.toLowerCase().includes('price') || r.toLowerCase().includes('cheap') || r.toLowerCase().includes('expensive')) cls = 'price-tag';
      return `<span class="tag ${cls}">${tagIcon(cls)}</span>${r}`;
    }).join('<br>');

    div.innerHTML = `
      <div class="history-ts">${fmtTs(e.ts)}</div>
      <div class="history-offset ${colorClass(e.combined)}">${fmt(e.combined)}</div>
      <div class="history-reasons">${reasonsHtml}</div>
    `;
    list.appendChild(div);
  });
}
function tagIcon(cls) {
  if (cls === 'weather-tag') return '⛅ ';
  if (cls === 'indoor-tag')  return '🌡 ';
  if (cls === 'price-tag')   return '⚡ ';
  return '';
}

// ── Charts ─────────────────────────────────────────────────────────────
let charts = {};
async function loadCharts() {
  await ensureChart();
  let history;
  try {
    const r = await fetch('/api/history?n=200');
    history = await r.json();
  } catch(e) { return; }
  if (!history.length) return;

  const labels = history.map(e => fmtTs(e.ts));

  // Chart 1: offsets
  const ctx1 = document.getElementById('chartOffset').getContext('2d');
  if (charts.offset) charts.offset.destroy();
  charts.offset = new Chart(ctx1, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label:'Combined', data: history.map(e=>e.combined), borderColor:'#e05c2a', backgroundColor:'#e05c2a22', fill:true, tension:.3, pointRadius:2 },
        { label:'Weather',  data: history.map(e=>e.weather),  borderColor:'#3a82f7', backgroundColor:'transparent', tension:.3, pointRadius:2 },
        { label:'Indoor',   data: history.map(e=>e.indoor),   borderColor:'#2ec27e', backgroundColor:'transparent', tension:.3, pointRadius:2 },
        { label:'Price',    data: history.map(e=>e.price),    borderColor:'#f6d24a', backgroundColor:'transparent', tension:.3, pointRadius:2 },
      ]
    },
    options: chartOpts({ title: 'Curve Offset (°C)', yMin:-5, yMax:5 })
  });

  // Chart 2: temps
  const ctx2 = document.getElementById('chartTemps').getContext('2d');
  if (charts.temps) charts.temps.destroy();
  charts.temps = new Chart(ctx2, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label:'Outdoor',  data: history.map(e=>e.outdoor_temp), borderColor:'#7b87a8', tension:.3, pointRadius:2 },
        { label:'Forecast', data: history.map(e=>e.forecast_temp),borderColor:'#3a82f7', borderDash:[4,3], tension:.3, pointRadius:2 },
        { label:'Indoor',   data: history.map(e=>e.indoor_temp),  borderColor:'#2ec27e', tension:.3, pointRadius:2 },
        { label:'Setpoint', data: history.map(e=>e.indoor_setpoint),borderColor:'#2ec27e66', borderDash:[2,3], tension:.3, pointRadius:0 },
      ]
    },
    options: chartOpts({ title: 'Temperatures (°C)' })
  });

  // Chart 3: price numeric
  const ctx3 = document.getElementById('chartPrice').getContext('2d');
  if (charts.price) charts.price.destroy();
  charts.price = new Chart(ctx3, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label:'Price',
        data: history.map(e=>e.price),
        backgroundColor: history.map(e => {
          const lv = e.price_level;
          if (lv==='VERY_CHEAP')     return '#2ec27e88';
          if (lv==='CHEAP')          return '#a3e63588';
          if (lv==='NORMAL')         return '#7b87a888';
          if (lv==='EXPENSIVE')      return '#f6d24a88';
          if (lv==='VERY_EXPENSIVE') return '#e05c2a88';
          return '#7b87a844';
        }),
        borderWidth: 0,
      }]
    },
    options: chartOpts({ title: 'Electricity Price', yMin: 0 })
  });
}

function chartOpts({ title='', yMin=undefined, yMax=undefined } = {}) {
  return {
    responsive: true,
    animation: { duration: 300 },
    plugins: {
      legend: { labels: { usePointStyle:true, boxWidth:8, padding:16 } },
      title: { display: false },
      tooltip: { mode:'index', intersect:false }
    },
    scales: {
      x: {
        ticks: { maxTicksLimit:8, maxRotation:0 },
        grid: { color:'#2a3050' }
      },
      y: {
        min: yMin, max: yMax,
        grid: { color:'#2a3050' }
      }
    }
  };
}

// ── Settings ───────────────────────────────────────────────────────────
async function loadConfig() {
  const r = await fetch('/api/config');
  const cfg = await r.json();
  window._cfg = cfg;
  const form = document.getElementById('configForm');
  Object.entries(cfg).forEach(([k, v]) => {
    const el = form.elements[k];
    if (!el) return;
    if (el.type === 'checkbox') el.checked = !!v;
    else el.value = v;
  });
}

document.getElementById('configForm').addEventListener('submit', async e => {
  e.preventDefault();
  const form = e.target;
  const data = {};
  // text + number fields
  form.querySelectorAll('input:not([type=checkbox]), select').forEach(el => {
    if (el.name) data[el.name] = el.type==='number' ? Number(el.value) : el.value;
  });
  // checkboxes
  form.querySelectorAll('input[type=checkbox]').forEach(el => {
    if (el.name) data[el.name] = el.checked;
  });
  const r = await fetch('/api/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) });
  const res = await r.json();
  const msg = document.getElementById('saveMsg');
  msg.textContent = res.ok ? 'Saved ✓' : ('Error: ' + res.error);
  msg.style.color = res.ok ? 'var(--green)' : 'var(--red)';
  msg.classList.add('show');
  setTimeout(() => msg.classList.remove('show'), 3000);
  if (res.ok) window._cfg = data;
});

// ── Entity autocomplete ────────────────────────────────────────────────
async function populateEntityLists() {
  try {
    const domains = [
      {domain:'sensor', listId:'dl-sensor'},
      {domain:'number', listId:'dl-number'},
      {domain:'weather', listId:'dl-weather'},
    ];
    await Promise.all(domains.map(async ({domain, listId}) => {
      const r = await fetch(`/api/entities?domain=${domain}`);
      const entities = await r.json();
      const dl = document.getElementById(listId);
      dl.innerHTML = '';
      entities.forEach(e => {
        const opt = document.createElement('option');
        opt.value = e.entity_id;
        opt.label = `${e.friendly_name} (${e.entity_id})${e.unit?' ['+e.unit+']':''}`;
        dl.appendChild(opt);
      });
    }));
  } catch(e) { /* fail silently */ }
}

// ── Init ───────────────────────────────────────────────────────────────
(async function init() {
  await updateDashboard();
  await loadConfig();
  populateEntityLists();
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
    cfg = load_config()
    logger = setup_logging(cfg.get("log_level", "info"))

    logger.info("=" * 60)
    logger.info("Nibe Smart Control v1.1 starting")
    logger.info(f"  Weather:  {'on' if cfg.get('weather_enabled') else 'off'}  ({cfg.get('weather_entity','')})")
    logger.info(f"  Indoor:   {'on' if cfg.get('indoor_enabled') else 'off'}  ({cfg.get('indoor_temp_entity','')})")
    logger.info(f"  Price:    {'on' if cfg.get('price_enabled') else 'off'}  ({cfg.get('electricity_price_entity','')})")
    logger.info(f"  Offset→:  {cfg.get('curve_offset_entity','')}")
    logger.info(f"  Min write interval: {cfg.get('min_write_interval_min', MIN_WRITE_INTERVAL_MIN)} min")
    logger.info("=" * 60)

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        controller = NibeController(cfg, logger)
        web_server = WebServer(controller, logger)

        await web_server.start(port=8099)

        await controller.run(session)


if __name__ == "__main__":
    asyncio.run(main())
