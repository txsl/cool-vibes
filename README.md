# Office Aircon — Daikin BRC1H ("Madoka") web control

A small Python service that turns a single phone-only Daikin BRC1H wall controller
into a **shared web app** anyone in the office can open to see and adjust the
aircon. It runs on a Mac (e.g. an always-on Mac mini), holds one persistent
Bluetooth Low Energy (BLE) connection to the controller, and serves a control
page over the local network.

This sidesteps the usual limitation where only one phone can pair with the unit
at a time: the server owns the single BLE link, and everyone shares it through
the web page.

## How it works

The BRC1H exposes an emulated UART-over-BLE protocol (reverse-engineered by
Benjamin Lafois and implemented in [pymadoka](https://github.com/mduran80/pymadoka)).
This project talks that protocol directly — no cloud, no Daikin account, no WiFi
adapter required.

```
Browser (phone/laptop)  ──HTTP──►  daikin_server.py  ──BLE──►  BRC1H controller ──►  AC unit
        (office LAN)                  (on the Mac mini)            (in the room)
```

## Files

| File | Purpose |
|------|---------|
| `daikin_server.py` | The web server + BLE bridge. This is the thing you run. |
| `brc1h_spike.py`   | A read-only connectivity tester. Use it once to scan for units and complete pairing, and any time you need to debug the BLE link. |
| `requirements.txt` | Python dependencies. |

## Requirements

- A Mac with Bluetooth (tested on macOS via CoreBluetooth).
- Python 3.10+.
- A Daikin BRC1H controller with Bluetooth enabled in its on-device menu.

## Setup

```bash
# 1. (recommended) create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. install dependencies
pip3 install -r requirements.txt
```

### First-time pairing

The Mac must be **bonded** (paired) to the controller once before the server can
talk to it. Use the spike script to find your unit and pair:

```bash
# list all nearby controllers with signal strength
python3 brc1h_spike.py --scan --timeout 15

# connect to the office unit (use the address from the scan) and complete pairing
python3 brc1h_spike.py --address <ADDRESS>
```

When prompted, start pairing on the controller's Bluetooth menu (it shows a PIN),
accept the macOS "Bluetooth Pairing Request" banner (top-right of the screen),
and enter the PIN. After this one-time step, connections are silent.

A successful run prints the current power state, mode, setpoint and room
temperature.

> On macOS, BLE devices are identified by a CoreBluetooth UUID, not a MAC address,
> so the "address" you pass will look like `XXXXXXXX-XXXX-...`.

## Running the server

```bash
python3 daikin_server.py --address <ADDRESS>
```

Then find the Mac's LAN IP and open the page from any office device:

```bash
ipconfig getifaddr en0        # prints the Mac's IP, e.g. 192.168.0.10
# open http://192.168.0.10:8000 in a browser
```

Options:

| Flag | Default | Meaning |
|------|---------|---------|
| `--address` | (required) | BLE address / CoreBluetooth UUID of the controller |
| `--host` | `0.0.0.0` | Bind address (all interfaces) |
| `--port` | `8000` | Port to serve on |
| `--selftest` | — | Run offline protocol-encoding checks and exit |

The web page shows room temperature and lets you change power, target
temperature, mode (Cool/Heat/Auto/Fan/Dry) and fan speed, auto-refreshing every
10 seconds.

## Stopping safely

**Important:** stop the server with a single `Ctrl+C` and wait for it to print:

```
Disconnected cleanly. Safe to close this window now.
```

That ensures the BLE link is torn down gracefully. An *improper* disconnect has
been reported to wedge the controller until the AC unit is power-cycled at the
isolator. So:

- **Do** press `Ctrl+C` once and wait for the "Disconnected cleanly" message.
- **Don't** press `Ctrl+C` twice, `kill -9` the process, or close the terminal /
  let the Mac sleep mid-connection. (A plain `kill <pid>` / SIGTERM is fine — it
  shuts down gracefully like `Ctrl+C`.)

## Notes & limitations

- While the server holds the connection, phones running the Daikin Madoka app
  cannot connect to that unit — this is the intended "one shared controller" model.
- There is no authentication: anyone on the office network can open the page. It
  is meant for a trusted LAN.
- BLE is short-range; keep the Mac within Bluetooth range of the controller.

## Known issues / TODO

- [ ] **Temperature changes don't apply.** Fan and mode writes work, but
  `SetSetpoint` is acknowledged and then ignored by the unit. Likely cause: the
  command needs the full setpoint field block (range-enabled, setpoint-mode,
  limits) rather than just the cooling/heating values. Planned fix: read the
  current setpoint block and echo it back with only the target changed.
- [ ] **Add swing / louver controls.** Need to identify the swing command id
  (not in the core protocol notes) — check pymadoka's feature set.
- [ ] Single unit only. Multi-room support (one card per controller) is a
  possible future extension.
- [ ] The BLE protocol helpers are currently duplicated between the two scripts;
  could be factored into a shared module.

## Credits

- BLE protocol reverse-engineering: [Benjamin Lafois — Daikin-Madoka-BRC1H-BLE-Reverse](https://github.com/blafois/Daikin-Madoka-BRC1H-BLE-Reverse)
- Reference implementation: [pymadoka](https://github.com/mduran80/pymadoka)
- BLE library: [bleak](https://github.com/hbldh/bleak)
