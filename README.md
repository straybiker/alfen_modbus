# Alfen Modbus for Home Assistant

[![HACS Default](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/straybiker/alfen_modbus)](https://github.com/straybiker/alfen_modbus/releases)
[![License](https://img.shields.io/github/license/straybiker/alfen_modbus)](LICENSE)

Home Assistant integration for **Alfen Eve NG9xx** series EV chargers via Modbus TCP.

![Demo](demo.png)

## Features

- 🔌 **Real-time monitoring** - Voltage, current, power, energy for all phases
- 🚗 **Car status detection** - Connected, charging, disconnected states
- ⚡ **Load balancing control** - Set maximum charging current dynamically
- 🛡️ **Max current protection** - Prevents setting current above station limit
- 📊 **Session tracking** - Energy consumed and duration per charging session
- 🔄 **Auto-renew max current** - Prevents timeout to safe current mode
- 🏢 **Multi-socket support** - Works with dual socket chargers
- 🌐 **SCN support** - Smart Charging Network (partial)

## Requirements

- Home Assistant **2025.10.0** or newer (needs pymodbus 3.11.2 or newer, which Home Assistant ships from that release)
- Alfen Eve NG9xx charger with:
  - Firmware **4.2.0** or newer (Modbus TCP support)
  - Firmware **6.4.0+** recommended (fixes power budget reset bug)
  - **Active Load Balancing** license enabled
- Modbus TCP enabled on the charger

## Installation

### HACS (Recommended)

Note: this is install the source version, not this branch.

1. Open HACS in Home Assistant
2. Search for "Alfen Modbus"
3. Click Install
4. Restart Home Assistant

### Installing a custom repository in Home Assistant

1. Open the Home Assistant UI.
2. Go to "Settings" > "Integrations".
3. Click on the three dots in the top right corner and select "Add custom repository".
4. Enter the repository URL: `https://github.com/straybiker/alfen_modbus` and select the category (e.g., "Integration").
5. Click "Add" to save the repository.
6. Search for "Alfen Modbus" in the integrations list and follow the prompts to install.

> Note: Ensure that you restart Home Assistant after adding the repository for the changes to take effect.

### Manual

1. Copy `custom_components/alfen_modbus` to your `config/custom_components/` folder
2. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for **Alfen Modbus**
4. Enter your charger's IP address and port (default: 502)

### Polling intervals

| Option | Reads | Default |
|--------|-------|---------|
| **Scan Interval** | Energy totals, apparent/reactive power, station data and clock | 30 s |
| **Measurement Interval** | Power, current, voltage, meter state, mode 3 state and setpoint | Scan interval |

For load balancing, set the **Measurement Interval** to 2-5 s. A load balancer that
subtracts the charger power from a grid meter otherwise works with a charger value
up to one scan interval old, and sees phantom headroom after every current change.
A measurement read is 62 registers in 2 requests per socket. The product
identification is read at setup (HA startup and integration reload) and again
when the charger restarts. Both options can be changed later under
**Configure**.

## Enabling Modbus on Alfen Charger

1. Acquire the **Active Load Balancing** license from Alfen
2. Enable **Active Load Balancing** via the Alfen Service Installer app
3. Set **Data Source** to "Energy Management System" for slave mode

See the [Alfen Smart Charging Manual](https://knowledge.alfen.com/space/IN/639762449) for details.

## Sensors

| Category | Sensors |
|----------|---------|
| **Device** | Name, Manufacturer, Serial, Firmware, Platform |
| **Station** | Max Current, Temperature, Backoffice Connection |
| **Socket** | Voltages (L1-N, L2-N, L3-N, L1-L2, L2-L3, L3-L1) |
| | Currents (L1, L2, L3, N, Sum) |
| | Power (Real, Apparent, Reactive per phase + Sum) |
| | Energy (Delivered, Consumed per phase + Sum) |
| | Mode 3 State, Availability, Charging Phases |
| **Derived** | Car Connected, Car Charging, Session Wh, Session Duration |

## Controls

| Control | Description |
|---------|-------------|
| **Max Current** | Set the maximum charging current (load balancing) |
| **Phase Mode** | Select 1-phase or 3-phase charging |

## Known Issues

- Power budget may reset to 0A when no car is connected (fixed in firmware [6.4.0-4210](https://knowledge.alfen.com/space/IN/243466257))
- **Reallin power meter (post-2021)**: Chargers with a Reallin power meter produced after 2021 only export a subset of measurement values. Per-phase energy, apparent energy, and reactive energy sensors will show as "unavailable" (NaN). This is a hardware limitation, not a bug.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Changelog

### v1.2.0 (beta)

- **Device classes** - Every sensor with a unit now has a Home Assistant device class (current, voltage, frequency, power, apparent power, reactive power, energy, reactive energy, duration, temperature), and the power factor sensors have the power factor class. Entity selectors that filter on device class now list these sensors. Apparent energy (VAh) has no device class, because Home Assistant has none for it
- **Reactive units** - Reactive power is now in `var` (was `VAr`) and reactive energy in `varh` (was `VAh`, which was wrong). Home Assistant flags the unit change for existing statistics: fix it in **Developer tools > Statistics**
- **Apparent power and energy** - These sensors now show values (they were always unknown, because the hub stored the values under a misspelled key)
- **Session duration** - Now a number of seconds (it showed a time such as `0:12:34` with unit `s`)
- **State classes** - Energy counters are totals; text, on/off and settings sensors (name, firmware, mode 3 state, number of sockets, ...) no longer have a state class and no longer go into long-term statistics

### v1.1.0

- **Measurement interval** - Power, current, voltage and charger state can be polled faster than the energy totals, for load balancing
- **Lighter polling** - Product identification is read at setup and when the charger restarts (for example after a firmware update) instead of every poll
- **Meter reading age** - Now decoded as one 64-bit value and shown in seconds (it showed a list of four numbers)
- **No read backlog** - A timer tick is skipped while the previous read still runs

### v1.0.0

- **Stable release** - First stable release for HACS
- **Binary sensors** - Added `car_connected` and `car_charging` binary sensors
- **Improved config flow** - Enhanced UI with descriptions and connection testing
- **Options flow** - Reconfigure host, port, and settings after setup
- **pymodbus 3.11 compatibility** - Updated API calls for latest pymodbus

### v0.2.0

- **Max current protection** - The max current slider now dynamically limits to the station's actual max current (Register 1100), preventing values higher than the hardware allows
- **pymodbus 3.11 compatibility** - Updated API calls to use `device_id` parameter (replaces deprecated `slave`)

### v0.1.9

- Initial release

## License

This project is licensed under the Apache 2.0 License - see the [LICENSE](LICENSE) file for details.
