DOMAIN = "thermal_efficiency"

CONF_GAS_METER = "gas_meter"
CONF_OUTDOOR = "outdoor"
CONF_LOFT = "loft"
CONF_LOFT_SINCE = "loft_since"
CONF_LOFT_HUMIDITY = "loft_humidity"
CONF_FLOOR_AREA = "floor_area_m2"
CONF_ROOMS = "rooms"
CONF_TEMPERATURE = "temperature"
CONF_HEATING_POWER = "heating_power"
CONF_MAX_WINDOW_DAYS = "max_window_days"
CONF_CO2 = "co2"
CONF_CEILING_HEIGHT = "ceiling_height_m"
CONF_WATER = "water"
CONF_GAS_UNIT_RATE = "gas_unit_rate"
CONF_BOILER_EFFICIENCY = "boiler_efficiency"

DEFAULT_MAX_WINDOW_DAYS = 365
DEFAULT_BOILER_EFFICIENCY = 0.88
# Windows tried in order until enough usable data is found.
EXPANDING_WINDOWS_DAYS = (60, 120, 365)
UPDATE_INTERVAL_HOURS = 6
