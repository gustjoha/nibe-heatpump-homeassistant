"""Constants for the Nibe Smart Control Statistics integration."""
from datetime import timedelta

DOMAIN = "nibe_smart_control_stats"

CONF_HOST = "host"
CONF_PORT = "port"

DEFAULT_PORT = 8099

# The addon computes/writes at most once a minute; polling every 60s keeps
# HA's own state history (and any state_class entities' short-term stats)
# aligned with what actually changed, without hammering the addon.
UPDATE_INTERVAL = timedelta(seconds=60)

API_PATH = "/api/hastats"
HISTORY_PATH = "/api/history"
POWER_PATH = "/api/power"

# statistic_id namespace used for backfilled EXTERNAL statistics
# (recorder requires the "domain:name" form for external, i.e. non-entity,
# statistics — see homeassistant.components.recorder.statistics)
STAT_DOMAIN = "nibe_smart_control_stats"

SERVICE_BACKFILL = "backfill_history"
