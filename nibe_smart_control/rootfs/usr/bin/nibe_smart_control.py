#!/usr/bin/env python3
"""
Nibe Smart Control — Home Assistant Addon v1.3.0
React 18 UMD frontend, served via HA Ingress.
All config stored in /data/config.json (edited via web UI).
"""

import asyncio, json, logging, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Any

import aiohttp
from aiohttp import web

# ---------------------------------------------------------------------------
DATA_DIR     = Path("/data")
CONFIG_FILE  = DATA_DIR / "config.json"
STATE_FILE   = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.json"
MAX_HISTORY  = 500
MIN_WRITE_INTERVAL = 10

LOG_LEVEL_MAP = {"debug": logging.DEBUG, "info": logging.INFO,
                 "warning": logging.WARNING, "error": logging.ERROR}

DEFAULT_CONFIG = {
    "weather_entity": "", "electricity_price_entity": "sensor.electricity_price",
    "outdoor_temp_entity": "", "indoor_temp_entity": "",
    "indoor_setpoint_entity": "", "heat_curve_entity": "",
    "curve_offset_entity": "",
    "forecast_hours": 6,
    "weather_enabled": False, "weather_enable_up": True, "weather_enable_down": True,
    "weather_adjust_factor": 0.0,
    "indoor_enabled": False, "indoor_target_temp": 21.0, "indoor_factor": 10.0,
    "price_enabled": False,
    "price_very_cheap": 2.0, "price_cheap": 1.0, "price_normal": 0.0,
    "price_expensive": -1.0, "price_very_expensive": -2.0,
    "price_very_cheap_threshold": 0.15, "price_cheap_threshold": 0.22,
    "price_expensive_threshold": 0.32, "price_very_expensive_threshold": 0.38,
    "min_write_interval_min": MIN_WRITE_INTERVAL, "dry_run": True,
    "planning_enabled": True, "planning_lookahead_hours": 24,
    "price_preheat_hours": 2,
    "solar_enabled": False, "solar_entity": "", "solar_peak_kwh": 10.0,
    "solar_weight": 0.4, "battery_entity": "", "battery_weight": 0.3,
    "battery_useful_soc_min": 20.0, "indoor_gate_dead_band": 0.5,
    "max_step_per_write": 3.0,
    "prio_entity": "", "compressor_status_entity": "",
    "int_add_power_entity": "",
    "compressor_rated_kw": 1.7, "pump_overhead_kw": 0.12,
    "log_level": "info",
}

# ---------------------------------------------------------------------------
def setup_logging(level_str: str) -> logging.Logger:
    level = LOG_LEVEL_MAP.get(level_str.lower(), logging.INFO)
    logging.basicConfig(level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S", stream=sys.stdout)
    return logging.getLogger("nibe")

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
    return {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}

def load_state() -> dict:
    return load_json(STATE_FILE, {
        "weather_offset": 0.0, "indoor_offset": 0.0, "price_offset": 0.0,
        "last_combined_offset": None, "last_write_ts": 0,
        "last_price_level": "UNKNOWN", "last_forecast_temp": None,
        "last_outdoor_temp": None, "last_indoor_temp": None,
        "last_indoor_setpoint": None, "last_price": None,
    })

# ---------------------------------------------------------------------------
class HAClient:
    def __init__(self, session: aiohttp.ClientSession, logger: logging.Logger):
        self.session = session
        self.logger  = logger
        self.base    = "http://supervisor/core/api"
        token        = os.environ.get("SUPERVISOR_TOKEN", "")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def get_float(self, entity_id: str) -> Optional[float]:
        if not entity_id: return None
        url = f"{self.base}/states/{entity_id}"
        try:
            async with self.session.get(url, headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: return None
                s = await r.json()
                raw = s.get("state")
                if raw in (None, "unavailable", "unknown", ""): return None
                return float(raw)
        except Exception: return None

    async def get_state_raw(self, entity_id: str) -> Optional[str]:
        if not entity_id: return None
        url = f"{self.base}/states/{entity_id}"
        try:
            async with self.session.get(url, headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: return None
                s = await r.json()
                raw = s.get("state")
                if raw in (None, "unavailable", "unknown", ""): return None
                return str(raw)
        except Exception: return None

    async def set_number(self, entity_id: str, value: float) -> bool:
        url = f"{self.base}/services/number/set_value"
        try:
            async with self.session.post(url, headers=self.headers,
                    json={"entity_id": entity_id, "value": str(round(value, 1))},
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status in (200, 201)
        except Exception as e:
            self.logger.error(f"set_number({entity_id}): {e}"); return False

    async def get_weather_forecast(self, entity_id: str) -> Optional[list]:
        # Must use ?return_response=true so the supervisor passes back the
        # service response body (forecast data) instead of just changed states.
        url = f"{self.base}/services/weather/get_forecasts?return_response=true"
        try:
            async with self.session.post(url, headers=self.headers,
                    json={"entity_id": entity_id, "type": "hourly"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status in (200, 201):
                    data = await r.json()
                    # HA returns {"service_response": {"weather.x": {"forecast": [...]}}}
                    if isinstance(data, dict):
                        svc = data.get("service_response", data)
                        fc = svc.get(entity_id, {}).get("forecast")
                        if fc: return sorted(fc, key=lambda x: x.get("datetime", ""))
                    # Older shape: list of state-change dicts
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                fc = item.get("response", {}).get(entity_id, {}).get("forecast")
                                if fc: return sorted(fc, key=lambda x: x.get("datetime", ""))
                    self.logger.warning(f"get_forecasts returned unexpected shape: {str(data)[:200]}")
        except Exception as e:
            self.logger.warning(f"get_forecasts service call failed: {e}")
        # Fallback: attributes (older HA or custom integrations)
        try:
            async with self.session.get(f"{self.base}/states/{entity_id}", headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    s = await r.json()
                    fc = s.get("attributes", {}).get("forecast", [])
                    if fc:
                        self.logger.debug(f"get_forecasts: using attributes fallback for {entity_id}")
                        return sorted(fc, key=lambda x: x.get("datetime", ""))
        except Exception as e:
            self.logger.debug(f"get_forecasts attributes fallback failed: {e}")
        self.logger.warning(f"get_forecasts: no forecast data found for {entity_id}")
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
def classify_price(price: float, cfg: dict, all_prices: list = None) -> str:
    """
    Dynamic classification: if all_prices (the 48h price series) is provided,
    use percentile bands so thresholds self-adjust to the current market.
    Falls back to fixed thresholds when no series is available (reactive loop).
    Percentile bands: VERY_CHEAP ≤15%, CHEAP ≤40%, NORMAL ≤75%, EXPENSIVE ≤92%, else VERY_EXPENSIVE
    """
    if all_prices and len(all_prices) >= 4:
        sorted_p = sorted(all_prices)
        n = len(sorted_p)
        def pct(p): return sorted_p[min(int(p * n / 100), n-1)]
        if price <= pct(15):  return "VERY_CHEAP"
        if price <= pct(40):  return "CHEAP"
        if price <= pct(75):  return "NORMAL"
        if price <= pct(92):  return "EXPENSIVE"
        return "VERY_EXPENSIVE"
    # Fallback: fixed thresholds (used by reactive price loop)
    t = [cfg.get("price_very_cheap_threshold", 0.15), cfg.get("price_cheap_threshold", 0.22),
         cfg.get("price_expensive_threshold", 0.32),  cfg.get("price_very_expensive_threshold", 0.38)]
    if price <= t[0]: return "VERY_CHEAP"
    if price <= t[1]: return "CHEAP"
    if price <  t[2]: return "NORMAL"
    if price <  t[3]: return "EXPENSIVE"
    return "VERY_EXPENSIVE"

def price_to_offset(level: str, cfg: dict) -> float:
    return float({"VERY_CHEAP": cfg.get("price_very_cheap", 2.0),
                  "CHEAP": cfg.get("price_cheap", 1.0), "NORMAL": cfg.get("price_normal", 0.0),
                  "EXPENSIVE": cfg.get("price_expensive", -1.0),
                  "VERY_EXPENSIVE": cfg.get("price_very_expensive", -2.0)}.get(level, 0.0))

def calc_weather_offset(outdoor, forecast, curve, sun=0.0, up=True, down=True) -> float:
    if curve == 0: return 0.0
    raw = round((outdoor - forecast - sun) * (curve * 1.2 / 10) / ((curve / 10) + 1), 2)
    if raw > 0 and not up:   raw = 0.0
    if raw < 0 and not down: raw = 0.0
    return max(-10.0, min(10.0, raw))

def calc_indoor_offset(setpoint, actual, factor) -> float:
    return max(-10.0, min(10.0, round((setpoint - actual) * factor, 2)))

def forecast_at_hours(forecasts: list, hours: int) -> Optional[dict]:
    now    = datetime.now(timezone.utc)
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
class NibeController:
    def __init__(self, logger: logging.Logger):
        self.logger  = logger
        self.cfg     = load_config()
        self.state   = load_state()
        self.history: List[dict] = load_json(HISTORY_FILE, [])
        self.ha: Optional[HAClient] = None
        self._live: dict = {}
        self._plan: List[dict] = []  # 24h hourly plan slots

    async def run(self, session: aiohttp.ClientSession):
        self.ha = HAClient(session, self.logger)
        self.logger.info("Controller started")
        await asyncio.gather(self._outdoor_loop(), self._weather_loop(),
                             self._indoor_loop(), self._price_loop(),
                             self._planning_loop(), self._apply_loop())

    async def _outdoor_loop(self):
        """Read outdoor sensor every 2 min, independently of weather forecast."""
        while True:
            try:
                val = await self.ha.get_float(self.cfg.get("outdoor_temp_entity", ""))
                if val is not None:
                    self.state["last_outdoor_temp"] = val
                    self._live["outdoor_temp"] = val
            except Exception as e:
                self.logger.error(f"outdoor: {e}")
            await asyncio.sleep(2 * 60)

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

    async def _planning_loop(self):
        """Rebuild the 24h plan every hour."""
        await asyncio.sleep(90)
        while True:
            try: await self._run_planning()
            except Exception as e: self.logger.error(f"planning: {e}")
            await asyncio.sleep(60 * 60)

    async def _run_planning(self):
        cfg = self.cfg
        if not cfg.get("planning_enabled", True):
            self._plan = []; return

        lookahead = int(cfg.get("planning_lookahead_hours", 24))
        preheat_h = int(cfg.get("price_preheat_hours", 2))
        now       = datetime.now(timezone.utc)

        # Current indoor state for the gate (read from controller state, which
        # the indoor loop refreshes every 5 min; falls back to configured
        # target if no setpoint entity is set — mirrors _run_indoor behaviour)
        indoor_temp = self.state.get("last_indoor_temp")
        indoor_set  = self.state.get("last_indoor_setpoint")
        if indoor_set is None and cfg.get("indoor_enabled"):
            indoor_set = float(cfg.get("indoor_target_temp", 21.0))

        # ── Fetch weather forecast (temp + UV + cloud per hour) ────────────
        forecasts = await self.ha.get_weather_forecast(cfg.get("weather_entity", ""))
        fc_by_hour: dict = {}  # h_offset -> full forecast dict
        if forecasts:
            for fc in forecasts:
                try:
                    dt = datetime.fromisoformat(fc["datetime"].replace("Z", "+00:00"))
                    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                    h = int((dt - now).total_seconds() / 3600)
                    if 0 <= h < lookahead:
                        fc_by_hour[h] = fc
                except Exception: continue

        # ── Fetch Nordpool price series using raw timestamps ───────────────
        price_by_hour: dict = {}
        all_prices_raw: list = []  # flat list for percentile calc
        price_entity = cfg.get("electricity_price_entity", "")
        if price_entity:
            url = f"{self.ha.base}/states/{price_entity}"
            try:
                async with self.ha.session.get(url, headers=self.ha.headers,
                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        attrs = data.get("attributes", {})
                        raw_entries = attrs.get("raw_today", []) + attrs.get("raw_tomorrow", [])
                        buckets: dict = {}
                        for entry in raw_entries:
                            if not entry or entry.get("value") is None: continue
                            try:
                                dt = datetime.fromisoformat(entry["start"])
                                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                                h_offset = int((dt - now).total_seconds() / 3600)
                                val = float(entry["value"])
                                all_prices_raw.append(val)
                                if 0 <= h_offset < lookahead:
                                    buckets.setdefault(h_offset, []).append(val)
                            except Exception: continue
                        price_by_hour = {h: sum(v)/len(v) for h, v in buckets.items() if v}
            except Exception as e:
                self.logger.debug(f"planning price fetch: {e}")

        # ── Fetch current battery SoC ──────────────────────────────────────
        battery_soc: float = None
        battery_entity = cfg.get("battery_entity", "")
        if battery_entity and cfg.get("solar_enabled", False):
            try:
                val = await self.ha.get_float(battery_entity)
                if val is not None:
                    battery_soc = val
                    self._live["battery_soc"] = battery_soc
            except Exception: pass

        if not price_by_hour and not fc_by_hour:
            self.logger.debug("Planning: no data available")
            return

        # ── Solar scoring per hour ─────────────────────────────────────────
        # Use UV index + cloud coverage from OWM forecast to estimate relative
        # solar output as a fraction of peak_kwh (0.0 – 1.0).
        # Formula: solar_fraction = uv_index/11 * (1 - cloud_coverage/100) * 0.85
        # This is intentionally approximate — weighted down vs price anyway.
        solar_enabled  = cfg.get("solar_enabled", False)
        solar_entity   = cfg.get("solar_entity", "")
        peak_kwh       = float(cfg.get("solar_peak_kwh", 10.0))
        solar_weight   = float(cfg.get("solar_weight", 0.4))
        battery_weight = float(cfg.get("battery_weight", 0.3))
        batt_soc_min   = float(cfg.get("battery_useful_soc_min", 20.0))

        # Read current solar output from user-specified entity (W or kWh sensor)
        current_solar_frac = 0.0
        if solar_enabled and solar_entity:
            sol_val = await self.ha.get_float(solar_entity)
            if sol_val is not None and peak_kwh > 0:
                # Accept both W (e.g. 3500 W) and kWh (e.g. 3.5 kWh) sensors
                # Heuristic: if value > 100, assume it's in W, else kWh
                if sol_val > 100:
                    sol_kwh = sol_val / 1000.0   # W → kWh equivalent
                else:
                    sol_kwh = sol_val
                current_solar_frac = min(1.0, max(0.0, sol_kwh / peak_kwh))
                self._live["solar_fraction"] = round(current_solar_frac, 2)
                self._live["solar_kwh"] = round(sol_kwh, 2)

        # For per-slot planning: solar fraction is uniform (current reading).
        # A user with a Solcast-type forecast sensor could extend this later.
        def solar_fraction(fc_slot: dict) -> float:
            return current_solar_frac

        def battery_coverage(soc: float) -> float:
            """0.0–1.0 — how much useful battery reserve exists above minimum."""
            if soc is None: return 0.0
            usable = max(0.0, soc - batt_soc_min)
            return min(1.0, usable / max(1.0, 100.0 - batt_soc_min))

        # ── Stats for dynamic price classification ─────────────────────────
        price_series = all_prices_raw if len(all_prices_raw) >= 8 else list(price_by_hour.values())
        temps_list   = [fc.get("temperature", 0) for fc in fc_by_hour.values() if fc.get("temperature") is not None]
        t_mean       = sum(temps_list) / len(temps_list) if temps_list else None

        # ── Build plan slots ───────────────────────────────────────────────
        plan = []
        heat_curve = float(self._live.get("heat_curve") or 0)
        outdoor    = float(self.state.get("last_outdoor_temp") or 0)

        for h in range(lookahead):
            slot_time = now + timedelta(hours=h)
            fc_slot   = fc_by_hour.get(h, {})
            price     = price_by_hour.get(h)

            # Dynamic price level using full 48h distribution
            price_level = classify_price(price, cfg, price_series) if price is not None else None

            # Base price offset
            price_offset = price_to_offset(price_level, cfg) if price_level else 0.0

            # Solar bonus: good solar ahead → cheaper effective hour → shift offset up slightly
            solar_offset = 0.0
            solar_frac   = 0.0
            if solar_enabled and fc_slot:
                solar_frac = solar_fraction(fc_slot)
                est_kwh    = round(solar_frac * peak_kwh, 2)
                # High solar output = we can afford to heat more during that hour
                # Weight it as a fraction of the VERY_CHEAP offset
                solar_bonus = solar_frac * cfg.get("price_very_cheap", 2.0) * solar_weight
                solar_offset = round(solar_bonus, 2)

            # Battery coverage: if battery is well-charged, night hours become cheaper
            batt_offset = 0.0
            if solar_enabled and battery_soc is not None and fc_slot:
                # Battery helps mostly at night (uv_index == 0)
                uv = float(fc_slot.get("uv_index") or 0)
                if uv == 0:  # nighttime
                    batt_cov = battery_coverage(battery_soc)
                    batt_bonus = batt_cov * cfg.get("price_very_cheap", 2.0) * battery_weight
                    batt_offset = round(batt_bonus, 2)

            # Pre-heat logic: look ahead for expensive windows
            preheat_offset = 0.0
            if price is not None and price_series:
                future_prices = [price_by_hour[h2] for h2 in range(h+1, min(h+1+preheat_h, lookahead))
                                 if h2 in price_by_hour]
                if future_prices:
                    future_level = classify_price(max(future_prices), cfg, price_series)
                    if future_level in ("EXPENSIVE", "VERY_EXPENSIVE"):
                        current_level = classify_price(price, cfg, price_series)
                        if current_level in ("VERY_CHEAP", "CHEAP", "NORMAL"):
                            # Pre-heat now before the expensive window
                            preheat_offset = round(
                                cfg.get("price_cheap", 1.0) * (1.5 if future_level == "VERY_EXPENSIVE" else 1.0), 1)

            # Weather offset for this forecast slot
            weather_offset = 0.0
            if fc_slot.get("temperature") is not None and heat_curve > 0:
                weather_offset = calc_weather_offset(
                    outdoor, float(fc_slot["temperature"]), heat_curve,
                    float(cfg.get("weather_adjust_factor", 0)),
                    cfg.get("weather_enable_up", True),
                    cfg.get("weather_enable_down", True))

            # Combine: weather + price + solar + battery + preheat
            # Gate positive contributions if indoor is currently above setpoint
            w_slot = weather_offset
            p_slot = price_offset + preheat_offset
            if (cfg.get("indoor_enabled") and indoor_temp is not None
                    and indoor_set is not None):
                dead_band_plan = float(cfg.get("indoor_gate_dead_band", 0.5))
                overshoot = indoor_temp - indoor_set - dead_band_plan
                if overshoot > 0:
                    gf = max(0.0, 1.0 - overshoot / 2.0)
                    if w_slot > 0: w_slot = round(w_slot * gf, 2)
                    if p_slot > 0: p_slot = round(p_slot * gf, 2)
                    if solar_offset > 0: solar_offset = round(solar_offset * gf, 2)
                    if batt_offset > 0:  batt_offset  = round(batt_offset  * gf, 2)

            combined = round(max(-10.0, min(10.0,
                w_slot + p_slot + solar_offset + batt_offset)), 1)

            # Build human-readable action annotations
            actions = []
            if price_level in ("VERY_CHEAP", "CHEAP"):
                actions.append(f"Cheap: {price:.4f} ({price_level.replace('_',' ')})")
            elif price_level in ("EXPENSIVE", "VERY_EXPENSIVE"):
                actions.append(f"Expensive: {price:.4f} ({price_level.replace('_',' ')})" if price else "")
            if preheat_offset > 0:
                actions.append(f"Pre-heat +{preheat_offset}°C before {future_level.replace('_',' ').lower()}")
            if solar_enabled and solar_frac > 0.3:
                actions.append(f"Solar ~{round(solar_frac*peak_kwh,1)}kWh")
            if solar_enabled and batt_offset > 0:
                actions.append(f"Battery {battery_soc:.0f}% covers night")
            if fc_slot.get("temperature") is not None and t_mean is not None:
                temp = float(fc_slot["temperature"])
                if temp < t_mean - 2:   actions.append(f"Cold: {temp:.1f}°C")
                elif temp > t_mean + 2: actions.append(f"Warm: {temp:.1f}°C")

            plan.append({
                "hour_offset":        h,
                "ts":                 int(slot_time.timestamp()),
                "temp":               fc_slot.get("temperature"),
                "uv_index":           fc_slot.get("uv_index"),
                "cloud_coverage":     fc_slot.get("cloud_coverage"),
                "price":              round(price, 4) if price else None,
                "price_level":        price_level,
                "weather_plan_offset": round(weather_offset, 2),
                "price_plan_offset":   round(price_offset,   2),
                "solar_offset":        round(solar_offset,   2),
                "battery_offset":      round(batt_offset,    2),
                "preheat_offset":      round(preheat_offset, 2),
                "solar_fraction":      round(solar_frac,     2),
                "combined_plan_offset": combined,
                "actions":            [a for a in actions if a],
            })

        self._plan = plan
        self.logger.info(
            f"Planning: {len(plan)}-slot plan | "
            f"price:{len(price_by_hour)}h ({len(price_series)} raw pts) | "
            f"weather:{len(fc_by_hour)}h | "
            f"solar:{'on' if solar_enabled else 'off'} | "
            f"battery:{f'{battery_soc:.0f}%' if battery_soc is not None else 'n/a'}"
        )

    async def _apply_loop(self):
        await asyncio.sleep(60)
        while True:
            try: await self._apply()
            except Exception as e: self.logger.error(f"apply: {e}")
            try: await self._update_power_estimate()
            except Exception as e: self.logger.debug(f"power est: {e}")
            await asyncio.sleep(60)

    async def _update_power_estimate(self):
        """Estimate heat pump electrical power from compressor status +
        immersion heater register. F1245-8 is fixed speed: compressor either
        draws its rated input (~1.7 kW at 0/35) or nothing. Pump overhead
        (brine GP2 + heating medium GP1) only applies while the compressor runs."""
        cfg = self.cfg
        cpr_ent = cfg.get("compressor_status_entity", "")
        add_ent = cfg.get("int_add_power_entity", "")
        if not cpr_ent and not add_ent:
            self._live.pop("est_power_kw", None)
            self._live.pop("compressor_on", None)
            return
        comp_on = None
        if cpr_ent:
            raw = await self.ha.get_state_raw(cpr_ent)
            if raw is not None:
                comp_on = raw.lower() in ("on", "true", "1", "running")
        add_kw = await self.ha.get_float(add_ent) if add_ent else None
        rated = float(cfg.get("compressor_rated_kw", 1.7))
        pumps = float(cfg.get("pump_overhead_kw", 0.12))
        total = 0.0
        if comp_on:
            total += rated + pumps
        if add_kw:
            total += add_kw
        if comp_on is not None:
            self._live["compressor_on"] = comp_on
        if comp_on is not None or add_kw is not None:
            self._live["est_power_kw"] = round(total, 2)
        if add_kw is not None:
            self._live["int_add_kw"] = round(add_kw, 2)
        prio_ent = cfg.get("prio_entity", "")
        if prio_ent:
            prio = await self.ha.get_state_raw(prio_ent)
            if prio: self._live["prio"] = prio

    async def _run_weather(self):
        cfg = self.cfg
        if not cfg.get("weather_enabled"): self.state["weather_offset"] = 0.0; return
        outdoor = await self.ha.get_float(cfg.get("outdoor_temp_entity", ""))
        curve   = await self.ha.get_float(cfg.get("heat_curve_entity", ""))
        if outdoor is None or not curve: return
        forecasts = await self.ha.get_weather_forecast(cfg.get("weather_entity", ""))
        if not forecasts: self.logger.warning("Weather: no forecast"); return
        hours   = int(cfg.get("forecast_hours", 6))
        fc_now  = forecast_at_hours(forecasts, 0)
        fc_then = forecast_at_hours(forecasts, hours)
        if not fc_then: return
        fr = float(fc_then.get("temperature", outdoor))
        if fc_now:
            tn = float(fc_now.get("temperature", outdoor))
            fr = round((outdoor - tn) + fr, 2)
        offset = calc_weather_offset(outdoor, fr, curve, float(cfg.get("weather_adjust_factor", 0)),
                                     cfg.get("weather_enable_up", True), cfg.get("weather_enable_down", True))
        self.state.update({"weather_offset": offset, "last_outdoor_temp": outdoor, "last_forecast_temp": fr})
        self._live.update({"outdoor_temp": outdoor, "forecast_temp": fr, "heat_curve": curve})
        self.logger.info(f"Weather: {outdoor}→{fr}@{hours}h curve={curve} → {offset:+.2f}")

    async def _run_indoor(self):
        cfg = self.cfg
        if not cfg.get("indoor_enabled"): self.state["indoor_offset"] = 0.0; return
        sp_ent = cfg.get("indoor_setpoint_entity", "")
        setpoint = await self.ha.get_float(sp_ent) if sp_ent else float(cfg.get("indoor_target_temp", 21.0))
        actual   = await self.ha.get_float(cfg.get("indoor_temp_entity", ""))
        if actual is None or setpoint is None: return
        if actual < 4: self.logger.warning(f"Indoor {actual}°C looks like fault"); return
        factor = float(cfg.get("indoor_factor", 10.0))
        offset = calc_indoor_offset(setpoint, actual, factor)
        self.state.update({"indoor_offset": offset, "last_indoor_temp": actual, "last_indoor_setpoint": setpoint})
        self._live.update({"indoor_temp": actual, "indoor_setpoint": setpoint})
        self.logger.info(f"Indoor: {actual}→{setpoint} f={factor} → {offset:+.2f}")

    async def _run_price(self):
        cfg = self.cfg
        if not cfg.get("price_enabled"): self.state["price_offset"] = 0.0; return
        price = await self.ha.get_float(cfg.get("electricity_price_entity", ""))
        if price is None: return
        level  = classify_price(price, cfg)
        offset = price_to_offset(level, cfg)
        self.state.update({"price_offset": offset, "last_price_level": level, "last_price": price})
        self._live.update({"price": price, "price_level": level})
        self.logger.info(f"Price: {price:.4f} → {level} → {offset:+.1f}")

    async def _apply(self):
        cfg      = self.cfg
        s        = self.state
        dry_run  = bool(cfg.get("dry_run", True))
        w   = float(s.get("weather_offset") or 0)
        ind = float(s.get("indoor_offset")  or 0)
        p   = float(s.get("price_offset")   or 0)

        # ── Indoor gate: if house is above setpoint, suppress positive offsets
        # proportionally. The further above setpoint, the more we suppress.
        # dead_band = tolerance before gate activates (default 0.5°C)
        indoor_temp    = s.get("last_indoor_temp")
        indoor_set     = s.get("last_indoor_setpoint")
        dead_band      = float(cfg.get("indoor_gate_dead_band", 0.5))
        gate_factor    = 1.0  # 1.0 = no suppression
        if (cfg.get("indoor_enabled") and indoor_temp is not None
                and indoor_set is not None and indoor_temp > indoor_set + dead_band):
            # How far above setpoint+dead_band (in °C)
            overshoot = indoor_temp - indoor_set - dead_band
            # Linearly suppress positive contributions: 0 at dead_band, full at dead_band+2°C
            gate_factor = max(0.0, 1.0 - overshoot / 2.0)
            # Apply gate to weather and price (only suppress positive parts)
            w_gated = min(w, w * gate_factor) if w > 0 else w
            p_gated = min(p, p * gate_factor) if p > 0 else p
            if gate_factor < 1.0:
                self.logger.debug(
                    f"Indoor gate: {indoor_temp:.1f}°C > {indoor_set:.1f}+{dead_band}°C "
                    f"→ gate_factor={gate_factor:.2f} (weather {w:+.2f}→{w_gated:+.2f}, "
                    f"price {p:+.2f}→{p_gated:+.2f})")
            w, p = w_gated, p_gated

        combined = max(-10.0, min(10.0, round(w + ind + p, 1)))
        min_interval = float(cfg.get("min_write_interval_min", MIN_WRITE_INTERVAL)) * 60
        elapsed = time.time() - (s.get("last_write_ts") or 0)
        last    = s.get("last_combined_offset")

        # ── Slew limit: never move the offset more than max_step_per_write in
        # a single write. F1245 menu 5.1.3 ("max diff flow line temp", default
        # 10°C) forces DM to +2 and stops the compressor if the actual supply
        # exceeds the calculated supply by that amount. One offset step ≈ 2.5°C
        # of calculated supply, so a swing > 4 steps could hard-stop the
        # compressor mid-cycle. Default limit of 3 steps ≈ 7.5°C stays safely
        # under the trip wire while still reacting within 2-3 write cycles.
        max_step = float(cfg.get("max_step_per_write", 3.0))
        if last is not None and abs(combined - last) > max_step:
            slewed = last + max_step if combined > last else last - max_step
            self.logger.info(
                f"Slew limit: target {combined:+.1f} clamped to {slewed:+.1f} "
                f"(max {max_step:.1f} steps/write, protects against 5.1.3 trip)")
            combined = round(slewed, 1)

        delta   = abs(combined - last) if last is not None else 999
        if delta < 0.2: return
        if delta < 0.5 and elapsed < min_interval: return

        # ── Hot water priority guard: curve offset only affects space heating;
        # during hot water production the pump runs fixed-condensing. Defer
        # the write so it lands when it can actually take effect.
        prio_entity = cfg.get("prio_entity", "")
        if prio_entity and not dry_run:
            prio = await self.ha.get_state_raw(prio_entity)
            if prio and "hot water" in prio.lower():
                self.logger.debug(f"Prio is '{prio}' — deferring offset write until HW cycle ends")
                return

        offset_entity = cfg.get("curve_offset_entity", "")
        if not offset_entity and not dry_run: return
        reasons = self._build_reasons(w, ind, p, cfg, s)
        if dry_run:
            self.logger.info(f"DRY RUN — would apply {combined:+.1f}°C (not written to pump)")
        else:
            self.logger.info(f"Apply {combined:+.1f}°C → {offset_entity}")
        # In dry run: always log but never write. Outside dry run: log only on success.
        wrote = False
        if not dry_run:
            wrote = await self.ha.set_number(offset_entity, combined)
        if dry_run or wrote:
            s.update({"last_combined_offset": combined, "last_write_ts": int(time.time())})
            save_json(STATE_FILE, s)
            entry = {"ts": int(time.time()), "combined": combined,
                     "weather": round(w, 2), "indoor": round(ind, 2), "price": round(p, 2),
                     "price_level": s.get("last_price_level", "UNKNOWN"),
                     "outdoor_temp": s.get("last_outdoor_temp"), "indoor_temp": s.get("last_indoor_temp"),
                     "indoor_setpoint": s.get("last_indoor_setpoint"),
                     "forecast_temp": s.get("last_forecast_temp"), "price_value": s.get("last_price"),
                     "dry_run": dry_run,
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
                out.append(f"Forecast {diff}°C {'colder' if f < o else 'warmer'} in {cfg.get('forecast_hours',6)}h → {w:+.2f}°C")
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
            out.append(f"Electricity {lv} ({f'{pv:.4f}' if pv else '?'}) → {p:+.1f}°C")
        if not out:
            out.append("No active adjustments")
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
            "last_outdoor_temp":    s.get("last_outdoor_temp"),
            "last_indoor_temp":     s.get("last_indoor_temp"),
            "last_indoor_setpoint": s.get("last_indoor_setpoint"),
            "last_forecast_temp":   s.get("last_forecast_temp"),
            "last_price":           s.get("last_price"),
            "dry_run": bool(self.cfg.get("dry_run", True)),
            "planning_enabled": bool(self.cfg.get("planning_enabled", True)),
            **self._live,
        }

# ---------------------------------------------------------------------------
class WebApp:
    def __init__(self, ctrl: NibeController, logger: logging.Logger):
        self.ctrl   = ctrl
        self.logger = logger

    def build(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/",             self._index)
        app.router.add_get("/api/status",   self._status)
        app.router.add_get("/api/history",  self._history)
        app.router.add_get("/api/config",   self._config_get)
        app.router.add_post("/api/config",  self._config_post)
        app.router.add_get("/api/entities", self._entities)
        app.router.add_get("/api/plan",     self._plan_api)
        return app

    async def _index(self, req: web.Request) -> web.Response:
        ingress_path = req.headers.get("X-Ingress-Path", "").rstrip("/")
        return web.Response(
            text=FRONTEND_HTML.replace("%%INGRESS_PATH%%", ingress_path),
            content_type="text/html")

    async def _status(self, req):   return web.json_response(self.ctrl.get_status())
    async def _history(self, req):
        n = int(req.rel_url.query.get("n", 200))
        return web.json_response(self.ctrl.history[-n:])

    async def _config_get(self, req): return web.json_response(self.ctrl.cfg)

    async def _config_post(self, req):
        try:
            body = await req.json()
            for k in ["forecast_hours","weather_adjust_factor","indoor_target_temp",
                      "indoor_factor","price_very_cheap","price_cheap","price_normal",
                      "price_expensive","price_very_expensive","price_very_cheap_threshold",
                      "price_cheap_threshold","price_expensive_threshold",
                      "price_very_expensive_threshold","min_write_interval_min",
                      "planning_lookahead_hours","price_preheat_hours",
                      "solar_peak_kwh","solar_weight","battery_weight","battery_useful_soc_min",
                      "indoor_gate_dead_band","max_step_per_write",
                      "compressor_rated_kw","pump_overhead_kw"]:
                if k in body: body[k] = float(body[k])
            for k in ["weather_enabled","weather_enable_up","weather_enable_down",
                      "indoor_enabled","price_enabled","dry_run","planning_enabled","solar_enabled"]:
                if k in body: body[k] = bool(body[k])
            self.ctrl.cfg.update(body)
            save_json(CONFIG_FILE, self.ctrl.cfg)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _entities(self, req):
        domain = req.rel_url.query.get("domain", "")
        return web.json_response(await self.ctrl.ha.list_entities(domain))

    async def _plan_api(self, req):
        return web.json_response(self.ctrl._plan)

    async def start(self, port=8099):
        runner = web.AppRunner(self.build())
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        self.logger.info(f"Listening on :{port}")

# ---------------------------------------------------------------------------
FRONTEND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>Nibe Smart Control</title>
<script>window.__INGRESS_PATH__ = "%%INGRESS_PATH%%";</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://unpkg.com/react@18/umd/react.production.min.js" crossorigin></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js" crossorigin></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1117;--sur:#181c27;--sur2:#1f2436;--bdr:#2a3050;
  --txt:#e4e9f7;--mut:#7b87a8;--acc:#e05c2a;
  --cold:#3a82f7;--warm:#f7953a;--grn:#2ec27e;--ylw:#f6d24a;
  --font:'Inter',system-ui,sans-serif;--mono:ui-monospace,monospace;--r:8px;
}
html,body,#root{height:100%;background:var(--bg);color:var(--txt);font-family:var(--font);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:2px}
input,select,button,textarea{font-family:var(--font);font-size:14px;outline:none}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes toastIn{from{transform:translateY(16px);opacity:0}to{transform:translateY(0);opacity:1}}
</style>
</head>
<body>
<div id="root"><div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#7b87a8;font-size:13px">Loading…</div></div>
<script>
const {useState,useEffect,useRef,useCallback,createElement:h} = React;

// ── Ingress-aware API ─────────────────────────────────────────────────────────
const _BASE = (window.__INGRESS_PATH__ || '').replace(/\\/+$/, '');
const apiFetch = async (path, opts={}) => {
  const url = _BASE + '/' + path.replace(/^\\/+/, '');
  const res = await fetch(url, {headers:{'Content-Type':'application/json'}, ...opts});
  if (!res.ok) { const e = await res.json().catch(()=>({})); throw new Error(e.error || 'HTTP '+res.status); }
  return res.json();
};
const GET  = p => apiFetch(p);
const POST = (p,d) => apiFetch(p, {method:'POST', body:JSON.stringify(d)});

// ── Toast ─────────────────────────────────────────────────────────────────────
const ToastCtx = React.createContext(null);
function ToastProvider({children}) {
  const [toasts, setToasts] = useState([]);
  const add = useCallback((msg, type='success') => {
    const id = Date.now();
    setToasts(t => [...t, {id,msg,type}]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 3000);
  }, []);
  const colors = {success:'#2ec27e', error:'#e05c2a', info:'#3a82f7', warn:'#f6d24a'};
  return h(ToastCtx.Provider, {value:add},
    children,
    h('div', {style:{position:'fixed',bottom:20,right:20,zIndex:9999,display:'flex',flexDirection:'column',gap:8,pointerEvents:'none'}},
      toasts.map(t => h('div', {key:t.id, style:{background:'#181c27',border:`1px solid ${colors[t.type]}`,color:colors[t.type],padding:'10px 16px',borderRadius:8,fontSize:13,fontWeight:600,animation:'toastIn .2s ease',boxShadow:'0 8px 24px rgba(0,0,0,.5)'}}, t.msg))
    )
  );
}
const useToast = () => React.useContext(ToastCtx);

// ── Entity search with dropdown ───────────────────────────────────────────────
const _entCache = {};
async function fetchEnts(domains) {
  const key = domains.join(',');
  if (_entCache[key]) return _entCache[key];
  try {
    const all = [];
    for (const d of domains) {
      const r = await GET('api/entities?domain='+d);
      all.push(...r);
    }
    _entCache[key] = all;
    return all;
  } catch(e) { return []; }
}

function EntityInput({label, name, value, onChange, domains=['sensor'], hint}) {
  const [q, setQ]       = useState(value || '');
  const [opts, setOpts]  = useState([]);
  const [open, setOpen]  = useState(false);
  const ref              = useRef();
  useEffect(() => setQ(value || ''), [value]);

  useEffect(() => {
    if (q.length < 2) { setOpen(false); return; }
    let cancelled = false;
    fetchEnts(domains).then(all => {
      if (cancelled) return;
      const hits = all.filter(e =>
        e.entity_id.toLowerCase().includes(q.toLowerCase()) ||
        e.friendly_name.toLowerCase().includes(q.toLowerCase())
      ).slice(0, 10);
      setOpts(hits);
      setOpen(hits.length > 0);
    });
    return () => { cancelled = true; };
  }, [q]);

  useEffect(() => {
    const h = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, []);

  return h('div', {style:{marginBottom:14}, ref},
    label && h('div', {style:{fontSize:11,fontWeight:600,color:'#7b87a8',textTransform:'uppercase',letterSpacing:'.07em',marginBottom:5}}, label),
    h('div', {style:{position:'relative'}},
      h('input', {
        value: q, autoComplete: 'off',
        placeholder: domains[0]+'.entity_name',
        style: {width:'100%',background:'#1f2436',border:'1px solid #2a3050',color:'#e4e9f7',padding:'8px 10px',borderRadius:6,fontSize:13,transition:'border-color .15s'},
        onChange: e => { setQ(e.target.value); onChange(e.target.value); },
        onFocus: () => q.length >= 2 && setOpen(opts.length > 0),
      }),
      open && h('div', {style:{position:'absolute',top:'100%',left:0,right:0,zIndex:400,background:'#1f2436',border:'1px solid #e05c2a',borderTop:'none',borderRadius:'0 0 6px 6px',maxHeight:180,overflowY:'auto'}},
        opts.map(e => h('div', {
          key: e.entity_id,
          style: {padding:'7px 10px',cursor:'pointer',borderBottom:'1px solid #2a3050'},
          onMouseDown: () => { setQ(e.entity_id); onChange(e.entity_id); setOpen(false); },
          onMouseEnter: ev => ev.currentTarget.style.background='rgba(224,92,42,.1)',
          onMouseLeave: ev => ev.currentTarget.style.background='transparent',
        },
          h('div', {style:{fontSize:13}}, e.friendly_name),
          h('div', {style:{fontSize:11,color:'#7b87a8'}}, e.entity_id+(e.unit?' · '+e.unit:''))
        ))
      )
    ),
    hint && h('div', {style:{fontSize:11,color:'#7b87a8',marginTop:4}}, hint)
  );
}

// ── Toggle ────────────────────────────────────────────────────────────────────
function Toggle({label, checked, onChange}) {
  return h('label', {style:{display:'flex',alignItems:'center',gap:10,cursor:'pointer',marginBottom:10}},
    h('div', {
      style:{width:36,height:20,borderRadius:10,background:checked?'#e05c2a':'#2a3050',position:'relative',transition:'background .2s',flexShrink:0},
      onClick: () => onChange(!checked),
    },
      h('div', {style:{position:'absolute',width:14,height:14,top:3,borderRadius:'50%',background:'#fff',transition:'transform .2s',transform:checked?'translateX(19px)':'translateX(3px)'}})
    ),
    h('span', {style:{fontSize:13}}, label)
  );
}

// ── Number field ──────────────────────────────────────────────────────────────
function NumField({label, value, onChange, min, max, step=0.5, hint}) {
  return h('div', {style:{marginBottom:14}},
    label && h('div', {style:{fontSize:11,fontWeight:600,color:'#7b87a8',textTransform:'uppercase',letterSpacing:'.07em',marginBottom:5}}, label),
    h('input', {type:'number', value, min, max, step,
      style:{width:'100%',background:'#1f2436',border:'1px solid #2a3050',color:'#e4e9f7',padding:'8px 10px',borderRadius:6,fontSize:13},
      onChange: e => onChange(e.target.value)}),
    hint && h('div', {style:{fontSize:11,color:'#7b87a8',marginTop:4}}, hint)
  );
}

// ── Decomp bar ────────────────────────────────────────────────────────────────
function DecompBar({weather=0, indoor=0, price=0}) {
  const pct = v => 50 + (v / 10) * 50;
  const segs = [
    {v:weather, color:'#3a82f7', label:'Weather'},
    {v:indoor,  color:'#2ec27e', label:'Indoor'},
    {v:price,   color:'#e05c2a', label:'Price'},
  ];
  const rowH = Math.floor((34 - 8) / 3);
  return h('div', null,
    h('div', {style:{display:'flex',justifyContent:'space-between',fontSize:10,color:'#7b87a8',marginBottom:5}},
      h('span', null, '−10°C'), h('span', null, '0'), h('span', null, '+10°C')
    ),
    h('div', {style:{position:'relative',height:34,background:'#1f2436',borderRadius:6,overflow:'hidden',border:'1px solid #2a3050'}},
      h('div', {style:{position:'absolute',left:'50%',top:0,bottom:0,width:1,background:'#2a3050',zIndex:2}}),
      segs.map((s, i) => {
        const l = Math.min(50, pct(s.v)), r = Math.max(50, pct(s.v));
        return h('div', {key:s.label, style:{
          position:'absolute', borderRadius:3,
          left:l+'%', width:(r-l)+'%',
          top:(4+i*rowH)+'px', height:(rowH-2)+'px',
          background:s.color+'aa', border:`1px solid ${s.color}`,
          transition:'left .5s,width .5s',
        }});
      })
    ),
    h('div', {style:{display:'flex',gap:14,marginTop:8}},
      segs.map(s => h('span', {key:s.label, style:{fontSize:11,color:'#7b87a8',display:'flex',alignItems:'center',gap:5}},
        h('div', {style:{width:8,height:8,borderRadius:2,background:s.color,flexShrink:0}}),
        s.label, ' ',
        h('b', {style:{color:'#e4e9f7'}}, (s.v > 0 ? '+' : '') + s.v.toFixed(2) + '°C')
      ))
    )
  );
}

// ── Price badge ───────────────────────────────────────────────────────────────
function PriceBadge({level}) {
  const styles = {
    VERY_CHEAP:     {bg:'#2ec27e22',color:'#2ec27e',border:'1px solid #2ec27e55'},
    CHEAP:          {bg:'#a3e63522',color:'#a3e635',border:'1px solid #a3e63555'},
    NORMAL:         {bg:'#7b87a822',color:'#7b87a8',border:'1px solid #7b87a844'},
    EXPENSIVE:      {bg:'#f6d24a22',color:'#f6d24a',border:'1px solid #f6d24a55'},
    VERY_EXPENSIVE: {bg:'#e05c2a22',color:'#e05c2a',border:'1px solid #e05c2a55'},
    UNKNOWN:        {bg:'#2a305044',color:'#7b87a8',border:'1px solid #2a3050'},
  };
  const s = styles[level] || styles.UNKNOWN;
  return h('span', {style:{display:'inline-flex',alignItems:'center',padding:'2px 9px',borderRadius:20,fontSize:11,fontWeight:600,background:s.bg,color:s.color,border:s.border}},
    (level||'UNKNOWN').replace('_',' '));
}

// ── Chart component ───────────────────────────────────────────────────────────
function LineChart({id, datasets, labels, height=200, yMin, yMax}) {
  const ref = useRef(); const chart = useRef();
  useEffect(() => {
    if (!ref.current) return;
    if (chart.current) chart.current.destroy();
    chart.current = new Chart(ref.current, {
      type:'line', data:{labels, datasets},
      options:{
        responsive:true, maintainAspectRatio:false, animation:false,
        plugins:{legend:{labels:{color:'#7b87a8',usePointStyle:true,boxWidth:8,padding:12}},tooltip:{mode:'index',intersect:false,backgroundColor:'#181c27',titleColor:'#7b87a8',bodyColor:'#e4e9f7',borderColor:'#2a3050',borderWidth:1}},
        scales:{x:{ticks:{color:'#7b87a8',maxTicksLimit:8,maxRotation:0},grid:{color:'#2a3050'}},y:{min:yMin,max:yMax,ticks:{color:'#7b87a8'},grid:{color:'#2a3050'}}},
      }
    });
    return () => { if (chart.current) chart.current.destroy(); };
  }, [datasets, labels]);
  return h('div', {style:{position:'relative',height}}, h('canvas', {ref}));
}

function BarChart({id, data, labels, colors, height=160}) {
  const ref = useRef(); const chart = useRef();
  useEffect(() => {
    if (!ref.current) return;
    if (chart.current) chart.current.destroy();
    chart.current = new Chart(ref.current, {
      type:'bar', data:{labels, datasets:[{label:'Price',data,backgroundColor:colors,borderWidth:0}]},
      options:{responsive:true,maintainAspectRatio:false,animation:false,plugins:{legend:{display:false},tooltip:{backgroundColor:'#181c27',titleColor:'#7b87a8',bodyColor:'#e4e9f7',borderColor:'#2a3050',borderWidth:1}},scales:{x:{ticks:{color:'#7b87a8',maxTicksLimit:8,maxRotation:0},grid:{color:'#2a3050'}},y:{min:0,ticks:{color:'#7b87a8'},grid:{color:'#2a3050'}}}},
    });
    return () => { if (chart.current) chart.current.destroy(); };
  }, [data, labels, colors]);
  return h('div', {style:{position:'relative',height}}, h('canvas', {ref}));
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const fmtOff = v => v == null ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(1) + '°C';
const fmtTemp = v => v == null ? '—' : Number(v).toFixed(1) + '°C';
const fmtTs = ts => ts ? new Date(ts*1000).toLocaleString('en-GB',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const offColor = v => Number(v) > 0.05 ? '#f7953a' : Number(v) < -0.05 ? '#3a82f7' : '#7b87a8';

// ── Section header ────────────────────────────────────────────────────────────
function SectionHead({title}) {
  return h('div', {style:{fontSize:11,fontWeight:600,textTransform:'uppercase',letterSpacing:'.08em',color:'#e05c2a',marginBottom:12,paddingBottom:6,borderBottom:'1px solid #2a3050'}}, title);
}

// ── Card ──────────────────────────────────────────────────────────────────────
function Card({title, children, style={}}) {
  return h('div', {style:{background:'#181c27',border:'1px solid #2a3050',borderRadius:8,padding:18,marginBottom:14,...style}},
    title && h('div', {style:{fontSize:11,fontWeight:600,textTransform:'uppercase',letterSpacing:'.08em',color:'#7b87a8',marginBottom:14}}, title),
    children
  );
}

// ── Stat tile ─────────────────────────────────────────────────────────────────
function Stat({label, value, note, valueColor}) {
  return h('div', null,
    h('div', {style:{fontSize:11,textTransform:'uppercase',letterSpacing:'.07em',color:'#7b87a8',marginBottom:4}}, label),
    h('div', {style:{fontFamily:'ui-monospace,monospace',fontSize:22,fontWeight:700,color:valueColor||'#e4e9f7'}}, value),
    note && h('div', {style:{fontSize:11,color:'#7b87a8',marginTop:3}}, note)
  );
}

// ── Grid ──────────────────────────────────────────────────────────────────────
function Grid({cols=3, children}) {
  return h('div', {style:{display:'grid',gridTemplateColumns:`repeat(${cols},1fr)`,gap:14,marginBottom:14}}, children);
}

// ── History row ───────────────────────────────────────────────────────────────
function HistoryRow({entry}) {
  const tagColor = r => {
    if (r.includes('Forecast') || r.includes('colder') || r.includes('warmer')) return {color:'#3a82f7',bg:'#3a82f711',border:'#3a82f744',icon:'⛅'};
    if (r.includes('Indoor') || r.includes('setpoint')) return {color:'#2ec27e',bg:'#2ec27e11',border:'#2ec27e44',icon:'🌡'};
    if (r.includes('Electricity') || r.includes('CHEAP') || r.includes('EXPENSIVE')) return {color:'#e05c2a',bg:'#e05c2a11',border:'#e05c2a44',icon:'⚡'};
    return {color:'#7b87a8',bg:'transparent',border:'#2a3050',icon:'•'};
  };
  const isDry = entry.dry_run === true;
  return h('div', {style:{display:'grid',gridTemplateColumns:'120px 80px 1fr',gap:10,padding:'11px 0',borderBottom:'1px solid #2a3050',alignItems:'start',opacity:isDry?0.75:1}},
    h('div', {style:{fontFamily:'ui-monospace,monospace',fontSize:11,color:'#7b87a8'}},
      fmtTs(entry.ts),
      isDry && h('span', {style:{display:'inline-block',marginLeft:5,padding:'1px 5px',borderRadius:3,fontSize:9,fontWeight:700,letterSpacing:'.05em',color:'#f6d24a',background:'rgba(246,210,74,.12)',border:'1px solid rgba(246,210,74,.3)'}}, 'DRY RUN')
    ),
    h('div', {style:{fontFamily:'ui-monospace,monospace',fontSize:15,fontWeight:700,color:offColor(entry.combined)}}, fmtOff(entry.combined)),
    h('div', {style:{fontSize:12,color:'#7b87a8',lineHeight:1.7}},
      (entry.reasons||[]).map((r,i) => {
        const tc = tagColor(r);
        return h('div', {key:i},
          h('span', {style:{display:'inline-block',padding:'1px 5px',borderRadius:3,fontSize:10,fontWeight:600,marginRight:5,color:tc.color,background:tc.bg,border:`1px solid ${tc.border}`}}, tc.icon),
          r
        );
      })
    )
  );
}

// ── Dashboard tab ─────────────────────────────────────────────────────────────
function DashboardTab({status, cfg}) {
  const minInterval = (cfg && cfg.min_write_interval_min) || 10;
  const dryRun = status.dry_run !== false;  // default true until explicitly disabled
  const elapsed = status.last_write_ts ? (Date.now()/1000 - status.last_write_ts) : null;
  const rem = elapsed != null ? Math.max(0, minInterval * 60 - elapsed) : null;
  return h('div', {style:{animation:'fadeIn .3s ease'}},
    dryRun && h('div', {style:{
      background:'rgba(246,210,74,.08)', border:'1px solid rgba(246,210,74,.35)',
      borderRadius:8, padding:'12px 18px', marginBottom:14,
      display:'flex', alignItems:'center', gap:12,
    }},
      h('div', {style:{fontSize:20, lineHeight:1}}, '🔬'),
      h('div', null,
        h('div', {style:{fontWeight:700, fontSize:13, color:'#f6d24a', marginBottom:2}}, 'Dry Run mode — heat pump is not being controlled'),
        h('div', {style:{fontSize:12, color:'#7b87a8'}}, 'The addon is calculating and logging what it would do, but writing nothing to the pump. Disable Dry Run in Settings when you are ready to go live.')
      )
    ),
    h(Grid, {cols:3},
      h(Card, {title:'Combined offset'},   h(Stat, {label:'Written to heat pump', value:fmtOff(status.combined_offset), valueColor:offColor(status.combined_offset), note:'Last: '+fmtTs(status.last_write_ts)})),
      h(Card, {title:'Outdoor'},           h(Stat, {label:'Current', value:fmtTemp(status.last_outdoor_temp), note:status.last_forecast_temp!=null?'Forecast → '+fmtTemp(status.last_forecast_temp):''})),
      h(Card, {title:'Electricity'},       h(Stat, {label:'Current price', value:status.last_price!=null?status.last_price.toFixed(4):'—', note:h(PriceBadge, {level:status.price_level})}))
    ),
    h(Card, {title:'Offset decomposition'},
      h(DecompBar, {weather:status.weather_offset||0, indoor:status.indoor_offset||0, price:status.price_offset||0})
    ),
    h(Grid, {cols:2},
      h(Card, {title:'Indoor'}, h(Stat, {label:'Actual', value:fmtTemp(status.last_indoor_temp), note:status.last_indoor_setpoint!=null?'Setpoint → '+fmtTemp(status.last_indoor_setpoint):''})),
      h(Card, {title:'Rate limiting'}, h(Stat, {label:'Next write', value:rem!=null?(rem>0?`${Math.ceil(rem/60)} min`:'Ready'):'—', note:rem!=null&&rem>0?`${minInterval} min interval active`:''  }))
    ),
    (status.est_power_kw!=null || status.compressor_on!=null) && h(Grid, {cols:2},
      h(Card, {title:'Compressor'}, h(Stat, {
        label:'Status',
        value: status.compressor_on==null ? '—' : (status.compressor_on ? 'Running' : 'Idle'),
        valueColor: status.compressor_on ? '#2ec27e' : '#7b87a8',
        note: status.prio ? 'Priority: '+status.prio : ''
      })),
      h(Card, {title:'Estimated power'}, h(Stat, {
        label:'Electrical draw',
        value: status.est_power_kw!=null ? status.est_power_kw.toFixed(2)+' kW' : '—',
        valueColor: status.est_power_kw>0 ? '#f6a23a' : '#7b87a8',
        note: status.int_add_kw>0 ? 'Immersion heater: '+status.int_add_kw.toFixed(1)+' kW' : 'Compressor + pumps model'
      }))
    )
  );
}

// ── History tab ───────────────────────────────────────────────────────────────
function HistoryTab() {
  const [history, setHistory] = useState(null);
  useEffect(() => { GET('api/history').then(setHistory).catch(()=>setHistory([])); }, []);
  if (history === null) return h('div', {style:{color:'#7b87a8',padding:20}}, 'Loading…');
  if (!history.length) return h('div', {style:{color:'#7b87a8',padding:20,fontSize:13}}, 'No changes recorded yet. The addon writes to the heat pump once the first sensor readings come in.');
  return h(Card, {title:'Change log'},
    h('div', null, [...history].reverse().map((e,i) => h(HistoryRow, {key:i, entry:e})))
  );
}

// ── Charts tab ────────────────────────────────────────────────────────────────
function ChartsTab() {
  const [history, setHistory] = useState(null);
  useEffect(() => { GET('api/history?n=200').then(setHistory).catch(()=>setHistory([])); }, []);
  if (history === null) return h('div', {style:{color:'#7b87a8',padding:20}}, 'Loading…');
  if (!history.length) return h('div', {style:{color:'#7b87a8',padding:20,fontSize:13}}, 'No data yet.');
  const labels = history.map(e => fmtTs(e.ts));
  const lvColor = lv => lv==='VERY_CHEAP'?'#2ec27e88':lv==='CHEAP'?'#a3e63588':lv==='NORMAL'?'#7b87a888':lv==='EXPENSIVE'?'#f6d24a88':'#e05c2a88';
  return h('div', null,
    h(Card, {title:'Offset over time'},
      h(LineChart, {id:'offsets', labels, height:200,
        datasets:[
          {label:'Combined',data:history.map(e=>e.combined),borderColor:'#e05c2a',backgroundColor:'#e05c2a22',fill:true,tension:.3,pointRadius:2},
          {label:'Weather', data:history.map(e=>e.weather), borderColor:'#3a82f7',fill:false,tension:.3,pointRadius:2},
          {label:'Indoor',  data:history.map(e=>e.indoor),  borderColor:'#2ec27e',fill:false,tension:.3,pointRadius:2},
          {label:'Price',   data:history.map(e=>e.price),   borderColor:'#f6d24a',fill:false,tension:.3,pointRadius:2},
        ]
      })
    ),
    h(Card, {title:'Temperatures'},
      h(LineChart, {id:'temps', labels, height:200,
        datasets:[
          {label:'Outdoor',  data:history.map(e=>e.outdoor_temp),   borderColor:'#7b87a8',tension:.3,pointRadius:2},
          {label:'Forecast', data:history.map(e=>e.forecast_temp),  borderColor:'#3a82f7',borderDash:[4,3],tension:.3,pointRadius:2},
          {label:'Indoor',   data:history.map(e=>e.indoor_temp),    borderColor:'#2ec27e',tension:.3,pointRadius:2},
          {label:'Setpoint', data:history.map(e=>e.indoor_setpoint),borderColor:'#2ec27e66',borderDash:[2,3],tension:.3,pointRadius:0},
        ]
      })
    ),
    h(Card, {title:'Electricity price'},
      h(BarChart, {id:'price', labels, height:160,
        data:   history.map(e=>e.price_value),
        colors: history.map(e=>lvColor(e.price_level)),
      })
    )
  );
}

// ── Settings tab ──────────────────────────────────────────────────────────────
function SettingsTab() {
  const toast    = useToast();
  const [cfg, setCfg]     = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => { GET('api/config').then(setCfg).catch(()=>toast('Failed to load config','error')); }, []);

  const set = (k,v) => setCfg(c => ({...c, [k]:v}));

  const save = async () => {
    setSaving(true);
    try {
      await POST('api/config', cfg);
      toast('Configuration saved ✓');
    } catch(e) {
      toast(e.message, 'error');
    } finally { setSaving(false); }
  };

  if (!cfg) return h('div', {style:{color:'#7b87a8',padding:20}}, 'Loading…');

  const inp = (label, k, type='text', extra={}) =>
    h(NumField, {label, value:cfg[k]??'', onChange:v=>set(k,v), ...extra});

  return h('div', {style:{animation:'fadeIn .3s ease'}},
    h(Card, null,

      h(SectionHead, {title:'Heat pump entities (NibeGW / ESPHome)'}),
      h('div', {style:{display:'grid',gridTemplateColumns:'1fr 1fr',gap:0}},
        h(EntityInput, {label:'Outdoor temperature sensor', name:'outdoor_temp_entity', value:cfg.outdoor_temp_entity, onChange:v=>set('outdoor_temp_entity',v), domains:['sensor']}),
        h('div', {style:{width:14}}),
        h(EntityInput, {label:'Heat curve (read only)', name:'heat_curve_entity', value:cfg.heat_curve_entity, onChange:v=>set('heat_curve_entity',v), domains:['number']}),
        h('div', {style:{width:14}}),
        h(EntityInput, {label:'Curve offset — addon writes here', name:'curve_offset_entity', value:cfg.curve_offset_entity, onChange:v=>set('curve_offset_entity',v), domains:['number']}),
      ),

      h(SectionHead, {title:'Power & status monitoring (optional)'}),
      h('div', {style:{fontSize:12,color:'#7b87a8',marginBottom:8}},
        'From the official nibe_heatpump integration. Compressor status enables the live power estimate; priority defers curve writes during hot water production.'),
      h('div', {style:{display:'grid',gridTemplateColumns:'1fr 1fr',gap:0}},
        h(EntityInput, {label:'Compressor status (e.g. binary_sensor.cpr_status_ep14_43435)', name:'compressor_status_entity', value:cfg.compressor_status_entity, onChange:v=>set('compressor_status_entity',v), domains:['binary_sensor','sensor']}),
        h('div', {style:{width:14}}),
        h(EntityInput, {label:'Priority (e.g. sensor.prio_43086)', name:'prio_entity', value:cfg.prio_entity, onChange:v=>set('prio_entity',v), domains:['sensor']}),
        h('div', {style:{width:14}}),
        h(EntityInput, {label:'Immersion heater power (e.g. sensor.int_el_add_power_43084)', name:'int_add_power_entity', value:cfg.int_add_power_entity, onChange:v=>set('int_add_power_entity',v), domains:['sensor']}),
      ),
      h('div', {style:{display:'grid',gridTemplateColumns:'1fr 1fr',gap:0}},
        h(NumField, {label:'Compressor rated draw (kW)', value:cfg.compressor_rated_kw, min:0.5, max:5, step:0.05, hint:'F1245-8: ~1.70 kW at 0/35', onChange:v=>set('compressor_rated_kw',v)}),
        h('div', {style:{width:14}}),
        h(NumField, {label:'Pump overhead (kW)', value:cfg.pump_overhead_kw, min:0, max:0.5, step:0.01, hint:'Brine + heating medium pumps while compressor runs', onChange:v=>set('pump_overhead_kw',v)}),
      ),

      h(SectionHead, {title:'Weather forecast'}),
      h('div', {style:{display:'grid',gridTemplateColumns:'1fr 1fr',gap:0}},
        h(EntityInput, {label:'Weather entity', name:'weather_entity', value:cfg.weather_entity, onChange:v=>set('weather_entity',v), domains:['weather']}),
        h('div', {style:{width:14}}),
        h(NumField, {label:'Forecast lookahead (hours)', value:cfg.forecast_hours, min:1, max:24, step:1, onChange:v=>set('forecast_hours',v)}),
        h('div', {style:{width:14}}),
        h(NumField, {label:'Static bias (°C)', value:cfg.weather_adjust_factor, min:-2, max:2, step:0.5, hint:'Fine-tune if pump over/undershoots', onChange:v=>set('weather_adjust_factor',v)}),
      ),
      h(Toggle, {label:'Enable weather control',     checked:!!cfg.weather_enabled,      onChange:v=>set('weather_enabled',v)}),
      h(Toggle, {label:'Allow raising offset',        checked:!!cfg.weather_enable_up,    onChange:v=>set('weather_enable_up',v)}),
      h(Toggle, {label:'Allow lowering offset',       checked:!!cfg.weather_enable_down,  onChange:v=>set('weather_enable_down',v)}),

      h(SectionHead, {title:'Indoor temperature'}),
      h('div', {style:{display:'grid',gridTemplateColumns:'1fr 1fr',gap:0}},
        h(EntityInput, {label:'Indoor temperature sensor', name:'indoor_temp_entity', value:cfg.indoor_temp_entity, onChange:v=>set('indoor_temp_entity',v), domains:['sensor']}),
        h('div', {style:{width:14}}),
        h(EntityInput, {label:'Setpoint entity (optional)', name:'indoor_setpoint_entity', value:cfg.indoor_setpoint_entity, onChange:v=>set('indoor_setpoint_entity',v), domains:['sensor','number','input_number']}),
        h('div', {style:{width:14}}),
        h(NumField, {label:'Target indoor temp (°C)', value:cfg.indoor_target_temp, min:10, max:28, step:0.5, hint:'Used when no setpoint entity', onChange:v=>set('indoor_target_temp',v)}),
        h('div', {style:{width:14}}),
        h(NumField, {label:'P-factor', value:cfg.indoor_factor, min:1, max:50, step:1, hint:'offset = (setpoint − actual) × factor. Default 10', onChange:v=>set('indoor_factor',v)}),
      ),
      h(Toggle, {label:'Enable indoor temperature control', checked:!!cfg.indoor_enabled, onChange:v=>set('indoor_enabled',v)}),
      cfg.indoor_enabled && h('div', {style:{maxWidth:300,marginTop:10}},
        h(NumField, {label:'Gate dead band (°C)', value:cfg.indoor_gate_dead_band!=null?cfg.indoor_gate_dead_band:0.5, min:0, max:3, step:0.25,
          hint:'If indoor exceeds setpoint by this much, heating offsets are suppressed. 0 = strict, 0.5 = recommended.',
          onChange:v=>set('indoor_gate_dead_band',v)})
      ),

      h(SectionHead, {title:'Electricity price'}),
      h(EntityInput, {label:'Price sensor', name:'electricity_price_entity', value:cfg.electricity_price_entity, onChange:v=>set('electricity_price_entity',v), domains:['sensor']}),
      h(Toggle, {label:'Enable price control', checked:!!cfg.price_enabled, onChange:v=>set('price_enabled',v)}),
      h('div', {style:{fontSize:11,color:'#7b87a8',textTransform:'uppercase',letterSpacing:'.06em',margin:'10px 0 8px'}}, 'Curve offsets per price level (°C)'),
      h('div', {style:{display:'grid',gridTemplateColumns:'repeat(5,1fr)',gap:10}},
        h(NumField, {label:'Very Cheap', value:cfg.price_very_cheap, min:-5, max:5, step:0.5, onChange:v=>set('price_very_cheap',v)}),
        h(NumField, {label:'Cheap',      value:cfg.price_cheap,      min:-5, max:5, step:0.5, onChange:v=>set('price_cheap',v)}),
        h(NumField, {label:'Normal',     value:cfg.price_normal,     min:-5, max:5, step:0.5, onChange:v=>set('price_normal',v)}),
        h(NumField, {label:'Expensive',  value:cfg.price_expensive,  min:-5, max:5, step:0.5, onChange:v=>set('price_expensive',v)}),
        h(NumField, {label:'Very Expensive', value:cfg.price_very_expensive, min:-5, max:5, step:0.5, onChange:v=>set('price_very_expensive',v)}),
      ),
      h('div', {style:{fontSize:11,color:'#7b87a8',textTransform:'uppercase',letterSpacing:'.06em',margin:'4px 0 8px'}}, 'Price thresholds (EUR/kWh)'),
      h('div', {style:{display:'grid',gridTemplateColumns:'repeat(4,1fr)',gap:10}},
        h(NumField, {label:'Very Cheap ≤', value:cfg.price_very_cheap_threshold, min:0, step:0.01, onChange:v=>set('price_very_cheap_threshold',v)}),
        h(NumField, {label:'Cheap ≤',      value:cfg.price_cheap_threshold,      min:0, step:0.01, onChange:v=>set('price_cheap_threshold',v)}),
        h(NumField, {label:'Expensive ≥',  value:cfg.price_expensive_threshold,  min:0, step:0.01, onChange:v=>set('price_expensive_threshold',v)}),
        h(NumField, {label:'Very Expensive ≥', value:cfg.price_very_expensive_threshold, min:0, step:0.01, onChange:v=>set('price_very_expensive_threshold',v)}),
      ),

      h(SectionHead, {title:'Solar & Battery'}),
      h('div', {style:{background:'rgba(246,210,74,.06)',border:'1px solid rgba(246,210,74,.2)',borderRadius:8,padding:'12px 14px',marginBottom:14}},
        h('div', {style:{fontSize:12,color:'#7b87a8',marginBottom:10,lineHeight:1.6}},
          'Point to any HA sensor that represents current solar output — W or kWh, the addon detects the unit automatically. ',
          'Hours with high solar output are treated as effectively cheaper to run the pump. ',
          h('strong',{style:{color:'#f6d24a'}},'Solar and battery are weighted lower than the raw electricity price.')
        ),
        h(Toggle, {label:'Enable solar & battery planning', checked:!!cfg.solar_enabled, onChange:v=>set('solar_enabled',v)}),
        h('div', {style:{display:'grid',gridTemplateColumns:'1fr 1fr',gap:10,marginTop:10}},
          h(EntityInput, {label:'Solar output sensor', name:'solar_entity', value:cfg.solar_entity||'', onChange:v=>set('solar_entity',v), domains:['sensor'], hint:'e.g. sensor.solax_measured_power (W) or sensor.solax_today_s_solar_energy (kWh)'}),
          h(EntityInput, {label:'Battery SoC entity', name:'battery_entity', value:cfg.battery_entity||'', onChange:v=>set('battery_entity',v), domains:['sensor'], hint:'e.g. sensor.solax_battery_capacity'}),
          h(NumField, {label:'Solar peak output (kWh)', value:cfg.solar_peak_kwh||10, min:1, max:50, step:0.5, hint:'Your system peak on a perfect sunny day', onChange:v=>set('solar_peak_kwh',v)}),
          h(NumField, {label:'Solar weight (0–1)', value:cfg.solar_weight||0.4, min:0.1, max:1, step:0.1, hint:'How much solar shifts the effective price', onChange:v=>set('solar_weight',v)}),
          h(NumField, {label:'Battery weight (0–1)', value:cfg.battery_weight||0.3, min:0.1, max:1, step:0.1, hint:'Influence of battery SoC on night-time scoring', onChange:v=>set('battery_weight',v)}),
          h(NumField, {label:'Min useful SoC (%)', value:cfg.battery_useful_soc_min||20, min:5, max:50, step:5, hint:'Reserve % kept for outages — not counted as available', onChange:v=>set('battery_useful_soc_min',v)}),
        )
      ),

      h(SectionHead, {title:'Planning (24h lookahead)'}),
      h('div', {style:{background:'rgba(58,130,247,.06)',border:'1px solid rgba(58,130,247,.2)',borderRadius:8,padding:'12px 14px',marginBottom:14}},
        h('div', {style:{fontSize:12,color:'#7b87a8',marginBottom:10,lineHeight:1.6}},
          'The planner fetches the full 48h Nordpool price schedule and weather forecast. ',
          h('strong',{style:{color:'#3a82f7'}},'Price thresholds are dynamic — '),
          'classified relative to the 48h distribution so the addon automatically adapts between summer and winter pricing. ',
          'It also pre-heats before expensive windows and cold spells.'
        ),
        h(Toggle, {label:'Enable 24h planning', checked:!!cfg.planning_enabled, onChange:v=>set('planning_enabled',v)}),
        h('div', {style:{display:'grid',gridTemplateColumns:'1fr 1fr',gap:10,marginTop:10}},
          h(NumField, {label:'Lookahead hours', value:cfg.planning_lookahead_hours||24, min:6, max:48, step:1, hint:'How many hours ahead to plan (6–48)', onChange:v=>set('planning_lookahead_hours',v)}),
          h(NumField, {label:'Pre-heat lead time (hours)', value:cfg.price_preheat_hours||2, min:1, max:6, step:1, hint:'Hours before expensive window to start pre-heating', onChange:v=>set('price_preheat_hours',v)}),
        )
      ),

      h(SectionHead, {title:'Dry Run'}),
      h('div', {style:{background:'rgba(246,210,74,.06)',border:'1px solid rgba(246,210,74,.2)',borderRadius:8,padding:'12px 14px',marginBottom:14}},
        h('div', {style:{fontSize:12,color:'#7b87a8',marginBottom:10,lineHeight:1.6}},
          'When enabled, the addon calculates and logs every decision it would make, but ',
          h('strong', {style:{color:'#f6d24a'}}, 'never writes'),
          ' to the heat pump. Run for a week to verify the logic before going live.'
        ),
        h(Toggle, {label:'Dry Run — observe only, do not control heat pump', checked:!!cfg.dry_run, onChange:v=>set('dry_run',v)})
      ),

      h(SectionHead, {title:'Rate limiting'}),
      h('div', {style:{maxWidth:260}}),
        h(NumField, {label:'Min minutes between writes', value:cfg.min_write_interval_min, min:5, max:120, step:5, hint:'Protects the heat pump compressor (5–120 min)', onChange:v=>set('min_write_interval_min',v)}),
        h('div', {style:{width:14}}),
        h(NumField, {label:'Max offset steps per write', value:cfg.max_step_per_write, min:1, max:10, step:0.5, hint:'1 step ≈ 2.5°C supply. Keep ≤ 3 to stay under the pump\u2019s 5.1.3 compressor-stop trip (10°C)', onChange:v=>set('max_step_per_write',v)}),

      h('div', {style:{marginTop:8,display:'flex',alignItems:'center',gap:12}},
        h('button', {
          onClick: save, disabled: saving,
          style:{background:'#e05c2a',color:'#fff',border:'none',borderRadius:6,padding:'10px 28px',fontFamily:'inherit',fontSize:14,fontWeight:600,cursor:'pointer',opacity:saving?.6:1,transition:'opacity .15s'},
        }, saving ? 'Saving…' : 'Save configuration'),
      )
    )
  );
}

// ── Nav tab button ────────────────────────────────────────────────────────────
function NavBtn({label, active, onClick}) {
  return h('button', {
    onClick,
    style:{background:'none',border:'none',color:active?'#e4e9f7':'#7b87a8',padding:'10px 16px',cursor:'pointer',fontFamily:'inherit',fontSize:14,borderBottom:active?'2px solid #e05c2a':'2px solid transparent',marginBottom:-1,transition:'all .15s'},
  }, label);
}


// ── Planning tab ──────────────────────────────────────────────────────────────
function PriceLevelDot({level}) {
  const c = {VERY_CHEAP:'#2ec27e',CHEAP:'#a3e635',NORMAL:'#7b87a8',EXPENSIVE:'#f6d24a',VERY_EXPENSIVE:'#e05c2a'};
  return h('div', {style:{width:10,height:10,borderRadius:'50%',background:c[level]||'#2a3050',flexShrink:0,marginTop:2}});
}

function PlanningTab({status, cfg}) {
  const [plan,  setPlan]  = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    GET('api/plan')
      .then(p => { setPlan(p); setError(null); })
      .catch(e => setError(e.message));
  }, []);

  if (error) return h(Card, null, h('div', {style:{color:'#e05c2a',fontSize:13}}, 'Error loading plan: ', error));
  if (plan === null) return h('div', {style:{color:'#7b87a8',padding:20}}, 'Loading plan…');
  if (!plan.length) return h(Card, null,
    h('div', {style:{color:'#7b87a8',fontSize:13,lineHeight:1.8}},
      'No plan available yet — the planner runs once per hour.',h('br'),
      'Make sure your weather entity (weather.openweathermap) and electricity price entity are configured in Settings.',h('br'),
      'The planner will generate its first plan within 90 seconds of startup.'
    )
  );

  const now = Date.now() / 1000;

  // Find min/max for scale
  const allOffsets = plan.map(s => s.combined_plan_offset).filter(v => v != null);
  const allTemps   = plan.map(s => s.temp).filter(v => v != null);
  const allPrices  = plan.map(s => s.price).filter(v => v != null);
  const maxAbsOff  = Math.max(1, ...allOffsets.map(Math.abs));
  const tMin = allTemps.length  ? Math.min(...allTemps)  : 0;
  const tMax = allTemps.length  ? Math.max(...allTemps)  : 30;
  const pMin = allPrices.length ? Math.min(...allPrices) : 0;
  const pMax = allPrices.length ? Math.max(...allPrices) : 0.3;

  const barW = (v, maxAbs) => Math.max(2, Math.round(Math.abs(v) / maxAbs * 120));
  const tempBar = t => t == null ? 0 : Math.round((t - tMin) / Math.max(0.1, tMax - tMin) * 80);
  const priceBar = p => p == null ? 0 : Math.round((p - pMin) / Math.max(0.001, pMax - pMin) * 80);

  return h('div', null,
    // Summary cards
    h('div', {style:{display:'grid',gridTemplateColumns:'repeat(3,1fr)',gap:14,marginBottom:14}},
      h(Card, {title:'Cheapest window (next 24h)'},
        (() => {
          const cheapSlot = [...plan].filter(s=>s.price!=null).sort((a,b)=>a.price-b.price)[0];
          if (!cheapSlot) return h('div',{style:{color:'#7b87a8',fontSize:13}},'No price data');
          const dt = new Date(cheapSlot.ts*1000);
          return h('div', null,
            h('div',{style:{fontFamily:'ui-monospace,monospace',fontSize:22,fontWeight:700,color:'#2ec27e'}},
              `€${cheapSlot.price.toFixed(4)}`),
            h('div',{style:{fontSize:12,color:'#7b87a8',marginTop:4}},
              dt.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}) +
              ' in +' + cheapSlot.hour_offset + 'h'),
            h('div',{style:{fontSize:12,color:'#7b87a8'}}, 'Planned: '+
              (cheapSlot.combined_plan_offset>0?'+':'')+cheapSlot.combined_plan_offset+'°C')
          );
        })()
      ),
      h(Card, {title:'Most expensive window'},
        (() => {
          const expSlot = [...plan].filter(s=>s.price!=null).sort((a,b)=>b.price-a.price)[0];
          if (!expSlot) return h('div',{style:{color:'#7b87a8',fontSize:13}},'No price data');
          const dt = new Date(expSlot.ts*1000);
          return h('div', null,
            h('div',{style:{fontFamily:'ui-monospace,monospace',fontSize:22,fontWeight:700,color:'#e05c2a'}},
              `€${expSlot.price.toFixed(4)}`),
            h('div',{style:{fontSize:12,color:'#7b87a8',marginTop:4}},
              dt.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}) +
              ' in +' + expSlot.hour_offset + 'h'),
            h('div',{style:{fontSize:12,color:'#7b87a8'}}, 'Planned: '+
              (expSlot.combined_plan_offset>0?'+':'')+expSlot.combined_plan_offset+'°C')
          );
        })()
      ),
      h(Card, {title:'Coldest window'},
        (() => {
          const coldSlot = [...plan].filter(s=>s.temp!=null).sort((a,b)=>a.temp-b.temp)[0];
          if (!coldSlot) return h('div',{style:{color:'#7b87a8',fontSize:13}},'No temp data');
          const dt = new Date(coldSlot.ts*1000);
          return h('div', null,
            h('div',{style:{fontFamily:'ui-monospace,monospace',fontSize:22,fontWeight:700,color:'#3a82f7'}},
              coldSlot.temp.toFixed(1)+'°C'),
            h('div',{style:{fontSize:12,color:'#7b87a8',marginTop:4}},
              dt.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}) +
              ' in +' + coldSlot.hour_offset + 'h'),
            h('div',{style:{fontSize:12,color:'#7b87a8'}}, 'Planned: '+
              (coldSlot.combined_plan_offset>0?'+':'')+coldSlot.combined_plan_offset+'°C')
          );
        })()
      )
    ),

    // Timeline
    h(Card, {title:'24h hourly plan'},
      h('div', {style:{overflowX:'auto'}},
        h('div', {style:{minWidth:860}},
          // ── Header ──────────────────────────────────────────────────────
          h('div', {style:{
            display:'grid',
            gridTemplateColumns:'52px 160px 52px 70px 72px 52px 52px 1fr',
            gap:10, padding:'0 6px 8px', borderBottom:'1px solid #2a3050',
            fontSize:10, textTransform:'uppercase', letterSpacing:'.07em',
            color:'#7b87a8', marginBottom:2,
          }},
            h('div',null,'Time'),
            h('div',{style:{textAlign:'center'}},'Offset'),
            h('div',null,''),  // value label column
            h('div',null,'Temp'),
            h('div',null,'Price'),
            h('div',null,'Solar'),
            h('div',null,'Batt'),
            h('div',null,'Actions')
          ),
          plan.map((slot, i) => {
            const dt      = new Date(slot.ts * 1000);
            const past    = slot.ts < now - 60;
            const curr    = !past && (i === 0 || plan[i-1].ts < now);
            const timeStr = dt.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
            const v       = slot.combined_plan_offset || 0;
            const barColor = v > 0.05 ? '#f7953a' : v < -0.05 ? '#3a82f7' : '#2a3050';
            const valColor = v > 0.05 ? '#f7953a' : v < -0.05 ? '#3a82f7' : '#7b87a8';

            // Centred bidirectional bar: total width 140px, zero at centre (70px)
            const BAR_HALF = 70;
            const maxAbs   = Math.max(1, maxAbsOff);
            const barPx    = Math.round(Math.abs(v) / maxAbs * BAR_HALF);
            const barLeft  = v >= 0 ? BAR_HALF : BAR_HALF - barPx;

            // Actions: one per line, colour by type
            const actionEls = (slot.actions || []).map((a, ai) => {
              const c = a.includes('Pre-heat') ? '#f6d24a'
                      : a.includes('Cheap') || a.includes('Solar') || a.includes('Battery') ? '#2ec27e'
                      : a.includes('Expensive') ? '#e05c2a'
                      : '#7b87a8';
              return h('div', {key:ai, style:{color:c, fontSize:11, lineHeight:'1.5'}}, a);
            });

            return h('div', {key:i, style:{
              display:'grid',
              gridTemplateColumns:'52px 160px 52px 70px 72px 52px 52px 1fr',
              gap:10, padding:'6px 6px',
              borderBottom:'1px solid rgba(42,48,80,.6)',
              alignItems:'center',
              opacity: past ? 0.35 : 1,
              background: curr ? 'rgba(224,92,42,.07)' : i%2===0 ? 'transparent' : 'rgba(31,36,54,.3)',
              borderRadius:4,
            }},
              // Time
              h('div', {style:{fontFamily:'ui-monospace,monospace',fontSize:12,color:curr?'#e05c2a':'#7b87a8',fontWeight:curr?700:400,lineHeight:'1.3'}},
                timeStr,
                curr && h('div',{style:{fontSize:9,fontWeight:700,color:'#e05c2a',letterSpacing:'.06em'}},'NOW')
              ),
              // Offset bar — bidirectional, centred at 70px
              h('div', {style:{position:'relative',height:8,background:'rgba(42,48,80,.8)',borderRadius:4,overflow:'hidden'}},
                h('div', {style:{position:'absolute',top:0,bottom:0,left:'50%',width:1,background:'rgba(255,255,255,.12)',zIndex:1}}),
                barPx > 0 && h('div', {style:{
                  position:'absolute', top:1, bottom:1,
                  left:barLeft, width:barPx,
                  background:barColor, borderRadius:3,
                  transition:'left .4s, width .4s',
                }})
              ),
              // Offset value
              h('div', {style:{fontFamily:'ui-monospace,monospace',fontSize:12,fontWeight:600,color:valColor,textAlign:'right'}},
                (v>0?'+':'')+v.toFixed(1)+'°'
              ),
              // Temp
              h('div', {style:{fontSize:12,color: slot.temp != null ? '#e4e9f7' : '#2a3050'}},
                slot.temp != null ? slot.temp.toFixed(1)+'°C' : '—'
              ),
              // Price with dot
              h('div', {style:{display:'flex',alignItems:'center',gap:5}},
                slot.price != null && h(PriceLevelDot, {level:slot.price_level}),
                h('span',{style:{fontFamily:'ui-monospace,monospace',fontSize:11,color:slot.price!=null?'#e4e9f7':'#2a3050'}},
                  slot.price != null ? '€'+slot.price.toFixed(4) : '—')
              ),
              // Solar %
              h('div', {style:{fontSize:11,color: (slot.solar_fraction||0) > 0.3 ? '#f6d24a' : '#2a3050',textAlign:'center'}},
                (slot.solar_fraction||0) > 0 ? Math.round(slot.solar_fraction*100)+'%' : '—'
              ),
              // Battery bonus
              h('div', {style:{fontSize:11,color:(slot.battery_offset||0)>0?'#2ec27e':'#2a3050',textAlign:'center'}},
                (slot.battery_offset||0) > 0 ? '+'+slot.battery_offset.toFixed(1) : '—'
              ),
              // Actions
              h('div', {style:{lineHeight:1}}, actionEls.length ? actionEls : h('span',{style:{color:'#2a3050',fontSize:11}},'—'))
            );
          })
        )
      )
    ),

    // Settings hint
    h('div', {style:{fontSize:12,color:'#7b87a8',padding:'4px 2px'}},
      'Planning runs hourly. Preheat lead time and lookahead are configurable in Settings.'
    )
  );
}

// ── App ───────────────────────────────────────────────────────────────────────
function App() {
  const [tab,    setTab]    = useState('dashboard');
  const [status, setStatus] = useState({});
  const [cfg,    setCfg]    = useState({});
  const [live,   setLive]   = useState(true);

  useEffect(() => {
    const poll = async () => {
      try {
        const s = await GET('api/status');
        setStatus(s); setLive(true);
      } catch(e) { setLive(false); }
    };
    poll();
    const t = setInterval(poll, 30000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    GET('api/config').then(setCfg).catch(()=>{});
  }, []);

  const tabs = [
    {id:'dashboard', label:'Dashboard'},
    {id:'history',   label:'History'},
    {id:'charts',    label:'Charts'},
    {id:'planning',  label:'Planning'},
    {id:'settings',  label:'Settings'},
  ];

  return h(ToastProvider, null,
    // Header
    h('header', {style:{background:'#181c27',borderBottom:'1px solid #2a3050',padding:'14px 20px',display:'flex',alignItems:'center',gap:12}},
      h('svg', {width:26,height:26,viewBox:'0 0 26 26',fill:'none'},
        h('circle', {cx:13,cy:13,r:12,stroke:'#e05c2a',strokeWidth:1.5}),
        h('path', {d:'M13 21C13 21 7.5 16.5 7.5 11.5A5.5 5.5 0 0 1 18.5 11.5C18.5 16.5 13 21 13 21Z',fill:'#e05c2a33',stroke:'#e05c2a',strokeWidth:1.2}),
        h('circle', {cx:13,cy:11.5,r:2.2,fill:'#e05c2a'})
      ),
      h('h1', {style:{fontSize:16,fontWeight:600,letterSpacing:'.02em'}}, 'Nibe Smart Control'),
      h('div', {style:{marginLeft:'auto',display:'flex',alignItems:'center',gap:6,fontSize:12,color:'#7b87a8'}},
        h('div', {style:{width:7,height:7,borderRadius:'50%',background:live?'#2ec27e':'#e05c2a',boxShadow:live?'0 0 5px #2ec27e':'none',animation:live?'pulse 2s infinite':'none'}}),
        live ? 'Live' : 'Offline'
      )
    ),
    // Nav
    h('nav', {style:{background:'#181c27',borderBottom:'1px solid #2a3050',padding:'0 20px',display:'flex',gap:2}},
      tabs.map(t => h(NavBtn, {key:t.id, label:t.label, active:tab===t.id, onClick:()=>setTab(t.id)}))
    ),
    // Content
    h('main', {style:{padding:20,maxWidth:1080}},
      tab === 'dashboard' && h(DashboardTab, {status, cfg}),
      tab === 'history'   && h(HistoryTab),
      tab === 'charts'    && h(ChartsTab),
      tab === 'planning'  && h(PlanningTab, {status, cfg}),  // pass status for battery display
      tab === 'settings'  && h(SettingsTab),
    )
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(h(App));
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    options   = load_json(DATA_DIR / "options.json", {"log_level": "info"})
    logger    = setup_logging(options.get("log_level", "info"))
    logger.info("Nibe Smart Control v1.3.0 starting")
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        ctrl   = NibeController(logger)
        webapp = WebApp(ctrl, logger)
        await webapp.start(8099)
        await ctrl.run(session)

if __name__ == "__main__":
    asyncio.run(main())
