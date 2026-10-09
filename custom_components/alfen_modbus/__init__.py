"""The Alfen Modbus Integration."""
import asyncio
import logging
import operator
from datetime import datetime, timedelta  
from dateutil.tz import tzoffset
from typing import Optional

import voluptuous as vol
from pymodbus.client import ModbusTcpClient

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from .const import (
    DOMAIN,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_MODBUS_ADDRESS,
    CONF_MODBUS_ADDRESS,
    CONF_READ_SCN,
    CONF_READ_SOCKET2,
    CONF_MEASUREMENT_INTERVAL,
    DEFAULT_READ_SCN,
    DEFAULT_READ_SOCKET2,
    VALID_TIME_S,
    MAX_CURRENT_S,
    CONTROL_PHASE_MODES
)

_LOGGER = logging.getLogger(__name__)

ALFEN_MODBUS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_PORT): cv.positive_int,
        vol.Optional(
            CONF_MODBUS_ADDRESS, default=DEFAULT_MODBUS_ADDRESS
        ): cv.positive_int,
        vol.Optional(CONF_READ_SCN, default=DEFAULT_READ_SCN): cv.boolean,
        vol.Optional(CONF_READ_SOCKET2, default=DEFAULT_READ_SOCKET2): cv.boolean,
        vol.Optional(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
        ): cv.positive_int,
        vol.Optional(CONF_MEASUREMENT_INTERVAL): cv.positive_int,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({cv.slug: ALFEN_MODBUS_SCHEMA})}, extra=vol.ALLOW_EXTRA
)

PLATFORMS = ["binary_sensor", "number", "select", "sensor"]


async def async_setup(hass, config):
    """Set up the Alfen modbus component."""
    hass.data[DOMAIN] = {}
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a alfen mobus."""
    host = entry.data[CONF_HOST]
    name = entry.data[CONF_NAME]
    port = entry.data[CONF_PORT]
    address = entry.data.get(CONF_MODBUS_ADDRESS, 1)
    scan_interval = entry.data[CONF_SCAN_INTERVAL]
    # Entries created before this option existed keep polling everything at
    # scan_interval. A measurement interval above scan_interval would make the
    # live values slower than the rest, so it is capped.
    measurement_interval = min(
        entry.data.get(CONF_MEASUREMENT_INTERVAL) or scan_interval, scan_interval
    )
    read_scn = entry.data.get(CONF_READ_SCN, False)
    read_socket2 = entry.data.get(CONF_READ_SOCKET2, False)

    _LOGGER.debug("Setup %s.%s", DOMAIN, name)

    hub = AlfenModbusHub(
        hass,
        name,
        host,
        port,
        address,
        scan_interval,
        read_scn,
        read_socket2,
        measurement_interval,
    )
    """Register the hub."""
    hass.data[DOMAIN][name] = {"hub": hub}

    # Read device info before setting up platforms so device_info is available
    await hass.async_add_executor_job(hub.connect)
    await hub.read_modbus_data()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True



async def async_unload_entry(hass, entry):
    """Unload Alfen mobus entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if not unload_ok:
        return False

    hass.data[DOMAIN].pop(entry.data["name"])
    return True


def validate(value, comparison, against):
    ops = {
        ">": operator.gt,
        "<": operator.lt,
        ">=": operator.ge,
        "<=": operator.le,
        "==": operator.eq,
        "!=": operator.ne,
    }
    if not ops[comparison](value, against):
        raise ValueError(f"Value {value} failed validation ({comparison}{against})")
    return value


class AlfenModbusHub:
    """Async-safe wrapper class for pymodbus."""

    def __init__(
        self,
        hass,
        name,
        host,
        port,
        address,
        scan_interval,
        read_scn=False,
        read_socket_2=False,
        measurement_interval=None,
    ):
        """Initialize the Modbus hub."""
        self._hass = hass
        self._client = ModbusTcpClient(host=host, port=port)
        self._lock = asyncio.Lock()
        self._name = name
        self._address = address
        self.read_scn = read_scn
        self.read_socket_2 = read_socket_2
        measurement_interval = measurement_interval or scan_interval
        # refresh_max_current runs after each measurement read, so its margin is
        # based on the measurement interval.
        self._refreshInterval = measurement_interval
        self._scan_interval = timedelta(seconds=scan_interval)
        self._measurement_interval = timedelta(seconds=measurement_interval)
        self._unsub_interval_method = None
        self._unsub_measurement_method = None
        self._measurement_busy = False
        self._slow_busy = False
        # Identification is static, so it is read at setup only, and again when
        # the charger restarts (for example after a firmware update).
        self._last_uptime_ms = None
        self._identification_pending = False
        self._sensors = []
        self._inputs = []
        self.data = {}

    @callback
    def async_add_alfen_sensor(self, update_callback, refresh_callback = None):
        """Listen for data updates."""
        # This is the first sensor, set up the intervals.
        if not self._sensors:
            self._hass.async_add_executor_job(self.connect)
            self._unsub_interval_method = async_track_time_interval(
                self._hass, self.async_refresh_slow_data, self._scan_interval
            )
            self._unsub_measurement_method = async_track_time_interval(
                self._hass, self.async_refresh_modbus_data, self._measurement_interval
            )
            # Schedule initial data read as a task (non-blocking)
            self._hass.async_create_task(self.read_modbus_data())

        self._sensors.append(update_callback)
        if refresh_callback is not None:
           self._inputs.append(refresh_callback)

    @callback
    def async_remove_alfen_sensor(self, update_callback, refresh_callback = None):
        """Remove data update."""
        self._sensors.remove(update_callback)
        if refresh_callback is not None:
           self._inputs.remove(refresh_callback)
        if not self._sensors:
            """stop the interval timers upon removal of last sensor"""
            self._unsub_interval_method()
            self._unsub_interval_method = None
            self._unsub_measurement_method()
            self._unsub_measurement_method = None
            self._hass.async_add_executor_job(self.close)

    def _notify_sensors(self) -> None:
        for update_callback in self._sensors:
            update_callback()

    async def async_refresh_modbus_data(self, _now: Optional[datetime] = None) -> None:
        """Read the live measurements.

        Called by the measurement timer, and by the number and select entities
        right after a write, so a new setpoint shows up without waiting a cycle.
        """
        if not self._sensors:
            return
        # A timer tick while the previous read still runs is dropped, so reads
        # never pile up at short intervals. A refresh after a write always runs.
        if _now is not None and self._measurement_busy:
            return

        self._measurement_busy = True
        try:
            update_result = await self.read_modbus_data_measurements()
        except Exception:
            _LOGGER.exception("Error reading modbus measurement data")
            update_result = False
        finally:
            self._measurement_busy = False

        if update_result:
            self._notify_sensors()
            self.refresh_max_current()

    async def async_refresh_slow_data(self, _now: Optional[datetime] = None) -> None:
        """Read the registers that change slowly (energy totals, station data)."""
        if not self._sensors or self._slow_busy:
            return

        self._slow_busy = True
        try:
            update_result = await self.read_modbus_data_slow()
        except Exception:
            _LOGGER.exception("Error reading modbus data")
            update_result = False
        finally:
            self._slow_busy = False

        if update_result:
            self._notify_sensors()

    @property
    def name(self):
        """Return the name of this hub."""
        return self._name

    def close(self):
        """Disconnect client."""
        self._client.close()

    def connect(self):
        """Connect client."""
        self._client.connect()

    def _ensure_connected(self):
        """Ensure the modbus client is connected, reconnect if necessary.
        Must be called while holding self._lock."""
        try:
            # Check if socket is open (method may vary by pymodbus version)
            is_open = getattr(self._client, 'is_socket_open', None)
            if is_open and not is_open():
                _LOGGER.debug("Modbus connection lost, reconnecting...")
                try:
                    self._client.close()
                except Exception:
                    pass  # Ignore errors when closing
                self._client.connect()
                # Verify reconnection
                if is_open and not is_open():
                    raise ConnectionError("Failed to reconnect to modbus device")
        except AttributeError:
            # If is_socket_open doesn't exist, just try to connect
            # The actual read/write will catch connection errors
            pass

    @property
    def has_socket_2(self):
        """Return true if a meter is available"""
        return self.read_socket_2

    @property
    def has_scn(self):
        """Return true if a battery is available"""
        return self.read_scn

    def _read_with_connect(self, unit, address, count):
        self._ensure_connected()
        return self._client.read_holding_registers(address=address, count=count, device_id=unit)

    async def read_holding_registers(self, unit, address, count):
        """Read holding registers."""
        try:
            async with self._lock:
                return await self._hass.async_add_executor_job(
                    self._read_with_connect, unit, address, count
                )
        except (BrokenPipeError, ConnectionError, OSError) as e:
            _LOGGER.warning("Connection error during read, attempting reconnect: %s", e)
            # Try to reconnect once
            try:
                async with self._lock:
                    def _reconnect_and_read():
                        try:
                            self._client.close()
                        except Exception:
                            pass
                        self._client.connect()
                        return self._client.read_holding_registers(address=address, count=count, device_id=unit)
                    return await self._hass.async_add_executor_job(_reconnect_and_read)
            except Exception as retry_error:
                _LOGGER.error("Failed to reconnect and retry read: %s", retry_error)
                raise

    def _write_with_connect(self, unit, address, payload):
        self._ensure_connected()
        return self._client.write_registers(address=address, values=payload, device_id=unit)

    async def write_registers(self, unit, address, payload):
        """Write registers."""
        try:
            async with self._lock:
                return await self._hass.async_add_executor_job(
                    self._write_with_connect, unit, address, payload
                )
        except (BrokenPipeError, ConnectionError, OSError) as e:
            _LOGGER.warning("Connection error during write, attempting reconnect: %s", e)
            # Try to reconnect once
            try:
                async with self._lock:
                    def _reconnect_and_write():
                        try:
                            self._client.close()
                        except Exception:
                            pass
                        self._client.connect()
                        return self._client.write_registers(address=address, values=payload, device_id=unit)
                    return await self._hass.async_add_executor_job(_reconnect_and_write)
            except Exception as retry_error:
                _LOGGER.error("Failed to reconnect and retry write: %s", retry_error)
                raise
            
    def refresh_max_current(self):
        # Guard against KeyError if data hasn't been populated yet
        key1 = VALID_TIME_S + "1"
        key2 = VALID_TIME_S + "2"
        if key1 not in self.data:
            return
        if int(self.data[key1]) < self._refreshInterval+10 or (self.has_socket_2 and key2 in self.data and int(self.data[key2]) < self._refreshInterval+10):
            for update_callback in self._inputs:
                # Schedule async callbacks as tasks
                result = update_callback()
                if asyncio.iscoroutine(result):
                    self._hass.async_create_task(result)
            
            

    async def read_modbus_data(self):
        """Read every register group once (setup and first update).

        The slow group comes before the measurements: the session start that
        the measurement read detects needs the energy total and station time.
        """
        return (
            await self.read_modbus_data_product()
            and await self.read_modbus_data_slow()
            and await self.read_modbus_data_measurements()
        )

    async def read_modbus_data_slow(self):
        """Station time and status, SCN, and the per-socket totals."""
        if not await self.read_modbus_data_station_time():
            return False
        # A failed read (the charger may still be booting) is retried next cycle;
        # it does not block the rest of the slow read.
        if self._identification_pending and await self.read_modbus_data_product():
            self._identification_pending = False
        return (
            await self.read_modbus_data_station()
            and await self.read_modbus_data_scn()
            and await self.read_modbus_data_socket_totals(1)
            and await self.read_modbus_data_socket_totals(2)
        )

    async def read_modbus_data_measurements(self):
        """Live meter values and socket status: the load balancing inputs."""
        return (
            await self.read_modbus_data_socket(1)
            and await self.read_modbus_data_socket(2)
        )

    def _socket_enabled(self, socket):
        return socket == 1 or (
            socket == 2 and self.has_socket_2 and self.data.get("numberOfSockets", 0) >= 2
        )

    def decode_string(self, decoder,length):
        s = decoder.decode_string(length*2)  # get 32 char string
        s = s.partition(b"\0")[0]  # omit NULL terminators
        s = s.decode("utf-8")  # decode UTF-8
        return str(s)

    def decode_from_registers(self, registers, offset, count, data_type):
        return self._client.convert_from_registers(registers[offset:offset+count], data_type=data_type, word_order='big')

    async def read_modbus_data_station(self):
        status_data = await self.read_holding_registers(self._address,1100,6)
        if status_data.isError():
            return False
    
        self.data["actualMaxCurrent"] =  round(self.decode_from_registers(status_data.registers,0,2,self._client.DATATYPE.FLOAT32),2)
        self.data["boardTemperature"] =  round(self.decode_from_registers(status_data.registers,2,2,self._client.DATATYPE.FLOAT32),2)
        self.data["backofficeConnected"] = self.decode_from_registers(status_data.registers,4,1,self._client.DATATYPE.UINT16)
        self.data["numberOfSockets"] = self.decode_from_registers(status_data.registers,5,1,self._client.DATATYPE.UINT16)
        return True
        
    async def read_modbus_data_scn(self):
        if(self.has_scn):
            status_data = await self.read_holding_registers(self._address,1400,32)
            if status_data.isError():
                return False

            self.data["scnName"] = self.decode_from_registers(status_data.registers,0,4,self._client.DATATYPE.STRING).strip('\x00')
            self.data["scnSockets"] =  self.decode_from_registers(status_data.registers,4,1,self._client.DATATYPE.UINT16)
            #todo, Smart charging network registers
        return True
        
    async def read_modbus_data_socket(self,socket):
        """Live measurements and status of one socket.

        Registers 300-345 (meter state and age, voltages, currents, power) and
        1200-1215 (mode 3 state, setpoint). The totals in 346-424 are read by
        read_modbus_data_socket_totals at the slower scan interval.
        """
        if not self._socket_enabled(socket):
            return True

        energy_data = await self.read_holding_registers(socket,300,46)
        if energy_data.isError():
            return False

        self.data["socket_"+str(socket)+"_meterstate"] =  self.decode_from_registers(energy_data.registers,0,1,self._client.DATATYPE.UINT16)
        # Age of the charger's own meter reading: one UINT64 in milliseconds.
        self.data["socket_"+str(socket)+"_meterAge"] =  round(self.decode_from_registers(energy_data.registers,1,4,self._client.DATATYPE.UINT64) / 1000, 3)
        self.data["socket_"+str(socket)+"_meterType"] =  self.decode_from_registers(energy_data.registers,5,1,self._client.DATATYPE.UINT16)

        self.data["socket_"+str(socket)+"_VL1-N"] =   round(self.decode_from_registers(energy_data.registers,6,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_VL2-N"] =   round(self.decode_from_registers(energy_data.registers,8,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_VL3-N"] =   round(self.decode_from_registers(energy_data.registers,10,2,self._client.DATATYPE.FLOAT32),2)

        self.data["socket_"+str(socket)+"_VL1-L2"] =  round(self.decode_from_registers(energy_data.registers,12,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_VL2-L3"] =   round(self.decode_from_registers(energy_data.registers,14,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_VL3-L1"] =   round(self.decode_from_registers(energy_data.registers,16,2,self._client.DATATYPE.FLOAT32),2)

        self.data["socket_"+str(socket)+"_currentN"] =   round(self.decode_from_registers(energy_data.registers,18,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_currentL1"] =   round(self.decode_from_registers(energy_data.registers,20,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_currentL2"] =   round(self.decode_from_registers(energy_data.registers,22,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_currentL3"] =  round(self.decode_from_registers(energy_data.registers,24,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_currentSum"] =   round(self.decode_from_registers(energy_data.registers,26,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_powerL1"] =  round(self.decode_from_registers(energy_data.registers,28,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_powerL2"] =   round(self.decode_from_registers(energy_data.registers,30,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_powerL3"] =  round(self.decode_from_registers(energy_data.registers,32,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_powerSum"] =   round(self.decode_from_registers(energy_data.registers,34,2,self._client.DATATYPE.FLOAT32),2)

        self.data["socket_"+str(socket)+"_frequency"] =   round(self.decode_from_registers(energy_data.registers,36,2,self._client.DATATYPE.FLOAT32),2)

        self.data["socket_"+str(socket)+"_realPowerL1"] =   round(self.decode_from_registers(energy_data.registers,38,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_realPowerL2"] =   round(self.decode_from_registers(energy_data.registers,40,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_realPowerL3"] =   round(self.decode_from_registers(energy_data.registers,42,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_realPowerSum"] =   round(self.decode_from_registers(energy_data.registers,44,2,self._client.DATATYPE.FLOAT32),2)

        status_data = await self.read_holding_registers(socket,1200,16)
        if status_data.isError():
            return False

        self.data["socket_"+str(socket)+"_available"] =  self.decode_from_registers(status_data.registers, 0, 1,self._client.DATATYPE.UINT16)
        self.data["socket_"+str(socket)+"_mode3state"] =  self.decode_from_registers(status_data.registers, 1, 5, self._client.DATATYPE.STRING).strip('\x00')
        self.data["socket_"+str(socket)+"_actualMaxCurrent"] =   round(self.decode_from_registers(status_data.registers,6,2,self._client.DATATYPE.FLOAT32),2)
        self.data[VALID_TIME_S+str(socket)] = self.decode_from_registers(status_data.registers, 8, 2,self._client.DATATYPE.UINT32)
        self.data[MAX_CURRENT_S+str(socket)] =  round(self.decode_from_registers(status_data.registers,10,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_saveCurrent"] =  round(self.decode_from_registers(status_data.registers,12,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_setpointAccounted"] =  self.decode_from_registers(status_data.registers, 14, 1,self._client.DATATYPE.UINT16)
        self.data["socket_"+str(socket)+"_chargephases"] =  self.decode_from_registers(status_data.registers, 15, 1,self._client.DATATYPE.UINT16)

        if self.data["socket_"+str(socket)+"_mode3state"] in ["A","E","F"]:
            self.data["socket_"+str(socket)+"_carconnected"] = 0
        else:
            self.data["socket_"+str(socket)+"_carconnected"] = 1

        if self.data["socket_"+str(socket)+"_mode3state"] not in ["C2","D2"]:
            self.data["socket_"+str(socket)+"_carcharging"] = 0
        else:
            if "socket_"+str(socket)+"_carcharging" not in self.data or self.data["socket_"+str(socket)+"_carcharging"] == 0:
                # The totals come from the last slow read. No energy is delivered
                # before charging starts, so that total is the start value; the
                # start time can be up to one scan interval early.
                self.data["socket_"+str(socket)+"_chargingStartWh"] = self.data.get("socket_"+str(socket)+"_realEnergyDeliveredSum")
                self.data["socket_"+str(socket)+"_chargingStart"] = self.data.get("stationTime")
            self.data["socket_"+str(socket)+"_carcharging"] = 1

        self._update_session(socket)

        if self.data["socket_"+str(socket)+"_chargephases"] in CONTROL_PHASE_MODES:
            self.data["usephases_S"+str(socket)] = CONTROL_PHASE_MODES[self.data["socket_"+str(socket)+"_chargephases"]]
        return True

    async def read_modbus_data_socket_totals(self,socket):
        """Apparent and reactive power, and the energy totals (registers 346-424)."""
        if not self._socket_enabled(socket):
            return True

        totals_data = await self.read_holding_registers(socket,346,79)
        if totals_data.isError():
            return False

        # Pad to keep the offsets relative to register 300, as in the register map.
        registers = [0] * 46 + list(totals_data.registers)

        self.data["socket_"+str(socket)+"_apparantPowerL1"] =   round(self.decode_from_registers(registers,46,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_apparantPowerL2"] =   round(self.decode_from_registers(registers,48,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_apparantPowerL3"] =  round(self.decode_from_registers(registers,50,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_apparantPowerSum"] =  round(self.decode_from_registers(registers,52,2,self._client.DATATYPE.FLOAT32),2)

        self.data["socket_"+str(socket)+"_reactivePowerL1"] =   round(self.decode_from_registers(registers,54,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_reactivePowerL2"] =   round(self.decode_from_registers(registers,56,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_reactivePowerL3"] =   round(self.decode_from_registers(registers,58,2,self._client.DATATYPE.FLOAT32),2)
        self.data["socket_"+str(socket)+"_reactivePowerSum"] =   round(self.decode_from_registers(registers,60,2,self._client.DATATYPE.FLOAT32),2)

        self.data["socket_"+str(socket)+"_realEnergyDeliveredL1"] = round(self.decode_from_registers(registers,62,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_realEnergyDeliveredL2"] =   round(self.decode_from_registers(registers,66,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_realEnergyDeliveredL3"] =   round(self.decode_from_registers(registers,70,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_realEnergyDeliveredSum"] =   round(self.decode_from_registers(registers,74,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_realEnergyConsumedL1"] =  round(self.decode_from_registers(registers,78,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_realEnergyConsumedL2"] =   round(self.decode_from_registers(registers,82,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_realEnergyConsumedL3"] =  round(self.decode_from_registers(registers,86,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_realEnergyConsumedSum"] =   round(self.decode_from_registers(registers,90,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_apparantEnergyL1"] =  round(self.decode_from_registers(registers,92,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_apparantEnergyL2"] =   round(self.decode_from_registers(registers,96,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_apparantEnergyL3"] =  round(self.decode_from_registers(registers,100,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_apparantEnergySum"] =  round(self.decode_from_registers(registers,104,4,self._client.DATATYPE.FLOAT64),2)

        self.data["socket_"+str(socket)+"_reactiveEnergyL1"] =   round(self.decode_from_registers(registers,108,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_reactiveEnergyL2"] =   round(self.decode_from_registers(registers,112,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_reactiveEnergyL3"] =  round(self.decode_from_registers(registers,116,4,self._client.DATATYPE.FLOAT64),2)
        self.data["socket_"+str(socket)+"_reactiveEnergySum"] = round(self.decode_from_registers(registers,120,4,self._client.DATATYPE.FLOAT64),2)

        self._update_session(socket)
        return True

    def _update_session(self, socket):
        """Energy and duration of the running charge session, if any."""
        s = "socket_"+str(socket)+"_"
        if self.data.get(s+"carcharging") != 1:
            return
        start_wh = self.data.get(s+"chargingStartWh")
        start = self.data.get(s+"chargingStart")
        if start_wh is None or start is None:
            return
        if s+"realEnergyDeliveredSum" in self.data and "stationTime" in self.data:
            self.data[s+"currentSession"] = self.data[s+"realEnergyDeliveredSum"] - start_wh
            self.data[s+"currentSessionDuration"] = int((self.data["stationTime"] - start).total_seconds())


    async def read_modbus_data_product(self):
        """Product identification (registers 100-167). Static: read at setup."""
        identification_data = await self.read_holding_registers(self._address, 100, 68)
        if identification_data.isError():
            return False

        self.data["name"] = self.decode_from_registers(identification_data.registers, 0, 17, self._client.DATATYPE.STRING).strip('\x00')
        self.data["manufacturer"] = self.decode_from_registers(identification_data.registers, 17, 5, self._client.DATATYPE.STRING).strip('\x00')
        self.data["modbustableVersion"] = self.decode_from_registers(identification_data.registers, 22, 1,self._client.DATATYPE.INT16)
        self.data["firmwareVersion"] = self.decode_from_registers(identification_data.registers, 23, 17, self._client.DATATYPE.STRING).strip('\x00')
        self.data["platformType"] = self.decode_from_registers(identification_data.registers, 40, 17, self._client.DATATYPE.STRING).strip('\x00')
        self.data["serial"] = self.decode_from_registers(identification_data.registers, 57, 11, self._client.DATATYPE.STRING).strip('\x00')
        return True

    async def read_modbus_data_station_time(self):
        """Station clock and uptime (registers 168-178)."""
        time_data = await self.read_holding_registers(self._address, 168, 11)
        if time_data.isError():
            return False

        year    = self.decode_from_registers(time_data.registers, 0, 1,self._client.DATATYPE.INT16)
        month   = self.decode_from_registers(time_data.registers, 1, 1,self._client.DATATYPE.INT16)
        day     = self.decode_from_registers(time_data.registers, 2, 1,self._client.DATATYPE.INT16)
        hour    = self.decode_from_registers(time_data.registers, 3, 1,self._client.DATATYPE.INT16)
        minute  = self.decode_from_registers(time_data.registers, 4, 1,self._client.DATATYPE.INT16)
        second  = self.decode_from_registers(time_data.registers, 5, 1,self._client.DATATYPE.INT16)
        uptime  = self.decode_from_registers(time_data.registers, 6, 4,self._client.DATATYPE.UINT64)
        utcoffset = self.decode_from_registers(time_data.registers, 10, 1,self._client.DATATYPE.INT16)

        # Uptime only goes back on a restart. Unlike lastBoot it does not move
        # when the station clock is corrected.
        if self._last_uptime_ms is not None and uptime < self._last_uptime_ms:
            _LOGGER.info("Charger restarted, reading its identification again")
            self._identification_pending = True
        self._last_uptime_ms = uptime

        # Tijdconversie
        self.data["stationTime"] = datetime(
            year, month, day, hour, minute, second,
            tzinfo=tzoffset("", utcoffset * 60)
        )

        last_boot = self.data["stationTime"] - timedelta(milliseconds=uptime)
        self.data["lastBoot"] = last_boot.replace(microsecond=0)

        return True
