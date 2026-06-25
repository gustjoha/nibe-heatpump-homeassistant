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
    "weather_entity": "", "electricity_price_entity": "",
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
    "price_very_cheap_threshold": 0.05, "price_cheap_threshold": 0.08,
    "price_expensive_threshold": 0.14, "price_very_expensive_threshold": 0.20,
    "min_write_interval_min": MIN_WRITE_INTERVAL, "dry_run": True, "log_level": "info",
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
        try:
            async with self.session.get(f"{self.base}/states/{entity_id}", headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    s = await r.json()
                    fc = s.get("attributes", {}).get("forecast", [])
                    if fc: return sorted(fc, key=lambda x: x.get("datetime", ""))
        except Exception: pass
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
def classify_price(price: float, cfg: dict) -> str:
    t = [cfg.get("price_very_cheap_threshold", 0.05), cfg.get("price_cheap_threshold", 0.08),
         cfg.get("price_expensive_threshold", 0.14),  cfg.get("price_very_expensive_threshold", 0.20)]
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

    async def run(self, session: aiohttp.ClientSession):
        self.ha = HAClient(session, self.logger)
        self.logger.info("Controller started")
        await asyncio.gather(self._outdoor_loop(), self._weather_loop(),
                             self._indoor_loop(), self._price_loop(), self._apply_loop())

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

    async def _apply_loop(self):
        await asyncio.sleep(60)
        while True:
            try: await self._apply()
            except Exception as e: self.logger.error(f"apply: {e}")
            await asyncio.sleep(60)

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
        combined = max(-10.0, min(10.0, round(w + ind + p, 1)))
        min_interval = float(cfg.get("min_write_interval_min", MIN_WRITE_INTERVAL)) * 60
        elapsed = time.time() - (s.get("last_write_ts") or 0)
        last    = s.get("last_combined_offset")
        delta   = abs(combined - last) if last is not None else 999
        if delta < 0.2: return
        if delta < 0.5 and elapsed < min_interval: return
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
                      "price_very_expensive_threshold","min_write_interval_min"]:
                if k in body: body[k] = float(body[k])
            for k in ["weather_enabled","weather_enable_up","weather_enable_down",
                      "indoor_enabled","price_enabled","dry_run"]:
                if k in body: body[k] = bool(body[k])
            self.ctrl.cfg.update(body)
            save_json(CONFIG_FILE, self.ctrl.cfg)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _entities(self, req):
        domain = req.rel_url.query.get("domain", "")
        return web.json_response(await self.ctrl.ha.list_entities(domain))

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
