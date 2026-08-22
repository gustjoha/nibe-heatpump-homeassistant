"""One-shot historical backfill using the recorder's external-statistics API.

Ongoing collection (see sensor.py) uses plain sensor entities and lets HA's
recorder derive statistics the normal way — that's the maintained, stable
path. This module is only for pulling in data the addon already accumulated
*before* the integration was set up (its history.json / power_history.json),
so it isn't lost. It is invoked once via the `nibe_smart_control_stats.
backfill_history` service, not on every coordinator refresh.

External statistics require statistic_id in "domain:name" form and are kept
in a separate table from entity statistics, so they show up as their own
series in the Statistics/History UI (searchable by name) rather than being
attached to one of the sensor entities above.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

import aiohttp

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import HISTORY_PATH, POWER_PATH, STAT_DOMAIN

_LOGGER = logging.getLogger(__name__)

# HA 2025.11+ requires mean_type on statistics metadata (has_mean is
# deprecated). Older cores don't have StatisticMeanType at all, so fall back
# to has_mean if the import fails — keeps this working across HA versions.
try:
    from homeassistant.components.recorder.statistics import StatisticMeanType
    _HAS_MEAN_TYPE = True
except ImportError:  # pragma: no cover - only on older HA cores
    StatisticMeanType = None  # type: ignore[assignment]
    _HAS_MEAN_TYPE = False


def _metadata(stat_id: str, name: str, unit: str, *, has_mean: bool, has_sum: bool) -> StatisticMetaData:
    meta: dict = {
        "has_sum": has_sum,
        "name": f"Nibe Smart Control {name}",
        "source": STAT_DOMAIN,
        "statistic_id": f"{STAT_DOMAIN}:{stat_id}",
        "unit_of_measurement": unit,
        "unit_class": None,
    }
    if _HAS_MEAN_TYPE:
        meta["mean_type"] = StatisticMeanType.ARITHMETIC if has_mean else StatisticMeanType.NONE
    else:
        meta["has_mean"] = has_mean
    return meta  # type: ignore[return-value]


def _bucket_hourly(entries: list[dict], ts_key: str, val_key: str) -> dict[int, list[float]]:
    buckets: dict[int, list[float]] = defaultdict(list)
    for e in entries:
        ts = e.get(ts_key)
        val = e.get(val_key)
        if ts is None or val is None:
            continue
        hour_start = int(ts) - (int(ts) % 3600)
        buckets[hour_start].append(float(val))
    return buckets


def _import_mean_series(hass: HomeAssistant, stat_id: str, name: str, unit: str, buckets: dict[int, list[float]]) -> int:
    if not buckets:
        return 0
    stats: list[StatisticData] = []
    for hour_start in sorted(buckets):
        vals = buckets[hour_start]
        stats.append(
            StatisticData(
                start=dt_util.utc_from_timestamp(hour_start),
                mean=sum(vals) / len(vals),
                min=min(vals),
                max=max(vals),
            )
        )
    async_add_external_statistics(hass, _metadata(stat_id, name, unit, has_mean=True, has_sum=False), stats)
    return len(stats)


def _import_energy_series(hass: HomeAssistant, stat_id: str, name: str, buckets: dict[int, list[float]]) -> int:
    """kW samples bucketed hourly -> cumulative kWh 'sum' series.

    Approximates each hour's energy as mean(kW in that hour) * 1h. Starts the
    cumulative sum at 0 for this run — running the backfill service twice
    over an overlapping window will double-count that overlap, so it's meant
    to be run once after first installing the integration, not repeatedly.
    """
    if not buckets:
        return 0
    stats: list[StatisticData] = []
    running_sum = 0.0
    for hour_start in sorted(buckets):
        vals = buckets[hour_start]
        mean_kw = sum(vals) / len(vals)
        running_sum += mean_kw  # * 1h
        stats.append(
            StatisticData(
                start=dt_util.utc_from_timestamp(hour_start),
                sum=running_sum,
                state=running_sum,
            )
        )
    async_add_external_statistics(hass, _metadata(stat_id, name, "kWh", has_mean=False, has_sum=True), stats)
    return len(stats)


async def async_backfill_history(hass: HomeAssistant, coordinator, days: int) -> None:
    session = async_get_clientsession(hass)
    base = coordinator.base_url
    cutoff = time.time() - days * 86400

    history: list = []
    power: list = []
    try:
        async with session.get(f"{base}{HISTORY_PATH}?n=5000", timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 200:
                history = await r.json()
        async with session.get(f"{base}{POWER_PATH}?hours={days * 24}", timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 200:
                power = await r.json()
    except aiohttp.ClientError as err:
        _LOGGER.error("Backfill: could not reach addon at %s: %s", base, err)
        return

    history = [h for h in history if h.get("ts", 0) >= cutoff]
    power = [p for p in power if p.get("ts", 0) >= cutoff]

    if not history and not power:
        _LOGGER.warning("Backfill: no data in the requested window (%s days) — nothing imported", days)
        return

    n1 = _import_mean_series(hass, "combined_offset_hourly", "Combined offset (hourly)", "°C",
                              _bucket_hourly(history, "ts", "combined"))
    n2 = _import_mean_series(hass, "indoor_temp_hourly", "Indoor temperature (hourly)", "°C",
                              _bucket_hourly(history, "ts", "indoor_temp"))
    n3 = _import_mean_series(hass, "outdoor_temp_hourly", "Outdoor temperature (hourly)", "°C",
                              _bucket_hourly(history, "ts", "outdoor_temp"))
    n4 = _import_mean_series(hass, "electricity_price_hourly", "Electricity price (hourly)", "EUR/kWh",
                              _bucket_hourly(history, "ts", "price_value"))
    n5 = _import_mean_series(hass, "estimated_power_hourly", "Estimated power (hourly)", "kW",
                              _bucket_hourly(power, "ts", "kw"))
    n6 = _import_energy_series(hass, "estimated_energy_backfill", "Estimated energy (backfilled)",
                                _bucket_hourly(power, "ts", "kw"))

    _LOGGER.info(
        "Backfill complete: %s offset, %s indoor, %s outdoor, %s price, %s power, %s energy hourly points imported",
        n1, n2, n3, n4, n5, n6,
    )
