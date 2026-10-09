import logging
from typing import Optional, Dict, Any
from .const import (
    SENSOR_TYPES,
    SOCKET1_SENSOR_TYPES,
    SOCKET2_SENSOR_TYPES,
    SCN_SENSOR_TYPES,
    DOMAIN,
    METER_TYPE,
    METER_STATE_MODES,
    AVAILABILITY_MODES,
    BOOLEAN_EXPLAINED,
    CONTROL_PHASE_MODES,
    ATTR_MANUFACTURER,
)
from homeassistant.const import (
    CONF_NAME,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactiveEnergy,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.components.sensor import (
    SensorStateClass,
    SensorEntity,
    SensorDeviceClass
)

from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

# Device class and state class per unit of measurement.
UNIT_CLASSES = {
    UnitOfElectricCurrent.AMPERE: (SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT),
    UnitOfElectricPotential.VOLT: (SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT),
    UnitOfFrequency.HERTZ: (SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT),
    UnitOfPower.WATT: (SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
    UnitOfApparentPower.VOLT_AMPERE: (SensorDeviceClass.APPARENT_POWER, SensorStateClass.MEASUREMENT),
    UnitOfReactivePower.VOLT_AMPERE_REACTIVE: (SensorDeviceClass.REACTIVE_POWER, SensorStateClass.MEASUREMENT),
    UnitOfTemperature.CELSIUS: (SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
    UnitOfTime.SECONDS: (SensorDeviceClass.DURATION, SensorStateClass.MEASUREMENT),
    # The session energy also falls to 0 when a new session starts; Home
    # Assistant reads that drop as a meter reset.
    UnitOfEnergy.WATT_HOUR: (SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING),
    # The register map does not say if the reactive energy is signed. TOTAL
    # does not read a decrease as a meter reset.
    UnitOfReactiveEnergy.VOLT_AMPERE_REACTIVE_HOUR: (SensorDeviceClass.REACTIVE_ENERGY, SensorStateClass.TOTAL),
    # Home Assistant has no device class for apparent energy.
    "VAh": (None, SensorStateClass.TOTAL_INCREASING),
}

# Power factor has no unit, so the unit cannot identify it.
POWER_FACTOR_KEYS = {
    f"socket_{socket}_power{phase}"
    for socket in (1, 2)
    for phase in ("L1", "L2", "L3", "Sum")
}


def sensor_classes(key, unit):
    """Return (device class, state class) for a sensor.

    Sensors without a unit hold text, flags or static settings, so they get
    no state class: Home Assistant expects a number when a state class is set.
    """
    if key in POWER_FACTOR_KEYS:
        return SensorDeviceClass.POWER_FACTOR, SensorStateClass.MEASUREMENT
    return UNIT_CLASSES.get(unit, (None, None))


async def async_setup_entry(hass, entry, async_add_entities):
    hub_name = entry.data[CONF_NAME]
    hub = hass.data[DOMAIN][hub_name]["hub"]
    
    device_info = {
        "identifiers": {(DOMAIN, hub_name)},
        "name": hub_name,
        "manufacturer": ATTR_MANUFACTURER,
        "model": hub.data.get("platformType", "Unknown"),
        "sw_version": hub.data.get("firmwareVersion", "Unknown"),
    }
    
    entities = []
    for sensor_info in SENSOR_TYPES.values():
        sensor = AlfenSensor(
            hub_name,
            hub,
            device_info,
            sensor_info[0],
            sensor_info[1],
            sensor_info[2],
            sensor_info[3],
        )
        entities.append(sensor)
    
    if hub.read_scn:
        for meter_sensor_info in SCN_SENSOR_TYPES.values():
            sensor = AlfenSensor(
                hub_name,
                hub,
                device_info,
                meter_sensor_info[0],
                meter_sensor_info[1],
                meter_sensor_info[2],
                meter_sensor_info[3],
            )
            entities.append(sensor)
 
    for meter_sensor_info in SOCKET1_SENSOR_TYPES.values():
        sensor = AlfenSensor(
            hub_name,
            hub,
            device_info,
            meter_sensor_info[0],
            meter_sensor_info[1],
            meter_sensor_info[2],
            meter_sensor_info[3],
        )
        entities.append(sensor)
        
    if hub.read_socket_2:
        for meter_sensor_info in SOCKET2_SENSOR_TYPES.values():
            sensor = AlfenSensor(
                hub_name,
                hub,
                device_info,
                meter_sensor_info[0],
                meter_sensor_info[1],
                meter_sensor_info[2],
                meter_sensor_info[3],
            )
            entities.append(sensor)
    async_add_entities(entities)
    return True


class AlfenSensor(SensorEntity):
    """Representation of an Alfen Modbus sensor."""

    def __init__(self, platform_name, hub, device_info, name, key, unit, icon):
        """Initialize the sensor."""
        self._platform_name = platform_name
        self._hub = hub
        self._key = key
        if not hub.has_socket_2:
            if name.startswith("S1 "):
                name = name.replace("S1 ","")
        self._name = name
        self._icon = icon
        self._device_info = device_info
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class, self._attr_state_class = sensor_classes(key, unit)

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self._hub.async_add_alfen_sensor(self._modbus_data_updated)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.async_remove_alfen_sensor(self._modbus_data_updated)

    @callback
    def _modbus_data_updated(self):
        self.async_write_ha_state()

    @property
    def name(self):
        """Return the name."""
        return f"{self._platform_name} {self._name}"

    @property
    def unique_id(self) -> Optional[str]:
        return f"{self._platform_name}_{self._key}"

    @property
    def icon(self):
        """Return the sensor icon."""
        return self._icon

    @property
    def native_value(self):
        """Return the value of the sensor."""
        if self._key in self._hub.data and self._hub.data[self._key] == self._hub.data[self._key]: #check for NaN
            if self._key in ["socket_1_meterType", "socket_2_meterType"] and self._hub.data[self._key] in METER_TYPE:
                return METER_TYPE[self._hub.data[self._key]]
            elif self._key in ["socket_1_meterstate", "socket_2_meterstate"] and self._hub.data[self._key] in METER_STATE_MODES:
                return METER_STATE_MODES[self._hub.data[self._key]]     
            elif self._key in ["socket_1_available", "socket_2_available"] and self._hub.data[self._key] in AVAILABILITY_MODES:
                return AVAILABILITY_MODES[self._hub.data[self._key]]   
            elif self._key in ["backofficeConnected","socket_1_setpointAccounted", "socket_2_setpointAccounted","socket_1_carconnected","socket_2_carconnected","socket_1_carcharging","socket_2_carcharging"] and self._hub.data[self._key] in BOOLEAN_EXPLAINED:
                return BOOLEAN_EXPLAINED[self._hub.data[self._key]]        
            elif self._key in ["socket_1_chargephases", "socket_2_chargephases"] and self._hub.data[self._key] in CONTROL_PHASE_MODES:
                return CONTROL_PHASE_MODES[self._hub.data[self._key]]  
            else:
                return self._hub.data[self._key]           

    @property
    def extra_state_attributes(self):         
        return None

    @property
    def should_poll(self) -> bool:
        """Data is delivered by the hub"""
        return False

    @property
    def device_info(self) -> Optional[Dict[str, Any]]:
        return {
            "identifiers": {(DOMAIN, self._platform_name)},
            "name": self._hub.data.get("name", self._platform_name),
            "manufacturer": ATTR_MANUFACTURER,
            "model": self._hub.data.get("platformType", "Unknown"),
            "sw_version": self._hub.data.get("firmwareVersion", "Unknown"),
        }
