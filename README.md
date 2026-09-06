# Nibe Heatpump HomeAssistant

Home Assistant addon repository for Nibe F-series geothermal heat pumps.

## Add to Home Assistant

1. Go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add: `https://github.com/gustjoha/nibe-heatpump-homeassistant`
3. Find **Nibe Smart Control** and install

---

## Nibe Smart Control

Intelligent heat curve control for **Nibe F1145 / F1245 / F1345** heat pumps, integrated natively with Home Assistant via NibeGW + ESPHome.

Ported from the [NibePi](https://github.com/anerdins/node-red-contrib-nibepi) Node-RED algorithm — no Node-RED, no Raspberry Pi required.

### What it controls

| Control loop | How it works |
|---|---|
| **Weather forecast** | Fetches HA weather entity, looks N hours ahead, adjusts curve so the house is warm before a cold front arrives |
| **Indoor temperature** | Proportional controller: `offset = (setpoint − actual) × factor` — keeps indoor temp steady |
| **Electricity price** | Classifies Nordpool/Tibber price into 5 levels, applies configurable offset per level |

All three offsets are summed and written to the NibeGW curve offset entity, with rate limiting to protect the compressor.

### Web dashboard (port 8099)

- **Dashboard** — live offset decomposition bar showing weather / indoor / price contributions
- **History** — every write to the heat pump with human-readable reasons
- **Charts** — offset components, temperatures, and electricity price over time
- **Settings** — entity pickers with live autocomplete from your HA instance

### Prerequisites

- NibeGW running on ESP32 (ESPHome), connected to your F-series pump
- The following entities enabled in HA (may need to be manually enabled in the ESPHome device page):

| Entity | Register | Purpose |
|---|---|---|
| `sensor.nibe_outdoor_temperature` | 40004 | Outdoor temp (read) |
| `number.nibe_heat_curve_s1` | 47007 | Heat curve steepness (read) |
| `number.nibe_heat_offset_s1` | 47011 | **Curve offset (addon writes here)** |

### Recommended NIBE settings when using this addon

The F1245 has its own internal room-compensation loop (**menu 1.9.4, "Room sensor settings"**), which nudges the calculated supply temperature based on the difference between a target room temperature and its own room sensor (BT50), scaled by a "factor system" gain. The installer manual itself warns that too high a factor system value can produce an unstable room temperature — which is exactly the risk of running that internal loop *and* this addon's indoor P-controller at the same time, especially if they're reading different physical sensors. **Set the room sensor factor system to 0 (or disable room sensor influence on the curve) so this addon is the sole source of indoor-driven correction.**

Once that's disabled, tune the indoor P-factor to your emitter type — the manual states the room-temperature-per-curve-step relationship differs a lot by system:

| Emitter | Curve steps per 1°C of room temperature | Suggested P-factor |
|---|---|---|
| Underfloor heating | ~1 | 1 |
| Radiators | ~2–3 | 2–3 |

Underfloor systems also have much larger thermal mass than radiators — a slab can take hours to visibly respond to a curve change. With the pump's own compensation disabled, this addon is the only thing correcting for indoor temperature, so reacting to every 5-minute sample before the slab has caught up from the *previous* correction causes the same kind of hunting the manual warns about. The **"Min. minutes between indoor reactions"** setting (default 45 min) holds the committed indoor offset steady between real changes for this reason — the indoor overshoot gate still reacts immediately regardless, so safety isn't delayed by the hold.

### Configuration

All settings are available in the addon's web UI on port 8099 — no need to edit YAML manually. Entity IDs autocomplete from your live HA instance.

Key options:

```yaml
weather_entity: "weather.forecast_home"
electricity_price_entity: "sensor.nordpool_kwh_lt_eur_3_10_025"
outdoor_temp_entity: "sensor.nibe_outdoor_temperature"
indoor_temp_entity: ""          # optional
curve_offset_entity: "number.nibe_heat_offset_s1"
forecast_hours: 6
min_write_interval_min: 10      # rate limiting — protects the compressor
```

### Electricity price classification — adaptive, not fixed

Prices are classified into 5 percentile bands (VERY_CHEAP ≤15th pct, CHEAP ≤40th, NORMAL ≤75th, EXPENSIVE ≤92nd, VERY_EXPENSIVE top 8%) computed from a **rolling window of real Nord Pool history** (default 21 days, configurable), not a fixed EUR/kWh number. Nord Pool prices move too much for a fixed threshold to stay meaningful — LT prices roughly halved between early and mid-2026 alone. Both the 5-minute reactive control loop and the hourly planner read from the same rolling window, so they never disagree about what counts as "cheap" that day.

On first install (or after a long addon downtime) there isn't enough history yet — the addon uses a fixed fallback until it has collected about half a day of data, shown live on the Dashboard:

| Level | Fallback threshold (EUR/kWh) | Default offset |
|---|---|---|
| VERY_CHEAP | ≤ 0.15 | +2°C |
| CHEAP | ≤ 0.22 | +1°C |
| NORMAL | < 0.32 | 0°C |
| EXPENSIVE | < 0.38 | −1°C |
| VERY_EXPENSIVE | ≥ 0.38 | −2°C |

These fallback numbers are a safety net, not something you need to tune to the current market — the adaptive window takes over automatically.

### Solar forecasting

The planner can weight cheap/expensive decisions by expected solar production. Two modes:

- **Manual entity** — point at any live solar sensor (W or kWh); the addon applies that single reading flatly across every hour of the plan. Simple, but blind to time-of-day.
- **[Helios Forecast](https://github.com/ReikanYsora/Helios-Forecast)** — a genuine self-learning solar production forecast. Helios exposes point values (`power_now`, `power_next_hour`) and daily totals over a 7-day horizon as plain sensors, but not a full hourly curve via simple entities — so the addon shapes those daily totals (today's remaining production, tomorrow's total) into an hourly curve itself, using a bell curve centred on a configurable daylight window (adjust seasonally — Lithuanian winter daylight is much shorter than summer). This means solar correctly reads near-zero at night and peaks around midday rather than repeating one flat number for all 24 hours.

Enable under **Settings → Solar & Battery → Use Helios Forecast**, then point the three entity fields at your Helios device's sensors (defaults match Helios's standard naming). If you have multiple panel lines (different roof orientations), each gets its own Helios device — sum them into a template sensor first, or point the addon at your primary line.

### Power estimation — no energy meter installed

Without an AA3 energy meter board (BE6/BE7) or a CT clamp, the addon estimates electrical draw from compressor status + immersion heater register rather than measuring it directly. This estimate distinguishes a few things that materially affect accuracy:

- **Space heating vs hot water draw are different constants.** Hot water targets a much higher condensing temperature (~46–50°C) than underfloor space heating (~35°C), and the manual's own EN14511 figures show COP dropping substantially between those two conditions — meaning real electrical draw is meaningfully higher during every HW cycle. The addon reads the Priority sensor (already used for the curve-offset write guard) to pick between "Compressor draw — space heating" and "Compressor draw — hot water" in Settings, rather than applying one flat number regardless of what the compressor is actually doing. The hot-water figure is a physically-reasoned estimate (COP-derived), not a manual value confirmed to two decimals — refine it against a real CT clamp reading once one is installed.
- **Heating medium pump power scales with its real reported speed** (%), across the manual's documented 7–67W range, rather than a flat guess — and applies whether or not the compressor is currently running, since the pump can circulate independently in "auto" mode.
- **A small standby draw is always included**, since the control board and display never fully power down even when the compressor, both pumps, and the immersion heater are all idle. This alone accounts for roughly 0.5 kWh/day that a "0 kW when idle" model would silently miss.
- **Brine pump (GP2)** stays a flat, compressor-gated estimate (30–87W range from the manual) since there's no live speed telemetry for it.

All of these are configurable under **Settings → Power & status monitoring**.

---

## Home Assistant statistics integration (optional)

A separate **custom component** (not an addon) lives in [`custom_components/nibe_smart_control_stats/`](custom_components/nibe_smart_control_stats/) and exposes the addon's computed values — curve offset, indoor/outdoor temp, electricity price, estimated power, and a lifetime energy counter — as normal HA sensor entities. Because they carry a proper `state_class`, Home Assistant's recorder automatically keeps short-term (5 min) and long-term (hourly) statistics for them, so they show up in the History and Statistics UI and the lifetime energy sensor is compatible with the Energy Dashboard.

### Install

1. Copy `custom_components/nibe_smart_control_stats/` into your HA config's `custom_components/` folder (via Samba, SSH, or the Studio Code Server addon), then restart Home Assistant.
2. **Settings → Devices & services → Add integration → Nibe Smart Control Statistics**
3. Enter the addon's host and port 8099. Find the hostname under **Settings → Add-ons → Nibe Smart Control → Info** (usually the slug with hyphens, e.g. `32c51978-nibe-smart-control`), or use the HA host's LAN IP.

### Backfilling pre-existing history

If the addon already has history from before you installed the integration, run the one-shot service once: **Developer tools → Actions → `Nibe Smart Control Statistics: Backfill history`**. This imports past data via HA's external-statistics API into its own named series (visible in the Statistics UI, separate from the live entities). Don't run it a second time over an overlapping window — the energy total will double-count.

---

## License

MIT
