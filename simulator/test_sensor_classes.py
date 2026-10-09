"""
Tests the device class, state class and unit of every AlfenSensor against the
real Home Assistant sensor platform.

Covers:
  - one sensor per unit gets the expected unit, device class and state class
  - every unit in the sensor tables has a device class (except VAh, for which
    Home Assistant has none), valid for that device class
  - every state class is valid for its device class
  - unique IDs keep their format
  - every sensor renders a valid state through SensorEntity.state, with the
    data of a real hub read from the static simulator (both sockets and SCN)

Usage:
    python test_sensor_classes.py         # starts its own simulator on 5023

Requires homeassistant (the minimum version in hacs.json), pymodbus >= 3.11.2
and python-dateutil. Unlike test_hub_polling.py, this test uses the real
homeassistant package and no stubs.
"""
import asyncio
import importlib
import logging
import os
import sys
import types

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor.const import (
    DEVICE_CLASS_STATE_CLASSES,
    DEVICE_CLASS_UNITS,
)
from homeassistant.util.unit_system import METRIC_SYSTEM

from test_hub_polling import FakeHass, Results, start_simulator, wait_for_port

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PORT = 5023
HUB_NAME = "alfen"

log = logging.getLogger("hub_test")

D = SensorDeviceClass
S = SensorStateClass

# key -> (unit, device class, state class). At least one sensor per unit.
EXPECTED = {
    # The sensor from straybiker/HA-EV-Charge-Control issue 4.
    "socket_1_actualMaxCurrent": ("A", D.CURRENT, S.MEASUREMENT),
    "actualMaxCurrent": ("A", D.CURRENT, S.MEASUREMENT),
    "socket_1_VL1-N": ("V", D.VOLTAGE, S.MEASUREMENT),
    "socket_2_VL3-L1": ("V", D.VOLTAGE, S.MEASUREMENT),
    "socket_1_frequency": ("Hz", D.FREQUENCY, S.MEASUREMENT),
    "socket_1_realPowerSum": ("W", D.POWER, S.MEASUREMENT),
    "socket_1_apparentPowerSum": ("VA", D.APPARENT_POWER, S.MEASUREMENT),
    "socket_1_reactivePowerSum": ("var", D.REACTIVE_POWER, S.MEASUREMENT),
    "socket_1_realEnergyDeliveredSum": ("Wh", D.ENERGY, S.TOTAL_INCREASING),
    "socket_1_currentSession": ("Wh", D.ENERGY, S.TOTAL_INCREASING),
    "socket_1_reactiveEnergySum": ("varh", D.REACTIVE_ENERGY, S.TOTAL),
    "socket_1_apparentEnergySum": ("VAh", None, S.TOTAL_INCREASING),
    "socket_1_meterAge": ("s", D.DURATION, S.MEASUREMENT),
    "socket_1_currentSessionDuration": ("s", D.DURATION, S.MEASUREMENT),
    "boardTemperature": ("°C", D.TEMPERATURE, S.MEASUREMENT),
    "socket_1_powerSum": (None, D.POWER_FACTOR, S.MEASUREMENT),
    "socket_2_powerL1": (None, D.POWER_FACTOR, S.MEASUREMENT),
    "socket_1_mode3state": (None, None, None),
    "socket_1_carcharging": (None, None, None),
    "numberOfSockets": (None, None, None),
    "name": (None, None, None),
}


def make_sensors(sensor_mod, const, hub):
    tables = (const.SENSOR_TYPES, const.SCN_SENSOR_TYPES,
              const.SOCKET1_SENSOR_TYPES, const.SOCKET2_SENSOR_TYPES)
    sensors = {}
    for table in tables:
        for name, key, unit, icon in table.values():
            sensors[key] = sensor_mod.AlfenSensor(HUB_NAME, hub, {}, name, key, unit, icon)
    return sensors


def test_classes(sensor_mod, const, results):
    log.info("\n=== Unit, device class and state class per sensor ===")
    hub = types.SimpleNamespace(has_socket_2=True, data={})
    sensors = make_sensors(sensor_mod, const, hub)

    for key, (unit, device_class, state_class) in EXPECTED.items():
        sensor = sensors[key]
        got = (sensor.native_unit_of_measurement, sensor.device_class, sensor.state_class)
        results.check(f"{key}: {unit} -> {device_class}, {state_class}",
                      got == (unit, device_class, state_class), f"got {got}")

    units = {s.native_unit_of_measurement for s in sensors.values()} - {None}
    no_class = sorted(u for u in units if u not in sensor_mod.UNIT_CLASSES)
    results.check("every unit is in the unit table", not no_class, f"missing {no_class}")
    without_device_class = sorted({s.native_unit_of_measurement for s in sensors.values()
                                   if s.native_unit_of_measurement and s.device_class is None})
    results.check("only VAh has no device class", without_device_class == ["VAh"],
                  f"got {without_device_class}")

    bad_unit = [k for k, s in sensors.items()
                if s.device_class and s.native_unit_of_measurement not in DEVICE_CLASS_UNITS[s.device_class]]
    results.check("every unit is valid for its device class", not bad_unit, f"invalid: {bad_unit}")

    bad_state_class = [k for k, s in sensors.items()
                       if s.device_class and s.state_class
                       and s.state_class not in DEVICE_CLASS_STATE_CLASSES[s.device_class]]
    results.check("every state class is valid for its device class", not bad_state_class,
                  f"invalid: {bad_state_class}")

    bad_id = [k for k, s in sensors.items() if s.unique_id != f"{HUB_NAME}_{k}"]
    results.check("unique IDs keep the <hub>_<key> format", not bad_id, f"changed: {bad_id}")


async def test_states(new_mod, sensor_mod, const, results):
    log.info("\n=== States with real hub data (static simulator) ===")
    hass = FakeHass()
    hass.config = types.SimpleNamespace(units=METRIC_SYSTEM)
    hub = new_mod.AlfenModbusHub(hass, HUB_NAME, "127.0.0.1", PORT, 200, 30,
                                 read_scn=True, read_socket_2=True)
    await hass.async_add_executor_job(hub.connect)
    await hub.read_modbus_data()
    hub.close()

    sensors = make_sensors(sensor_mod, const, hub)
    errors, wrong_type, with_value = [], [], 0
    for key, sensor in sensors.items():
        sensor.hass = hass
        sensor.entity_id = f"sensor.{HUB_NAME}_{key}".lower()
        try:
            state = sensor.state
        except ValueError as err:
            errors.append(f"{key}: {err}")
            continue
        if state is None:
            continue
        with_value += 1
        numeric = sensor.state_class is not None or sensor.native_unit_of_measurement is not None
        if numeric and not isinstance(state, (int, float)):
            wrong_type.append(f"{key}={state!r}")
    results.check("every sensor gives a state that Home Assistant accepts", not errors, "; ".join(errors))
    results.check("every sensor with a unit or state class gives a number", not wrong_type,
                  ", ".join(wrong_type))
    log.info(f"        {with_value} of {len(sensors)} sensors have a value")

    duration = sensors["socket_1_currentSessionDuration"]
    results.check("session duration renders as seconds",
                  duration.state == 0 and duration.unit_of_measurement == "s",
                  f"got {duration.state!r} {duration.unit_of_measurement!r}")
    results.check("actual applied max current renders in A",
                  sensors["socket_1_actualMaxCurrent"].state == 16.0
                  and sensors["socket_1_actualMaxCurrent"].unit_of_measurement == "A")


async def main():
    sys.path.insert(0, REPO)
    new_mod = importlib.import_module("custom_components.alfen_modbus")
    sensor_mod = importlib.import_module("custom_components.alfen_modbus.sensor")
    const = importlib.import_module("custom_components.alfen_modbus.const")

    results = Results()
    test_classes(sensor_mod, const, results)

    sim = start_simulator(PORT, True)
    try:
        if not await wait_for_port(PORT):
            log.error("Simulator did not start")
            return 1
        await test_states(new_mod, sensor_mod, const, results)
    finally:
        sim.terminate()

    log.info(f"\n{results.passed} passed, {len(results.failed)} failed")
    for name in results.failed:
        log.info(f"  failed: {name}")
    return 0 if not results.failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
