#!/usr/bin/env python3
"""
daikin_server.py - Shared web control for a Daikin BRC1H "Madoka" controller.

Runs on a Mac (e.g. an office Mac mini). Holds ONE persistent bonded Bluetooth
connection to the controller and serves a web page that everyone on the office
network can open to see and adjust the aircon. All web clients funnel through the
single BLE link, which sidesteps the "only one phone can pair" limitation.

    Read : power, mode, setpoint, room temperature, fan speed
    Write: on/off, target temperature, mode, fan speed

SETUP
    pip3 install bleak fastapi "uvicorn[standard]"
    python3 daikin_server.py --address <BRC1H address>
        (use the address brc1h_spike.py --scan showed for the office unit)

    Then open  http://<this-mac's-LAN-IP>/  from any office machine/phone.
    Find this Mac's IP with:  ipconfig getifaddr en0

    Port 80 needs no sudo: since macOS 10.14 an ordinary user may bind ports
    below 1024 on the wildcard address, which is what the default --host 0.0.0.0
    binds. Only a specific address (e.g. --host 127.0.0.1) still needs root.
    Don't run under sudo - CoreBluetooth's Bluetooth grant is tied to your login
    session, and running as root can lose it.

NOTES
    * The controller must already be BONDED to this Mac (run brc1h_spike.py once
      and complete pairing). After that, connections are silent.
    * While this server holds the connection, phones running the Madoka app can't
      connect - that's the intended "one shared controller" model.
    * Protocol: emulated UART-over-BLE, reverse-engineered by Benjamin Lafois and
      codified in pymadoka. This file is self-contained (no pymadoka dependency).
"""

import argparse
import asyncio
import logging
import os
import socket
import sqlite3
import time
from logging.handlers import TimedRotatingFileHandler

from madoka_protocol import (
    SERVICE_UUID, NOTIFY_CHAR, WRITE_CHAR, MAX_CHUNK_SIZE, CHUNK_DATA_LEN,
    CMD_GET_POWER, CMD_GET_MODE, CMD_GET_SETPOINT, CMD_GET_FAN, CMD_GET_SENSOR,
    CMD_GET_FILTER, CMD_GET_MAINTENANCE,
    CMD_SET_POWER, CMD_SET_MODE, CMD_SET_SETPOINT, CMD_SET_FAN,
    MODE_NAMES, MODE_IDS, FAN_NAMES, MODE_HEAT, SETPOINT_EXTRA_FIELDS,
    build_command, build_query, split_in_chunks, Reassembler, cmd_id_of,
    parse_objects, temp_to_bytes, temp_from_bytes, round_setpoint,
    build_setpoint_args, parse_device_info,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("daikin")

# Defaults anchored to this file's directory so they land in the repo no matter
# what the working directory is (e.g. under a LaunchAgent).
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(REPO_DIR, "aircon_history.db")
DEFAULT_LOG_FILE = os.path.join(REPO_DIR, "logs", "daikin.log")
DEFAULT_ENV_FILE = os.path.join(REPO_DIR, ".env")


def load_env_file(path=DEFAULT_ENV_FILE):
    """Read simple KEY=VALUE lines from `path` into os.environ.

    Deliberately tiny (no python-dotenv dependency): blank lines and #comments
    are skipped, surrounding quotes are stripped, and variables already set in
    the real environment win. Missing file is not an error.

    This exists mainly for launchd: a LaunchAgent runs with a minimal
    environment and never sources a shell profile, so an exported variable
    would not reach the service - but a file next to the code does.
    """
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return {}
    loaded = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded
LOG_RETENTION_DAYS = 180   # daily rotation, ~6 months kept


def setup_file_logging(path: str) -> logging.Handler:
    """Add a rotating file handler (one file per day, kept LOG_RETENTION_DAYS)
    alongside the console. Creates the parent directory if needed."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    handler = TimedRotatingFileHandler(path, when="midnight",
                                       backupCount=LOG_RETENTION_DAYS)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(name)s  %(message)s"))
    logging.getLogger().addHandler(handler)
    return handler

# ----------------------------------------------------------------------------
# Server-side policy (BLE protocol + pure helpers live in madoka_protocol.py)
# ----------------------------------------------------------------------------
# Fallback setpoint window if the controller doesn't report its own limits.
SETPOINT_MIN_DEFAULT = 16.0
SETPOINT_MAX_DEFAULT = 32.0
# Loose absolute sanity bounds for the write API (the controller and the
# device-reported window are the real limits; this only blocks junk values).
SETPOINT_HARD_MIN = 10.0
SETPOINT_HARD_MAX = 40.0

# Background poller cadence.
POLL_INTERVAL = 10   # seconds between status polls (drives the cache + live UI)
LOG_INTERVAL = 60    # max seconds between logged samples (also logs on any change)
FILTER_INTERVAL = 300  # seconds between (slow) clean-filter reads; it changes rarely

# ----------------------------------------------------------------------------
# BLE controller manager (one persistent, lock-serialized connection)
# ----------------------------------------------------------------------------
class Controller:
    def __init__(self, address: str):
        self.address = address
        self.client = None
        self.lock = asyncio.Lock()
        self.reasm = Reassembler()
        self.pending = {}
        self.connected = False
        self.latest = {"connected": False}   # cached status served to web clients
        self.latest_ts = 0.0
        self.device_info = {}
        self.filter_dirty = None   # cached clean-filter flag (read on a slow cadence)

    def _on_notify(self, _sender, data: bytearray):
        payload = self.reasm.feed(bytearray(data))
        if payload is None or len(payload) <= 4:
            return
        fut = self.pending.get(cmd_id_of(payload))
        if fut and not fut.done():
            fut.set_result(payload)

    async def ensure_connected(self):
        from bleak import BleakClient, BleakScanner
        if self.client is not None and self.client.is_connected:
            return
        self.connected = False
        log.info("Connecting to %s ...", self.address)
        dev = await BleakScanner.find_device_by_address(self.address, timeout=10.0)
        if dev is None:
            raise RuntimeError(
                f"Controller {self.address} not found. It may be connected to a phone "
                f"(close the Madoka app) or out of Bluetooth range."
            )
        self.client = BleakClient(dev, disconnected_callback=self._on_disconnect)
        await self.client.connect()
        await self.client.start_notify(NOTIFY_CHAR, self._on_notify)
        self.connected = True
        log.info("Connected and subscribed.")

    def _on_disconnect(self, _client):
        self.connected = False
        log.warning("BLE link dropped; will reconnect on next request.")

    async def request(self, cmd_id: int, args=None, timeout=6.0) -> dict:
        """Send a command/query and return the parsed response objects."""
        async with self.lock:
            await self.ensure_connected()
            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            self.pending[cmd_id] = fut
            try:
                for chunk in build_command(cmd_id, args):
                    await self.client.write_gatt_char(WRITE_CHAR, bytes(chunk), response=False)
                payload = await asyncio.wait_for(fut, timeout=timeout)
                return parse_objects(payload)
            finally:
                self.pending.pop(cmd_id, None)

    # ---- reads -------------------------------------------------------------
    async def get_status(self) -> dict:
        power = await self.request(CMD_GET_POWER)
        mode = await self.request(CMD_GET_MODE)
        setp = await self.request(CMD_GET_SETPOINT)
        sensor = await self.request(CMD_GET_SENSOR)
        fan = await self.request(CMD_GET_FAN)

        mode_id = mode.get(0x20, b"\x02")[0]
        is_heat = mode_id == MODE_HEAT
        cool_sp = temp_from_bytes(setp[0x20]) if 0x20 in setp else None
        heat_sp = temp_from_bytes(setp[0x21]) if 0x21 in setp else None
        fan_cool = fan.get(0x20, b"\x00")[0]
        fan_heat = fan.get(0x21, b"\x00")[0]

        # The controller reports the allowed setpoint window in the same
        # GetSetpoint response: cooling lower/upper at 0xa2/0xb2, heating at
        # 0xa3/0xb3 (2-byte temps). Fall back to the documented 16-32C range.
        lo_id, hi_id = (0xA3, 0xB3) if is_heat else (0xA2, 0xB2)
        min_sp = temp_from_bytes(setp[lo_id]) if lo_id in setp else SETPOINT_MIN_DEFAULT
        max_sp = temp_from_bytes(setp[hi_id]) if hi_id in setp else SETPOINT_MAX_DEFAULT

        room = None
        if 0x40 in sensor:
            room = int.from_bytes(sensor[0x40], "big", signed=True)
        outdoor = None
        if 0x41 in sensor and sensor[0x41][:1] != b"\xff":
            outdoor = int.from_bytes(sensor[0x41], "big", signed=True)

        return {
            "connected": True,
            "power_on": bool(power.get(0x20, b"\x00")[0]),
            "mode_id": mode_id,
            "mode": MODE_NAMES.get(mode_id, f"#{mode_id}"),
            "setpoint": heat_sp if is_heat else cool_sp,
            "cooling_setpoint": cool_sp,
            "heating_setpoint": heat_sp,
            "min_setpoint": min_sp,
            "max_setpoint": max_sp,
            "room_temp": room,
            "outdoor_temp": outdoor,
            "fan_id": fan_heat if is_heat else fan_cool,
            "fan": FAN_NAMES.get(fan_heat if is_heat else fan_cool, "?"),
            "filter_dirty": self.filter_dirty,   # from the slow filter read (cached)
        }

    async def refresh(self) -> dict:
        """Read status, update the cache, and return it. Errors are captured
        into the cache (not raised) so callers always get a dict."""
        try:
            status = await self.get_status()
        except Exception as e:
            log.warning("status read failed: %s", e)
            status = {"connected": False, "error": str(e)}
        self.latest = status
        self.latest_ts = time.time()
        return status

    async def read_device_info(self) -> dict:
        """Read static model/firmware info once (GetMaintenanceInformation)."""
        try:
            maint = await self.request(CMD_GET_MAINTENANCE)
            self.device_info = parse_device_info(maint)
        except Exception as e:
            log.warning("device info read failed: %s", e)
        return self.device_info

    async def read_filter(self) -> bool:
        """Read the clean-filter indicator (GetCleanFilter, field 0x62 bit 0).
        Cached on the controller and patched into the served status."""
        try:
            f = await self.request(CMD_GET_FILTER)
            self.filter_dirty = bool(f.get(0x62, b"\x00")[0] & 0x01)
            if isinstance(self.latest, dict):
                self.latest["filter_dirty"] = self.filter_dirty
        except Exception as e:
            log.warning("filter read failed: %s", e)
        return self.filter_dirty

    # ---- writes ------------------------------------------------------------
    async def set_power(self, on: bool):
        await self.request(CMD_SET_POWER, [(0x20, bytes([1 if on else 0]))])

    async def set_mode(self, mode_id: int):
        await self.request(CMD_SET_MODE, [(0x20, bytes([mode_id]))])

    async def set_setpoint(self, celsius: float):
        """Set the target temperature.

        Two unit quirks drive this:
          * It keeps the cooling and heating setpoints locked together as one
            shared value and *rejects* a command that sets them to different
            values, so we write both to the same target (rather than preserving
            one and changing the other).
          * It only accepts whole-degree setpoints, so the target is snapped to
            the nearest degree.
        The full field block (range/mode/limits) is still required or the
        command is acked and ignored.
        """
        target = round_setpoint(celsius)
        log.info("set_setpoint: requested %.1f -> %dC", celsius, target)
        await self.request(CMD_SET_SETPOINT, build_setpoint_args(target, target))

    async def set_fan(self, speed: int):
        await self.request(CMD_SET_FAN, [(0x20, bytes([speed])), (0x21, bytes([speed]))])

    async def disconnect(self):
        if self.client is not None and self.client.is_connected:
            try:
                await self.client.stop_notify(NOTIFY_CHAR)
            except Exception:
                pass
            await self.client.disconnect()


# ----------------------------------------------------------------------------
# Time-series storage (SQLite)
# ----------------------------------------------------------------------------
class HistoryStore:
    """Append-only SQLite store: a `samples` time series plus a one-row
    `device_info` table. All access is from the event-loop thread."""

    def __init__(self, path: str):
        self.path = path
        # In the app the connection is only touched from the event-loop thread,
        # but check_same_thread=False lets a threaded caller (e.g. TestClient, or
        # a future worker) use it too; access stays effectively serialized.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS samples (
            ts INTEGER PRIMARY KEY,
            power INTEGER, mode INTEGER,
            cooling_setpoint REAL, heating_setpoint REAL,
            room_temp INTEGER, outdoor_temp INTEGER, fan INTEGER,
            filter_dirty INTEGER)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS device_info (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            model TEXT, model_aux TEXT,
            controller_version TEXT, comm_version TEXT, updated_ts INTEGER)""")
        # Migrate DBs created before filter_dirty existed.
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(samples)").fetchall()]
        if "filter_dirty" not in cols:
            self.db.execute("ALTER TABLE samples ADD COLUMN filter_dirty INTEGER")
        self.db.commit()

    def insert_sample(self, ts: int, s: dict):
        fd = s.get("filter_dirty")
        self.db.execute(
            "INSERT OR REPLACE INTO samples "
            "(ts, power, mode, cooling_setpoint, heating_setpoint, room_temp, "
            "outdoor_temp, fan, filter_dirty) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, int(bool(s.get("power_on"))), s.get("mode_id"),
             s.get("cooling_setpoint"), s.get("heating_setpoint"),
             s.get("room_temp"), s.get("outdoor_temp"), s.get("fan_id"),
             None if fd is None else int(fd)))
        self.db.commit()

    def set_device_info(self, info: dict, ts: int):
        self.db.execute(
            "INSERT OR REPLACE INTO device_info VALUES (1,?,?,?,?,?)",
            (info.get("model"), info.get("model_aux"),
             info.get("controller_version"), info.get("comm_version"), ts))
        self.db.commit()

    def get_device_info(self) -> dict:
        row = self.db.execute(
            "SELECT model, model_aux, controller_version, comm_version "
            "FROM device_info WHERE id = 1").fetchone()
        if not row:
            return {}
        return {"model": row[0], "model_aux": row[1],
                "controller_version": row[2], "comm_version": row[3]}

    def query_since(self, since_ts: int) -> list:
        return self.db.execute(
            "SELECT ts, power, mode, cooling_setpoint, heating_setpoint, "
            "room_temp, outdoor_temp, fan, filter_dirty FROM samples "
            "WHERE ts >= ? ORDER BY ts", (since_ts,)).fetchall()

    def close(self):
        self.db.close()


async def run_poller(controller: "Controller", store: "HistoryStore"):
    """The single background reader. Refreshes the cache every POLL_INTERVAL and
    appends a sample to the store every LOG_INTERVAL or whenever state changes."""
    last_key = None
    last_log = 0.0
    last_filter = 0.0
    while True:
        try:
            s = await controller.refresh()
            if s.get("connected"):
                now = time.time()
                if not controller.device_info:
                    await controller.read_device_info()
                    if controller.device_info:
                        store.set_device_info(controller.device_info, int(now))
                if now - last_filter >= FILTER_INTERVAL:
                    await controller.read_filter()  # patches s["filter_dirty"] in place
                    last_filter = now
                key = (int(bool(s.get("power_on"))), s.get("mode_id"),
                       s.get("cooling_setpoint"), s.get("heating_setpoint"),
                       s.get("room_temp"), s.get("outdoor_temp"),
                       s.get("fan_id"), controller.filter_dirty)
                if key != last_key or (now - last_log) >= LOG_INTERVAL:
                    store.insert_sample(int(now), s)
                    last_key, last_log = key, now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("poller iteration failed: %s", e)
        await asyncio.sleep(POLL_INTERVAL)


# ----------------------------------------------------------------------------
# Web app
# ----------------------------------------------------------------------------
def build_app(controller: "Controller", store: "HistoryStore"):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel

    app = FastAPI(title="Office Aircon")

    class PowerBody(BaseModel):
        on: bool

    class ModeBody(BaseModel):
        mode: int

    class SetpointBody(BaseModel):
        temp: float

    class FanBody(BaseModel):
        speed: int

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(PAGE)

    @app.get("/history", response_class=HTMLResponse)
    async def history_page():
        return HTMLResponse(PAGE_HISTORY)

    @app.get("/api/status")
    async def status():
        # Served from the poller's cache - no BLE read per request.
        return JSONResponse(controller.latest)

    @app.get("/api/history")
    async def history(hours: float = 24.0):
        since = int(time.time() - hours * 3600)
        t, room, sp, power, mode_, fan, filt = [], [], [], [], [], [], []
        for ts, pw, md, cool, heat, rt, outdoor, fn, fd in store.query_since(since):
            t.append(ts)
            room.append(rt)
            sp.append(heat if md == MODE_HEAT else cool)  # active-mode setpoint
            power.append(pw)
            mode_.append(md)
            fan.append(fn)
            filt.append(fd)
        return JSONResponse({"info": store.get_device_info(), "t": t, "room": room,
                             "sp": sp, "power": power, "mode": mode_, "fan": fan, "filter": filt})

    @app.post("/api/power")
    async def power(body: PowerBody):
        await controller.set_power(body.on)
        return JSONResponse(await controller.refresh())

    @app.post("/api/mode")
    async def mode(body: ModeBody):
        if body.mode not in MODE_NAMES:
            raise HTTPException(400, "invalid mode")
        await controller.set_mode(body.mode)
        return JSONResponse(await controller.refresh())

    @app.post("/api/setpoint")
    async def setpoint(body: SetpointBody):
        # Loose sanity bound only - the real limits are the per-mode window the
        # controller reports (min_setpoint/max_setpoint in /api/status), which
        # the web UI enforces. The controller itself rejects anything it won't
        # accept, so this just rejects obviously-bogus values.
        if not (SETPOINT_HARD_MIN <= body.temp <= SETPOINT_HARD_MAX):
            raise HTTPException(400, f"temp out of range ({SETPOINT_HARD_MIN:.0f}-{SETPOINT_HARD_MAX:.0f}C)")
        await controller.set_setpoint(body.temp)
        return JSONResponse(await controller.refresh())

    @app.post("/api/fan")
    async def fan(body: FanBody):
        if not (1 <= body.speed <= 5):
            raise HTTPException(400, "invalid fan speed")
        await controller.set_fan(body.speed)
        return JSONResponse(await controller.refresh())

    poller_task = {}

    @app.on_event("startup")
    async def _startup():
        poller_task["t"] = asyncio.create_task(run_poller(controller, store))
        log.info("Background poller started (poll every %ds, log every %ds).",
                 POLL_INTERVAL, LOG_INTERVAL)

    @app.on_event("shutdown")
    async def _shutdown():
        log.info("Shutting down - disconnecting from controller cleanly ...")
        task = poller_task.get("t")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await controller.disconnect()
            log.info("Disconnected cleanly. Safe to close this window now.")
        except Exception as e:
            log.warning("Disconnect hit an error: %s", e)
        store.close()

    return app


# The pages are built from web/src by web/build.py; edit those, not web/dist.
WEB_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dist")


def read_page(name: str) -> str:
    with open(os.path.join(WEB_DIST, name), encoding="utf-8") as f:
        return f.read()


PAGE = read_page("index.html")
PAGE_HISTORY = read_page("history.html")


# ----------------------------------------------------------------------------
# Offline self-test of the command encoders
# ----------------------------------------------------------------------------
def selftest() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        passed = got == want
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        if not passed:
            print(f"        got : {got.hex() if isinstance(got, (bytes, bytearray)) else got}")
            print(f"        want: {want.hex() if isinstance(want, (bytes, bytearray)) else want}")

    # No-arg query still encodes like the spike
    check("GetSetpoint query", bytes(build_command(CMD_GET_SETPOINT)[0]),
          bytes([0x00, 0x06, 0x00, 0x00, 0x40, 0x00, 0x00]))

    # SetSettingStatus(on): func 0x4020, arg 0x20 size1 = 1
    # payload = [len][00][40][20][20][01][01]; len=7 -> chunk prefixes 0x00
    check("SetPower ON", bytes(build_command(CMD_SET_POWER, [(0x20, bytes([1]))])[0]),
          bytes([0x00, 0x07, 0x00, 0x40, 0x20, 0x20, 0x01, 0x01]))

    # SetOperationMode(Cool=3): func 0x4030, arg 0x20 size1 = 3
    check("SetMode Cool", bytes(build_command(CMD_SET_MODE, [(0x20, bytes([3]))])[0]),
          bytes([0x00, 0x07, 0x00, 0x40, 0x30, 0x20, 0x01, 0x03]))

    # SetSetpoint: 22*128 = 0x0B00. The command must carry the full field block
    # (cooling + heating + range/mode/limits), not just the two temperatures, or
    # the controller acks and ignores it. Reassemble all chunks and inspect them.
    sp = temp_to_bytes(22.0)
    check("temp 22.0C -> bytes", sp, bytes([0x0B, 0x00]))
    sp_args = build_setpoint_args(22.0, 22.0)
    check("SetSetpoint field count", len(sp_args), 2 + len(SETPOINT_EXTRA_FIELDS))
    sp_payload = bytearray()
    for chunk in build_command(CMD_SET_SETPOINT, sp_args):
        sp_payload += chunk[1:]  # drop the per-chunk index byte
    check("SetSetpoint length byte", sp_payload[0], len(sp_payload))
    check("SetSetpoint cmd id", cmd_id_of(sp_payload), CMD_SET_SETPOINT)
    sp_objs = parse_objects(sp_payload)
    check("SetSetpoint cooling field", sp_objs.get(0x20), bytes([0x0B, 0x00]))
    check("SetSetpoint heating field", sp_objs.get(0x21), bytes([0x0B, 0x00]))
    check("SetSetpoint mode field", sp_objs.get(0x31), bytes([0x02]))
    check("SetSetpoint zeroed limit", sp_objs.get(0xB2), bytes([0x00, 0x00]))

    # SetFanSpeed(High=5): func 0x4050, 0x20 + 0x21 each size1 = 5
    check("SetFan High", bytes(build_command(CMD_SET_FAN, [(0x20, bytes([5])), (0x21, bytes([5]))])[0]),
          bytes([0x00, 0x0A, 0x00, 0x40, 0x50, 0x20, 0x01, 0x05, 0x21, 0x01, 0x05]))

    # round-trip temperature decode
    check("temp decode 22.0", temp_from_bytes(bytes([0x0B, 0x00])), 22.0)

    # setpoints snap to whole degrees (the unit rejects half-degree values)
    check("round_setpoint 21.0", round_setpoint(21.0), 21)
    check("round_setpoint 21.5 -> 22", round_setpoint(21.5), 22)
    check("round_setpoint 21.4 -> 21", round_setpoint(21.4), 21)
    check("round_setpoint 20.5 -> 21", round_setpoint(20.5), 21)

    # device info parsing (model + firmware) from a GetMaintenance response
    di = parse_device_info({
        0x40: bytes.fromhex("00000000000000000000000000000000465846513332415645420000000000"
                            "4139502f303238000000000000000000"),
        0x45: bytes([0x03, 0x06, 0x00]),
        0x46: bytes([0x05, 0x11]),
    })
    check("device model", di["model"], "FXFQ32AVEB")
    check("device model_aux", di["model_aux"], "A9P/028")
    check("device controller_version", di["controller_version"], "3.6.0")
    check("device comm_version", di["comm_version"], "5.17")

    print("\nSelf-test:", "ALL PASSED" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


def port_bindable(host, port):
    """False only if binding host:port is refused for lack of privileges.

    Any other failure (e.g. the port already in use) returns True so that
    uvicorn reports it in its own words.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except PermissionError:
        return False
    except OSError:
        return True
    finally:
        s.close()
    return True


def main():
    load_env_file()   # before argparse, so --address can default to $DAIKIN_ADDRESS

    p = argparse.ArgumentParser(description="Shared web control for a Daikin BRC1H.")
    p.add_argument("--address", default=os.environ.get("DAIKIN_ADDRESS"),
                   help="BLE address / CoreBluetooth UUID of the controller "
                        "(default: $DAIKIN_ADDRESS, which <repo>/.env can supply)")
    p.add_argument("--host", default="0.0.0.0", help="bind host (default 0.0.0.0 = all interfaces)")
    p.add_argument("--port", type=int, default=80, help="port (default 80)")
    p.add_argument("--db", default=DEFAULT_DB,
                   help="SQLite history file (default: <repo>/aircon_history.db)")
    p.add_argument("--log-file", default=DEFAULT_LOG_FILE,
                   help="rotating log file (default: <repo>/logs/daikin.log)")
    p.add_argument("--selftest", action="store_true", help="run offline encoder checks and exit")
    args = p.parse_args()

    if args.selftest:
        raise SystemExit(selftest())

    if not args.address:
        p.error("no controller address: pass --address, or set DAIKIN_ADDRESS in the "
                "environment or in <repo>/.env (get the address from: "
                "python brc1h_spike.py --scan)")

    setup_file_logging(args.log_file)
    log.info("Logging to %s (daily rotation, %d days kept)", args.log_file, LOG_RETENTION_DAYS)

    # Checked up front rather than by catching the error from uvicorn.run:
    # uvicorn handles a failed bind itself (logs it, then SystemExit), so a
    # PermissionError never reaches us - and it runs the app's startup, which
    # opens the BLE link, before it binds.
    if not port_bindable(args.host, args.port):
        log.error("Permission denied binding %s:%d.", args.host, args.port)
        print(
            f"\nCould not bind {args.host}:{args.port} - permission denied.\n"
            "On macOS an ordinary user can bind ports below 1024 only on the wildcard\n"
            "address. Drop --host to use the default 0.0.0.0, or pass --port 8000.\n"
            "Avoid sudo: CoreBluetooth's Bluetooth grant is tied to your login session.\n"
        )
        raise SystemExit(1)

    import uvicorn
    controller = Controller(args.address)
    store = HistoryStore(args.db)
    app = build_app(controller, store)
    print(f"\nOffice Aircon server starting.")
    suffix = "" if args.port == 80 else f":{args.port}"
    print(f"Open http://<this-mac-ip>{suffix}  (find the IP with: ipconfig getifaddr en0)\n")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
