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
