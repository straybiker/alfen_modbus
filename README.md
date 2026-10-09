# Alfen Modbus for Home Assistant

[![HACS Default](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/thastealth/alfen_modbus)](https://github.com/thastealth/alfen_modbus/releases)
[![License](https://img.shields.io/github/license/thastealth/alfen_modbus)](LICENSE)

Home Assistant integration for **Alfen Eve NG9xx** and **AHP** series EV chargers via Modbus TCP.

![Demo](demo.png)

## Features

- 🔌 **Real-time monitoring** - Voltage, current, power, energy for all phases
- 🚗 **Car status detection** - Connected, charging, disconnected states
- ⚡ **Load balancing control** - Set maximum charging current dynamically
- 🛡️ **Max current protection** - Prevents setting current above station limit
- 📊 **Session tracking** - Energy consumed and duration per charging session
- 🔄 **Auto-renew max current** - Prevents timeout to safe current mode
- 🏢 **Multi-socket support** - Works with dual socket chargers
- 🌐 **SCN support** - Smart Charging Network: per-phase consumption, actual/max/safe current and enable state, with a control to set the max current per phase
- 🗣️ **Localized UI** - Entity names and states are translated into English, Dutch, German, French, Finnish, Norwegian, Swedish and Danish, following your Home Assistant language setting
- 🩺 **Diagnostics** - Download a redacted diagnostics report from Settings → Devices & Services for bug reports
- 🚑 **Repair notifications** - Flags known issues (e.g. outdated NG9xx firmware) directly in Settings → Repairs

## Requirements

- Home Assistant **2025.10.0** or newer (needs pymodbus 3.11.2 or newer, which Home Assistant ships from that release)
- A supported Alfen charger (see [Supported Platforms](#supported-platforms)) with:
  - The minimum firmware version for your platform
  - **Active Load Balancing** license enabled
- Modbus TCP enabled on the charger

## Supported Platforms

| Platform | Minimum firmware | Notes |
|----------|-------------------|-------|
| Eve NG9xx | **4.2.0** (adds Modbus TCP support); **6.4.0+** recommended | Fixes a power budget reset bug — see Known Issues |
| AHP (e.g. AHP02-60227) | **2.6.0** ([release notes](https://knowledge.alfen.com/categories/CAT-01021/KA-01635)) — currently the latest AHP firmware | Requires integration **v0.2.1+**; earlier versions fail every poll cycle because AHP rejects Modbus reads that don't align to register value boundaries ([#40](https://github.com/ThaStealth/alfen_modbus/issues/40)) |

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

## Localization

Entity names and on/off states follow the language set in your Home Assistant profile. Currently translated: 🇬🇧 English, 🇳🇱 Dutch, 🇩🇪 German, 🇫🇷 French, 🇫🇮 Finnish, 🇳🇴 Norwegian, 🇸🇪 Swedish and 🇩🇰 Danish. Any other language falls back to English.

Per-socket entities (voltages, currents, power, energy, ...) only show a socket number in the name (e.g. "Socket 2 voltage L1-N") when **Read Socket 2** is enabled; single-socket setups get a plain name instead.

Missing your language or spotted an incorrect translation? Pull requests adding or fixing a `custom_components/alfen_modbus/translations/<lang>.json` file are very welcome.

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
| **Derived** | Car Connected, Car Charging, Charger Enabled, Session Wh, Session Duration |
| **SCN** (if enabled) | Total Consumption, Actual Max Current and Safe Current per phase, Max Current Enabled |

## Controls

| Control | Description |
|---------|-------------|
| **Max Current** | Set the maximum charging current (load balancing) |
| **Phase Mode** | Select 1-phase (`"1"`) or 3-phase (`"3"`) charging |
| **Charger Enabled** | Switch that stops (0 A) or resumes (allowed max current) charging via the max-current setpoint |
| **SCN Max Current per phase** (if enabled) | Set the maximum current for the Smart Charging Network as a whole, per phase |

## EVCC Integration

[EVCC](https://evcc.io)'s generic Home Assistant charger lets you map its fields directly to entities from this integration. See [docs/evcc.md](docs/evcc.md) for a step-by-step walkthrough of EVCC's configuration wizard.

### Field mapping

| EVCC field | Entity | Notes |
|---|---|---|
| Charging status sensor | `sensor.alfen_mode_3_state` | Reports the raw IEC 61851 Mode 3 State (`A`, `B1`, `B2`, `C1`, `C2`, `D1`, `D2`, `E`, `F`). Add the state overrides below so EVCC maps it to its A/B/C. |
| States for status A | `E,F` | Error / EVSE-disabled states, grouped with "ready" the same way this integration's Car Connected sensor does. |
| States for status B | `B1,B2,C1,D1` | Connected but not drawing current. |
| States for status C | `C2,D2` | Actively charging (PWM applied). |
| Enabled status sensor | `binary_sensor.alfen_charger_enabled` | On when the max-current setpoint is above 0 A. |
| Enable switch | `switch.alfen_charger_enabled` | Off writes 0 A to the max-current register; on restores the station's currently allowed max current. |
| Maximum current entity | `number.alfen_max_current_limit_s1` | Must be this writable number entity, not the read-only `sensor.alfen_actual_max_current`. |
| Power entity | `sensor.alfen_real_power_sum` | |
| Energy entity | `sensor.alfen_real_energy_delivered_sum` | Reported in Wh; add a template sensor to convert to kWh if EVCC doesn't scale it for you. |
| L1 / L2 / L3 current entity | `sensor.alfen_current_l1` / `_l2` / `_l3` | |
| L1 / L2 / L3 voltage entity | `sensor.alfen_voltage_l1_n` / `_l2_n` / `_l3_n` | |
| Phase switching entity | `select.alfen_usable_phases` | Options are the literal values `"1"` and `"3"`. |

With **Read Socket 2** enabled, socket 2's entity IDs get a `_socket_2` suffix (e.g. `sensor.alfen_current_l1_socket_2`, `switch.alfen_charger_enabled_socket_2`).

> **Note:** Alfen has no dedicated enable/disable coil — the charger-enabled switch and sensor are derived from the max-current setpoint (register 1210). Turning charging off/on this way is equivalent to setting `number.alfen_max_current_limit_s1` to 0 A or back to its allowed maximum; don't drive both the switch and the number entity from automations at the same time to avoid fighting setpoints.

## Known Issues

- Power budget may reset to 0A when no car is connected (fixed in NG9xx firmware [6.4.0-4210](https://knowledge.alfen.com/space/IN/243466257)). The integration raises a repair notification in Settings → Repairs if it detects an NG9xx charger on older firmware.
- **Reallin power meter (post-2021)**: Chargers with a Reallin power meter produced after 2021 only export a subset of measurement values. Per-phase energy, apparent energy, and reactive energy sensors will show as "unavailable" (NaN). This is a hardware limitation, not a bug.
- **AHP platform, integration < v0.2.1**: all entities stay unavailable because a single oversized Modbus read fails on every poll cycle. Update to v0.2.1 or newer ([#40](https://github.com/ThaStealth/alfen_modbus/issues/40)).
- **Breaking change**: the Phase Mode select's options changed from `"1 Phase"`/`"3 Phases"` to the literal values `"1"`/`"3"` (needed for EVCC compatibility). Update any automation, script, or dashboard that matches on the old option text.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Changelog

### v1.2.0 (beta)

- **Merged with ThaStealth/alfen_modbus 0.3.0-beta.1** - Translated entity names (8 languages), EVCC support with a Charger Enabled switch, the full SCN register block, diagnostics download, a repair notice for outdated NG9xx firmware, and a more robust Modbus connection. See the breaking changes below
- **Device classes** - Every sensor with a unit now has a Home Assistant device class (current, voltage, frequency, power, apparent power, reactive power, energy, reactive energy, duration, temperature), and the power factor sensors have the power factor class. Entity selectors that filter on device class now list these sensors. Apparent energy (VAh) has no device class, because Home Assistant has none for it
- **Energy registers** - Apparent and reactive energy were read 2 registers too early, so their values were wrong. They now follow the register map (394-425)
- **Apparent power and energy** - These sensors now show values. Their unique IDs are the original ones again (`..._apparantPowerL1` and so on): entities created by v1.0.2 to v1.1.0 never had values and stay behind as orphans, which you can delete
- **Reactive units** - Reactive power is now in `var` (was `VAr`) and reactive energy in `varh` (was `VAh`, which was wrong). Home Assistant flags the unit change for existing statistics: fix it in **Developer tools > Statistics**
- **Car connected and charging** - Mode 3 states are matched by family (`A1`, `B1`, ... are recognised), and the session runs from plug-in to unplug, so a short charging pause no longer starts a new session. Only state A (no car) ends a session: the short E state that Alfen reports at the end of a charge no longer resets it (beta.2)
- **Session duration** - Now a number of seconds (it showed a time such as `0:12:34` with unit `s`)
- **State classes** - Energy counters are totals; text, on/off and settings sensors (name, firmware, mode 3 state, number of sockets, ...) no longer have a state class and no longer go into long-term statistics
- **Breaking changes** - Check automations, scripts, templates and dashboards that use these entities:
  1. **Phase Mode select** (`select.*_usable_phases*`): the options are `1` and `3` (were `1 Phase` and `3 Phases`). A `select.select_option` call with the old text fails. The **Charging mode** sensor still shows `1 Phase` / `3 Phases`.
  2. **Backoffice connected, Car connected, Car charging and Received SP accounted for sensors**: they report `on` / `off` (were `True` / `False`). The binary sensors for car connected and car charging are unchanged.
  3. **Current session duration**: a number of seconds (was text such as `0:12:34`).
  4. **Current session Wh and duration**: they count from plug-in to unplug, including charging pauses and the time the car is connected without charging. They counted from the start of charging and restarted after each charging pause. Only unplug (Mode 3 state `A`) ends a session.
  5. **Car connected**: Mode 3 states are matched by family. `A1` and an empty state now read as not connected (they read as connected). `E` and `F` read as not connected, as before.
  6. **Entity names**: they come from translations and follow the Home Assistant language. Some friendly names change, for example "Usable phases1" becomes "Usable phases". Entity IDs of existing entities do not change. A new installation gets entity IDs from the new names.
  7. **Apparent power and apparent energy sensors**: they use their original unique IDs again (`<name>_socket_1_apparantPowerL1` and so on). Installations that started on v1.0.0 or v1.0.1 get their original entities back; if you disabled those, enable them again. The entities of v1.0.2 to v1.1.0 never had values and stay behind as orphans: delete them in **Settings → Entities**.
  8. **Long-term statistics**: reactive power is in `var` (was `VAr`) and reactive energy in `varh` (was `VAh`). Number of sockets, Number of SCN sockets and Modbus table version no longer have a state class. **Developer tools → Statistics** shows a notice for each: accept the new unit or delete the old statistics.

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
