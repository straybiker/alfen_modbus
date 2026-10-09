"""
Tests the real AlfenModbusHub (custom_components/alfen_modbus) against the
simulator, with the few Home Assistant imports stubbed out.

Covers the split into a measurement interval and a scan interval:
  - decodes are unchanged against the previous release (git tag v1.0.2), except the
    meter reading age, which is now one UINT64 in seconds, and the session
    duration, which is now whole seconds instead of a timedelta
  - which registers each kind of read requests
  - timer rates, the write-triggered refresh, the busy guard and the
    max current refresh
  - identification: read at setup and reload, and again after a charger
    restart (with a retry when that read fails)
  - how async_setup_entry picks the measurement interval, and the config schema

Usage:
    python test_hub_polling.py            # starts its own simulators on 5021/5022

    ALFEN_SIM_PYTHON=<python> python test_hub_polling.py
        runs the simulators with another interpreter, for example to test the
        hub with a pymodbus version that the simulator does not support

Requires pymodbus >= 3.11.2, voluptuous and python-dateutil. Run it from a
checkout that is a git repository with its tags (the comparison loads the
v1.0.2 version).
"""
import asyncio
import datetime as dt
import importlib
import logging
import os
import struct
import subprocess
import sys
import tempfile
import time
import types
from concurrent.futures import ThreadPoolExecutor

import voluptuous as vol
from pymodbus.client import AsyncModbusTcpClient

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BASELINE_REF = "v1.0.2"
STATIC_PORT = 5021
LIVE_PORT = 5022

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("hub_test")
log.setLevel(logging.INFO)


# ============================================================================
# Home Assistant stubs
# ============================================================================

def install_ha_stubs():
    """Register the minimal homeassistant modules the integration imports."""
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []

    const = types.ModuleType("homeassistant.const")
    const.CONF_NAME, const.CONF_HOST = "name", "host"
    const.CONF_PORT, const.CONF_SCAN_INTERVAL = "port", "scan_interval"

    core = types.ModuleType("homeassistant.core")
    core.callback = lambda func: func
    core.HomeAssistant = type("HomeAssistant", (), {})

    entries = types.ModuleType("homeassistant.config_entries")
    entries.ConfigEntry = type("ConfigEntry", (), {})

    class ConfigFlow:
        def __init_subclass__(cls, domain=None, **kwargs):
            super().__init_subclass__(**kwargs)

    entries.ConfigFlow = ConfigFlow
    entries.OptionsFlow = type("OptionsFlow", (), {})
    entries.CONN_CLASS_LOCAL_POLL = "local_poll"
    ha.config_entries = entries

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string, cv.boolean, cv.slug = str, bool, str
    cv.positive_int = vol.All(vol.Coerce(int), vol.Range(min=0))

    event = types.ModuleType("homeassistant.helpers.event")

    def async_track_time_interval(hass, action, interval):
        # Like HA: each tick runs as its own task, so a slow read can overlap the
        # next tick. That is what the busy guard in the hub is for.
        async def ticker():
            while True:
                await asyncio.sleep(interval.total_seconds())
                asyncio.get_running_loop().create_task(action(dt.datetime.now()))

        task = asyncio.get_running_loop().create_task(ticker())
        return task.cancel

    event.async_track_time_interval = async_track_time_interval

    sys.modules.update({
        "homeassistant": ha,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.config_entries": entries,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.config_validation": cv,
        "homeassistant.helpers.event": event,
    })


class FakeHass:
    """Just enough of hass for the hub: executor jobs, tasks, entry setup."""

    def __init__(self):
        self.data = {}
        self._executor = ThreadPoolExecutor(4)
        self.config_entries = types.SimpleNamespace(
            async_forward_entry_setups=self._forward,
            async_forward_entry_unload=self._unload,
        )

    async def _forward(self, entry, platforms):
        return True

    async def _unload(self, entry, platform):
        return True

    def async_add_executor_job(self, func, *args):
        return asyncio.get_running_loop().run_in_executor(self._executor, func, *args)

    def async_create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)


# ============================================================================
# Helpers
# ============================================================================

class Results:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, name, condition, detail=""):
        if condition:
            self.passed += 1
            log.info(f"  PASS  {name}")
        else:
            self.failed.append(name)
            log.error(f"  FAIL  {name} {detail}")


def load_old_package():
    """Load the BASELINE_REF version of the integration as package 'alfen_modbus_old'."""
    root = tempfile.mkdtemp(prefix="alfen_old_")
    pkg = os.path.join(root, "alfen_modbus_old")
    os.makedirs(pkg)
    for name in ("__init__.py", "const.py"):
        source = subprocess.run(
            ["git", "-C", REPO, "show", f"{BASELINE_REF}:custom_components/alfen_modbus/{name}"],
            capture_output=True, check=True,
        ).stdout
        with open(os.path.join(pkg, name), "wb") as f:
            f.write(source)
    sys.path.insert(0, root)
    return importlib.import_module("alfen_modbus_old")


def record_reads(hub):
    """Wrap hub.read_holding_registers to log (unit, address, count) per call."""
    calls = []
    original = hub.read_holding_registers

    async def wrapper(unit, address, count):
        calls.append((unit, address, count))
        return await original(unit, address, count)

    hub.read_holding_registers = wrapper
    return calls


def float_registers(value):
    raw = struct.pack(">f", float(value))
    return [int.from_bytes(raw[0:2], "big"), int.from_bytes(raw[2:4], "big")]


def start_simulator(port, static):
    # ALFEN_SIM_PYTHON runs the simulator under another interpreter, so the hub
    # can be tested with a pymodbus version the simulator does not support.
    python = os.environ.get("ALFEN_SIM_PYTHON", sys.executable)
    args = [python, os.path.join(HERE, "simulator.py"), "-p", str(port)]
    if static:
        args.append("--static")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


async def wait_for_port(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        client = AsyncModbusTcpClient("127.0.0.1", port=port)
        await client.connect()
        if client.connected:
            client.close()
            return True
        await asyncio.sleep(0.5)
    return False


async def sim_write(port, unit, address, registers):
    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    await client.connect()
    await client.write_registers(address, registers, device_id=unit)
    client.close()


# ============================================================================
# Tests
# ============================================================================

async def test_decode_regression(new_mod, old_mod, hass, results):
    log.info("\n=== Decodes against the previous release (static simulator) ===")
    old_hub = old_mod.AlfenModbusHub(hass, "alfen", "127.0.0.1", STATIC_PORT, 200, 30)
    new_hub = new_mod.AlfenModbusHub(hass, "alfen", "127.0.0.1", STATIC_PORT, 200, 30)
    for hub in (old_hub, new_hub):
        await hass.async_add_executor_job(hub.connect)
        await hub.read_modbus_data()
        hub.close()

    only_old = sorted(set(old_hub.data) - set(new_hub.data))
    only_new = sorted(set(new_hub.data) - set(old_hub.data))
    differ = sorted(k for k in set(old_hub.data) & set(new_hub.data) if old_hub.data[k] != new_hub.data[k])
    results.check("same set of data keys", not only_old and not only_new, f"old-only={only_old} new-only={only_new}")
    results.check("only the meter reading age and session duration differ",
                  differ == ["socket_1_currentSessionDuration", "socket_1_meterAge"], f"differ={differ}")
    log.info(f"        {len(old_hub.data)} keys compared")
    log.info(f"        meter age: old {old_hub.data.get('socket_1_meterAge')!r} -> new {new_hub.data.get('socket_1_meterAge')!r}")
    results.check("meter age is one value in seconds (500 ms -> 0.5)", new_hub.data.get("socket_1_meterAge") == 0.5)
    old_duration = old_hub.data.get("socket_1_currentSessionDuration")
    new_duration = new_hub.data.get("socket_1_currentSessionDuration")
    results.check("session duration is whole seconds, not a timedelta",
                  isinstance(new_duration, int) and new_duration == int(old_duration.total_seconds()),
                  f"old {old_duration!r} -> new {new_duration!r}")


async def test_request_plan(new_mod, hass, results):
    log.info("\n=== Registers requested per read ===")
    hub = new_mod.AlfenModbusHub(hass, "alfen", "127.0.0.1", STATIC_PORT, 200, 30, measurement_interval=2)
    await hass.async_add_executor_job(hub.connect)
    calls = record_reads(hub)

    await hub.read_modbus_data()
    full = list(calls)
    calls.clear()
    await hub.read_modbus_data_measurements()
    measurement = list(calls)
    calls.clear()
    await hub.read_modbus_data_slow()
    slow = list(calls)
    hub.close()

    results.check("full read: identification once, then slow, then measurements",
                  full == [(200, 100, 68), (200, 168, 11), (200, 1100, 6), (1, 346, 79), (1, 300, 46), (1, 1200, 16)],
                  f"got {full}")
    results.check("measurement read: 300-345 and 1200-1215 only", measurement == [(1, 300, 46), (1, 1200, 16)], f"got {measurement}")
    results.check("slow read: clock, station, totals (no identification)",
                  slow == [(200, 168, 11), (200, 1100, 6), (1, 346, 79)], f"got {slow}")
    log.info(f"        measurement read: {sum(c for _, _, c in measurement)} registers in {len(measurement)} requests "
             f"(previous release: 226 in 5 every scan)")


async def test_timers_and_writes(new_mod, hass, results):
    log.info("\n=== Timers, write refresh, busy guard (live simulator) ===")
    hub = new_mod.AlfenModbusHub(hass, "alfen", "127.0.0.1", LIVE_PORT, 200, 4, measurement_interval=1)
    await hass.async_add_executor_job(hub.connect)
    calls = record_reads(hub)
    notified = []
    max_current_refreshes = []
    hub.async_add_alfen_sensor(lambda: notified.append(time.time()), lambda: max_current_refreshes.append(time.time()))

    ages = []
    start = time.time()
    while time.time() - start < 12:
        await asyncio.sleep(0.25)
        if "socket_1_meterAge" in hub.data:
            ages.append(hub.data["socket_1_meterAge"])
    count = lambda address: sum(1 for _, a, _ in calls if a == address)
    log.info(f"        12 s: measurement reads {count(300)}, slow reads {count(346)}, identification reads {count(100)}")
    results.check("measurement timer ~1 s (10-14 reads in 12 s)", 10 <= count(300) <= 14, f"got {count(300)}")
    results.check("scan timer ~4 s (3-5 reads in 12 s, incl. the first full read)", 3 <= count(346) <= 5, f"got {count(346)}")
    results.check("identification read once", count(100) == 1, f"got {count(100)}")
    results.check("meter age always under 1 s and changing", ages and max(ages) < 1 and len(set(ages)) > 3,
                  f"values {sorted(set(ages))[:6]}")

    # A write is followed by a measurement-only refresh. Stop the scan timer
    # first: its own reads would otherwise land in this check at random.
    hub._unsub_interval_method()
    hub._unsub_interval_method = lambda: None
    await asyncio.sleep(0.5)
    calls.clear()
    payload = float_registers(10.0)
    await hub.write_registers(unit=1, address=1210, payload=payload)
    await hub.async_refresh_modbus_data()
    write_reads = [a for _, a, _ in calls]
    results.check("refresh after a write reads only the measurement registers",
                  set(write_reads) <= {300, 1200} and write_reads, f"got {write_reads}")

    # The new setpoint reaches the power reading within ~2 measurement cycles.
    target = 10 * 230 * 0.98 * 3
    t0 = time.time()
    while time.time() - t0 < 5 and abs(hub.data.get("socket_1_realPowerSum", 0) - target) > 1:
        await asyncio.sleep(0.1)
    latency = time.time() - t0
    results.check("new power visible within 3 s of the write", latency < 3, f"took {latency:.1f}s")
    log.info(f"        power {hub.data.get('socket_1_realPowerSum')} W, {latency:.1f} s after writing 10 A")

    # Busy guard: a timer tick is dropped while a read runs; a write refresh is not.
    calls.clear()
    hub._measurement_busy = True
    await hub.async_refresh_modbus_data(dt.datetime.now())
    dropped = len(calls)
    await hub.async_refresh_modbus_data()
    after_write = len(calls)
    hub._measurement_busy = False
    results.check("timer tick dropped while busy", dropped == 0, f"{dropped} reads")
    results.check("write refresh runs while busy", after_write > 0)

    # Max current refresh: quiet while the setpoint is valid, triggered near expiry.
    before = len(max_current_refreshes)
    await asyncio.sleep(2.5)
    results.check("no max current refresh while valid time is 60 s", len(max_current_refreshes) == before)
    await sim_write(LIVE_PORT, 1, 1208, [0, 5])  # valid time 5 s < 1 s + 10 s margin
    await asyncio.sleep(2.5)
    results.check("max current refresh when valid time drops to 5 s", len(max_current_refreshes) > before)
    await sim_write(LIVE_PORT, 1, 1208, [0, 60])

    results.check("entities notified after reads", len(notified) >= 10, f"{len(notified)} notifications")
    for unsub in (hub._unsub_interval_method, hub._unsub_measurement_method):
        unsub()
    hub.close()


async def test_setup_entry_intervals(new_mod, hass, results):
    log.info("\n=== async_setup_entry: measurement interval choice ===")
    await new_mod.async_setup(hass, {})
    cases = [
        ("no option (existing entry)", {}, 20),
        ("measurement 2 s", {"measurement_interval": 2}, 2),
        ("measurement above scan is capped", {"measurement_interval": 60}, 20),
    ]
    for label, extra, expected in cases:
        name = "alfen_" + str(len(hass.data[new_mod.DOMAIN]))
        entry = types.SimpleNamespace(data={"host": "127.0.0.1", "name": name, "port": STATIC_PORT,
                                            "modbus_address": 200, "scan_interval": 20, **extra})
        await new_mod.async_setup_entry(hass, entry)
        hub = hass.data[new_mod.DOMAIN][name]["hub"]
        got = hub._measurement_interval.total_seconds()
        results.check(f"{label}: {expected} s", got == expected, f"got {got}")
        results.check(f"{label}: scan stays 20 s", hub._scan_interval.total_seconds() == 20)
        hub.close()


async def test_identification_on_setup_and_reload(new_mod, hass, results):
    log.info("\n=== Identification at HA startup and integration reload ===")
    # Record at class level: async_setup_entry creates the hub itself.
    calls = []
    original = new_mod.AlfenModbusHub.read_holding_registers

    async def recording(self, unit, address, count):
        calls.append((unit, address, count))
        return await original(self, unit, address, count)

    new_mod.AlfenModbusHub.read_holding_registers = recording
    try:
        await new_mod.async_setup(hass, {})
        entry = types.SimpleNamespace(data={"host": "127.0.0.1", "name": "alfen_reload", "port": STATIC_PORT,
                                            "modbus_address": 200, "scan_interval": 20,
                                            "measurement_interval": 2})
        # HA startup: the entry is set up.
        await new_mod.async_setup_entry(hass, entry)
        first_hub = hass.data[new_mod.DOMAIN]["alfen_reload"]["hub"]
        after_startup = calls.count((200, 100, 68))
        # Integration reload (also after an options change): unload, then set up again.
        await new_mod.async_unload_entry(hass, entry)
        first_hub.close()
        hass.data[new_mod.DOMAIN].pop("alfen_reload", None)
        first_hub.data.clear()
        await new_mod.async_setup_entry(hass, entry)
        second_hub = hass.data[new_mod.DOMAIN]["alfen_reload"]["hub"]
        after_reload = calls.count((200, 100, 68))
        second_hub.close()
    finally:
        new_mod.AlfenModbusHub.read_holding_registers = original

    results.check("identification read at startup", after_startup == 1, f"got {after_startup}")
    results.check("identification read again at reload", after_reload == 2, f"got {after_reload}")
    results.check("reload builds a new hub with the identification filled in",
                  second_hub is not first_hub and second_hub.data.get("serial") and second_hub.data.get("firmwareVersion"),
                  f"serial={second_hub.data.get('serial')!r} firmware={second_hub.data.get('firmwareVersion')!r}")


def string_registers(text, length_bytes):
    raw = text.encode("utf-8")[:length_bytes].ljust(length_bytes, b"\x00")
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, length_bytes, 2)]


def uint64_registers(value):
    raw = struct.pack(">Q", int(value))
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, 8, 2)]


async def test_identification_after_charger_restart(new_mod, hass, results):
    log.info("\n=== Identification after a charger restart (uptime goes back) ===")
    hub = new_mod.AlfenModbusHub(hass, "alfen", "127.0.0.1", STATIC_PORT, 200, 30, measurement_interval=2)
    await hass.async_add_executor_job(hub.connect)
    await hub.read_modbus_data()
    old_firmware = hub.data["firmwareVersion"]
    calls = record_reads(hub)
    ident = lambda: calls.count((200, 100, 68))
    try:
        await hub.read_modbus_data_slow()
        results.check("no identification read while the charger keeps running", ident() == 0, f"got {ident()}")

        # Restart with new firmware: uptime drops, the firmware string changes.
        await sim_write(STATIC_PORT, 200, 123, string_registers("6.4.0-4210", 34))
        await sim_write(STATIC_PORT, 200, 174, uint64_registers(1000))
        await hub.read_modbus_data_slow()
        results.check("identification read once after the restart", ident() == 1, f"got {ident()}")
        results.check("new firmware version picked up", hub.data["firmwareVersion"] == "6.4.0-4210",
                      f"{old_firmware!r} -> {hub.data['firmwareVersion']!r}")
        await hub.read_modbus_data_slow()
        results.check("not read again on the next cycle", ident() == 1, f"got {ident()}")

        # Restart where the first identification read fails: retried, and the
        # rest of the slow read still completes.
        await sim_write(STATIC_PORT, 200, 174, uint64_registers(500))
        real_read = hub.read_holding_registers
        failed_once = []

        async def flaky(unit, address, count):
            if address == 100 and not failed_once:
                failed_once.append(True)
                calls.append((unit, address, count))
                return types.SimpleNamespace(isError=lambda: True)
            return await real_read(unit, address, count)

        hub.read_holding_registers = flaky
        slow_ok = await hub.read_modbus_data_slow()
        results.check("failed identification does not fail the slow read", slow_ok is True)
        results.check("identification still pending after the failure", hub._identification_pending is True)
        await hub.read_modbus_data_slow()
        results.check("identification retried and done on the next cycle",
                      hub._identification_pending is False and ident() == 3, f"reads {ident()}")
    finally:
        await sim_write(STATIC_PORT, 200, 123, string_registers("5.16.0-4095", 34))
        await sim_write(STATIC_PORT, 200, 174, uint64_registers(3600000))
        hub.close()


def test_config_schema(results):
    log.info("\n=== Config flow schema ===")
    flow = importlib.import_module("custom_components.alfen_modbus.config_flow")
    data = flow.DATA_SCHEMA({"host": "192.0.2.1"})
    results.check("new entry defaults measurement interval to the scan interval (30)",
                  data.get("measurement_interval") == 30 == data.get("scan_interval"), f"got {data}")
    try:
        flow.DATA_SCHEMA({"host": "192.0.2.1", "measurement_interval": 0})
        rejected = False
    except vol.Invalid:
        rejected = True
    results.check("measurement interval 0 is rejected", rejected)


async def main():
    install_ha_stubs()
    sys.path.insert(0, REPO)
    new_mod = importlib.import_module("custom_components.alfen_modbus")
    old_mod = load_old_package()

    sims = [start_simulator(STATIC_PORT, True), start_simulator(LIVE_PORT, False)]
    results = Results()
    try:
        ready = await wait_for_port(STATIC_PORT) and await wait_for_port(LIVE_PORT)
        if not ready:
            log.error("Simulators did not start")
            return 1
        hass = FakeHass()
        await test_decode_regression(new_mod, old_mod, hass, results)
        await test_request_plan(new_mod, hass, results)
        await test_timers_and_writes(new_mod, hass, results)
        await test_setup_entry_intervals(new_mod, hass, results)
        await test_identification_on_setup_and_reload(new_mod, hass, results)
        await test_identification_after_charger_restart(new_mod, hass, results)
        test_config_schema(results)
    finally:
        for proc in sims:
            proc.terminate()

    log.info(f"\n{results.passed} passed, {len(results.failed)} failed")
    for name in results.failed:
        log.info(f"  failed: {name}")
    return 0 if not results.failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
