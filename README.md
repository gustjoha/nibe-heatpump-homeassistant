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

### Price thresholds (EUR/kWh — Lithuanian Ignitis spot market defaults)

| Level | Default threshold | Default offset |
|---|---|---|
| VERY_CHEAP | ≤ 0.05 | +2°C |
| CHEAP | ≤ 0.08 | +1°C |
| NORMAL | < 0.14 | 0°C |
| EXPENSIVE | < 0.20 | −1°C |
| VERY_EXPENSIVE | ≥ 0.20 | −2°C |

---

## License

MIT
