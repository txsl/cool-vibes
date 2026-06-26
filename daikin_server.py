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

    Then open  http://<this-mac's-LAN-IP>:8000  from any office machine/phone.
    Find this Mac's IP with:  ipconfig getifaddr en0

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
import math
import sqlite3
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("daikin")

# ----------------------------------------------------------------------------
# BLE protocol constants (BRC1H AC-management service)
# ----------------------------------------------------------------------------
SERVICE_UUID = "2141e110-213a-11e6-b67b-9e71128cae77"
NOTIFY_CHAR  = "2141e111-213a-11e6-b67b-9e71128cae77"  # RX (device -> us)
WRITE_CHAR   = "2141e112-213a-11e6-b67b-9e71128cae77"  # TX (us -> device)

MAX_CHUNK_SIZE = 20
CHUNK_DATA_LEN = 19

# Query (read) function ids
CMD_GET_POWER    = 0x0020
CMD_GET_MODE     = 0x0030
CMD_GET_SETPOINT = 0x0040
CMD_GET_FAN      = 0x0050
CMD_GET_SENSOR   = 0x0110
CMD_GET_MAINTENANCE = 0x0130  # model + firmware versions
CMD_GET_FILTER   = 0x0100  # clean-filter indicator (field 0x62, bit 0)
# Command (write) function ids
CMD_SET_POWER    = 0x4020
CMD_SET_MODE     = 0x4030
CMD_SET_SETPOINT = 0x4040
CMD_SET_FAN      = 0x4050

MODE_NAMES = {0: "Fan", 1: "Dry", 2: "Auto", 3: "Cool", 4: "Heat", 5: "Ventilation"}
MODE_IDS = {v.lower(): k for k, v in MODE_NAMES.items()}
FAN_NAMES = {1: "Low", 2: "Low-Med", 3: "Medium", 4: "Med-High", 5: "High"}
MODE_HEAT = 4

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

# Extra fields a SetSetpoint command must carry *in addition* to the two
# temperatures. The BRC1H acks a setpoint command that contains only the
# cooling/heating values and then silently ignores it; it only applies the
# change when the full field block (range flag, setpoint mode, and every
# min/max limit) is present. These are sent verbatim, exactly as pymadoka's
# SetPointStatus.get_values() does: range disabled, setpoint mode 2, all
# limits zeroed. Each entry is (object_id, byte_size, value).
SETPOINT_EXTRA_FIELDS = [
    (0x30, 1, 0),  # range_enabled
    (0x31, 1, 2),  # setpoint mode
    (0x32, 1, 0),  # minimum_differential
    (0xA0, 1, 0),  # min_cooling_lowerlimit
    (0xA1, 1, 0),  # min_heating_lowerlimit
    (0xA2, 2, 0),  # cooling_lowerlimit
    (0xA3, 2, 0),  # heating_lowerlimit
    (0xA4, 1, 0),  # cooling_lowerlimit_symbol
    (0xA5, 1, 0),  # heating_lowerlimit_symbol
    (0xB0, 1, 0),  # max_cooling_upperlimit
    (0xB1, 1, 0),  # max_heating_upperlimit
    (0xB2, 2, 0),  # cooling_upperlimit
    (0xB3, 2, 0),  # heating_upperlimit
    (0xB4, 1, 0),  # cooling_upperlimit_symbol
    (0xB5, 1, 0),  # heating_upperlimit_symbol
]


# ----------------------------------------------------------------------------
# Protocol helpers (pure; covered by --selftest)
# ----------------------------------------------------------------------------
def build_command(cmd_id: int, args=None) -> list:
    """Build on-wire chunk(s) for `cmd_id` with optional [(arg_id, value_bytes), ...].

    Payload: [total_len][0x00][cmd_hi][cmd_lo] then either the no-arg marker
    (0x00 0x00) or repeating [arg_id][size][value...]. `total_len` counts itself.
    """
    body = bytearray([0x00]) + cmd_id.to_bytes(2, "big")  # 3-byte function id
    if args:
        for arg_id, value in args:
            body += bytearray([arg_id, len(value)]) + bytearray(value)
    else:
        body += bytearray([0x00, 0x00])
    payload = bytearray([0x00]) + body
    payload[0] = len(payload)
    return split_in_chunks(payload)


def split_in_chunks(data: bytearray) -> list:
    chunks = []
    idx = 0
    while True:
        piece = data[idx * CHUNK_DATA_LEN:(idx + 1) * CHUNK_DATA_LEN]
        chunks.append(bytearray(idx.to_bytes(1, "big")) + piece)
        idx += 1
        if idx * CHUNK_DATA_LEN >= len(data):
            break
    return chunks


class Reassembler:
    def __init__(self):
        self.chunks = []

    def feed(self, chunk: bytearray):
        if len(chunk) < 2:
            return None
        if chunk[0] == 0:
            self.chunks = []
        self.chunks.append(chunk)
        total_len = self.chunks[0][1]
        expected = -(-total_len // MAX_CHUNK_SIZE)
        if len(self.chunks) == expected:
            out = bytearray()
            for c in self.chunks:
                out.extend(c[1:])
            self.chunks = []
            return out
        return None


def cmd_id_of(payload: bytearray) -> int:
    return int.from_bytes(payload[2:4], "big")


def parse_objects(payload: bytearray) -> dict:
    objects = {}
    i = 4
    while i + 1 < len(payload):
        oid = payload[i]
        size = payload[i + 1]
        value = bytes(payload[i + 2:i + 2 + size])
        if len(value) < size:
            break
        objects[oid] = value
        i += 2 + size
    return objects


def temp_to_bytes(celsius: float) -> bytes:
    """Encode a temperature as the device's 2-byte GFLOAT (value * 128)."""
    return int(round(celsius * 128)).to_bytes(2, "big")


def temp_from_bytes(raw: bytes) -> float:
    """Decode a 2-byte GFLOAT temperature, rounded to the nearest 0.5C."""
    return round(int.from_bytes(raw, "big") / 128.0 * 2) / 2


def round_setpoint(celsius: float) -> int:
    """Snap a target temperature to a whole degree.

    This controller only honours integer-degree setpoints (raw value = degrees
    * 128, i.e. a multiple of 128). Half-degree values like 21.5 are silently
    rejected and leave the setpoint on its previous whole degree, so we round to
    the nearest whole degree (half rounds up) before sending."""
    return int(math.floor(celsius + 0.5))


def build_setpoint_args(cooling: float, heating: float) -> list:
    """Args for a SetSetpoint command: the cooling and heating setpoints plus
    the full range/mode/limit field block the controller requires (see
    SETPOINT_EXTRA_FIELDS). Returns [(object_id, value_bytes), ...]."""
    args = [
        (0x20, temp_to_bytes(cooling)),
        (0x21, temp_to_bytes(heating)),
    ]
    for arg_id, size, value in SETPOINT_EXTRA_FIELDS:
        args.append((arg_id, value.to_bytes(size, "big")))
    return args


def parse_device_info(maint: dict) -> dict:
    """Pull model + firmware versions out of a GetMaintenanceInformation
    (0x0130) response. Field 0x40 holds NUL-padded ASCII model strings, 0x45 a
    3-byte controller version, 0x46 a 2-byte communication-controller version."""
    info = {"model": None, "model_aux": None,
            "controller_version": None, "comm_version": None}
    raw = maint.get(0x40)
    if raw:
        text = "".join(chr(b) if 32 <= b < 127 else "\x00" for b in raw)
        tokens = [t for t in text.split("\x00") if t.strip()]
        if tokens:
            info["model"] = tokens[0]
        if len(tokens) > 1:
            info["model_aux"] = tokens[1]
    v = maint.get(0x45)
    if v and len(v) >= 3:
        info["controller_version"] = f"{v[0]}.{v[1]}.{v[2]}"
    v = maint.get(0x46)
    if v and len(v) >= 2:
        info["comm_version"] = f"{v[0]}.{v[1]}"
    return info


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
        self.db = sqlite3.connect(path)
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


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Office Aircon</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #f2f3f5; color: #1c1c1e; display: flex; justify-content: center; }
  @media (prefers-color-scheme: dark) { body { background:#000; color:#f2f2f7; } .card{background:#1c1c1e !important;} }
  .wrap { width: 100%; max-width: 440px; padding: 20px; }
  h1 { font-size: 20px; margin: 8px 4px 16px; }
  .card { background: #fff; border-radius: 18px; padding: 22px; margin-bottom: 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .room { text-align: center; }
  .room .big { font-size: 64px; font-weight: 600; line-height: 1; }
  .room .sub { color: #8a8a8e; margin-top: 6px; font-size: 14px; }
  .row { display: flex; align-items: center; justify-content: space-between; margin: 14px 0; }
  .label { font-size: 15px; color: #8a8a8e; }
  .setpoint { display: flex; align-items: center; justify-content: center; gap: 22px; }
  .setpoint .val { font-size: 40px; font-weight: 600; min-width: 110px; text-align: center; }
  button { font: inherit; border: none; border-radius: 12px; padding: 10px 14px;
           background: #e5e5ea; color: inherit; cursor: pointer; }
  button:active { transform: scale(.97); }
  button:disabled { opacity: .3; pointer-events: none; }
  .round { width: 52px; height: 52px; border-radius: 50%; font-size: 26px; }
  .seg { display: flex; gap: 8px; flex-wrap: wrap; }
  .seg button { flex: 1; min-width: 64px; }
  .seg button.on { background: #0a84ff; color: #fff; }
  .power { width: 100%; padding: 16px; font-size: 17px; font-weight: 600; border-radius: 14px; }
  .power.on { background: #34c759; color: #fff; }
  .power.off { background: #ff3b30; color: #fff; }
  .status { text-align: center; font-size: 13px; color: #8a8a8e; min-height: 18px; }
  .offline { color: #ff3b30; }
  .dim { opacity: .45; pointer-events: none; }
  .filterline { text-align:center; font-size:13px; margin-top:8px; color:#8a8a8e; min-height:16px; }
  .filterline.warn { color:#ff9f0a; font-weight:600; }
  .histlink { display:block; text-align:center; margin-top:14px; font-size:14px;
              color:#0a84ff; text-decoration:none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Office Aircon</h1>

  <div class="card room">
    <div class="big" id="room">--</div>
    <div class="sub" id="roomsub">Room temperature</div>
  </div>

  <div class="card" id="controls">
    <button class="power off" id="power" onclick="togglePower()">Loading...</button>

    <div class="row" style="margin-top:18px;">
      <span class="label">Target</span>
      <div class="setpoint">
        <button class="round" id="tempDown" onclick="bumpTemp(-1)">-</button>
        <span class="val" id="setpoint">--</span>
        <button class="round" id="tempUp" onclick="bumpTemp(1)">+</button>
      </div>
    </div>

    <div class="row"><span class="label">Mode</span></div>
    <div class="seg" id="modes"></div>

    <div class="row" style="margin-top:18px;"><span class="label">Fan</span></div>
    <div class="seg" id="fans"></div>
  </div>

  <div class="status" id="status"></div>
  <div class="filterline" id="filterStatus"></div>
  <a class="histlink" href="/history">View history ›</a>
</div>

<script>
const MODES = [[3,"Cool"],[4,"Heat"],[2,"Auto"],[0,"Fan"],[1,"Dry"]];
const FANS  = [[1,"Low"],[3,"Medium"],[5,"High"]];
let state = null, busy = false;

function el(id){ return document.getElementById(id); }

function render(s){
  state = s;
  const controls = el("controls");
  if(!s || !s.connected){
    el("status").innerHTML = '<span class="offline">Controller offline - reconnecting...</span>';
    el("filterStatus").textContent = "";
    controls.classList.add("dim");
    return;
  }
  controls.classList.remove("dim");
  el("room").textContent = (s.room_temp ?? "--") + "°";
  const p = el("power");
  p.textContent = s.power_on ? "On" : "Off";
  p.className = "power " + (s.power_on ? "on" : "off");
  el("setpoint").textContent = (s.setpoint ?? "--") + "°";
  const lo = s.min_setpoint, hi = s.max_setpoint, sp = s.setpoint;
  el("tempDown").disabled = (sp == null) || (lo != null && sp <= lo);
  el("tempUp").disabled   = (sp == null) || (hi != null && sp >= hi);

  const modes = el("modes"); modes.innerHTML = "";
  for(const [id,name] of MODES){
    const b = document.createElement("button");
    b.textContent = name;
    if(id === s.mode_id) b.classList.add("on");
    b.onclick = () => setMode(id);
    modes.appendChild(b);
  }
  const fans = el("fans"); fans.innerHTML = "";
  for(const [id,name] of FANS){
    const b = document.createElement("button");
    b.textContent = name;
    if(id === s.fan_id) b.classList.add("on");
    b.onclick = () => setFan(id);
    fans.appendChild(b);
  }
  const t = new Date().toLocaleTimeString();
  el("status").textContent = "Updated " + t + " · " + s.mode + " · fan " + s.fan;
  const fs = el("filterStatus");
  if(s.filter_dirty === true){ fs.textContent = "⚠️ Filter needs cleaning"; fs.className = "filterline warn"; }
  else if(s.filter_dirty === false){ fs.textContent = "Filter: OK"; fs.className = "filterline"; }
  else { fs.textContent = ""; fs.className = "filterline"; }
}

async function call(path, body){
  if(busy) return;
  busy = true;
  el("status").textContent = "Sending...";
  try{
    const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    if(!r.ok){ throw new Error(await r.text()); }
    render(await r.json());
  }catch(e){ el("status").innerHTML = '<span class="offline">'+e.message+'</span>'; }
  finally{ busy = false; }
}

function togglePower(){ if(state) call("/api/power", {on: !state.power_on}); }
function setMode(id){ call("/api/mode", {mode: id}); }
function setFan(id){ call("/api/fan", {speed: id}); }
function bumpTemp(d){
  if(!state || state.setpoint == null) return;
  const lo = state.min_setpoint ?? 16, hi = state.max_setpoint ?? 32;
  const t = Math.min(hi, Math.max(lo, state.setpoint + d));
  if(t === state.setpoint) return;   // already at the limit
  call("/api/setpoint", {temp: t});
}

async function poll(){
  if(!busy){
    try{ const r = await fetch("/api/status"); render(await r.json()); }
    catch(e){ render({connected:false}); }
  }
  setTimeout(poll, 10000);
}
poll();
</script>
</body>
</html>"""


PAGE_HISTORY = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aircon History</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #f2f3f5; color: #1c1c1e; display: flex; justify-content: center; }
  @media (prefers-color-scheme: dark) { body { background:#000; color:#f2f2f7; } .card{background:#1c1c1e !important;} }
  .wrap { width: 100%; max-width: 720px; padding: 20px; }
  h1 { font-size: 20px; margin: 8px 4px 2px; }
  .sub { color:#8a8a8e; font-size: 13px; margin: 0 4px 16px; }
  .card { background:#fff; border-radius:18px; padding:18px; margin-bottom:16px;
          box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .ranges { display:flex; gap:8px; margin-bottom:14px; }
  .ranges button { font:inherit; border:none; border-radius:12px; padding:10px 16px;
                   background:#e5e5ea; color:inherit; cursor:pointer; flex:1; }
  .ranges button.on { background:#0a84ff; color:#fff; }
  .legend { font-size:13px; color:#8a8a8e; margin-bottom:8px; }
  .legend .room::before { content:"\\25cf"; color:#0a84ff; margin-right:4px; }
  .legend .sp::before   { content:"\\25cf"; color:#ff9f0a; margin-right:4px; }
  canvas { width:100%; height:300px; display:block; }
  .empty { text-align:center; color:#8a8a8e; padding:40px 0; }
  .histlink { display:block; text-align:center; margin-top:6px; font-size:14px;
              color:#0a84ff; text-decoration:none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Aircon History</h1>
  <div class="sub" id="device">&nbsp;</div>

  <div class="ranges" id="ranges">
    <button data-h="1">1h</button>
    <button data-h="2">2h</button>
    <button data-h="6">6h</button>
    <button data-h="24" class="on">24h</button>
    <button data-h="72">3d</button>
  </div>

  <div class="card">
    <div class="legend"><span class="room">Room temp</span> &nbsp; <span class="sp">Target</span>
      &nbsp; <span style="opacity:.6">shaded = off &middot; hover for details</span></div>
    <canvas id="chart"></canvas>
    <div class="empty" id="empty" style="display:none">Collecting data - check back in a minute.</div>
  </div>

  <a class="histlink" href="/">‹ Back to controls</a>
</div>

<script>
let rangeH = 24, DATA = [], hoverX = null;
const MODES = {0:"Fan",1:"Dry",2:"Auto",3:"Cool",4:"Heat",5:"Vent"};
const FANS = {0:"Auto",1:"Low",2:"Low-Med",3:"Medium",4:"Med-High",5:"High"};
const el = id => document.getElementById(id);

async function load(){
  try{
    const r = await fetch("/api/history?hours=" + rangeH);
    const j = await r.json();
    const info = j.info || {};
    el("device").textContent = info.model
      ? `${info.model} - controller ${info.controller_version||"?"}, comm ${info.comm_version||"?"}`
      : "\\u00a0";
    DATA = j.t.map((ts,i) => ({ t: ts*1000, room: j.room[i], sp: j.sp[i],
                                power: j.power[i], mode: j.mode[i],
                                fan: j.fan[i], filter: j.filter[i] }))
              .filter(d => d.room != null && d.sp != null);
  }catch(e){ DATA = []; }
  draw();
}

function draw(){
  const cv = el("chart");
  if(DATA.length < 2){ cv.style.display="none"; el("empty").style.display="block"; return; }
  cv.style.display="block"; el("empty").style.display="none";

  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = 300;
  cv.width = W*dpr; cv.height = H*dpr;
  const g = cv.getContext("2d"); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  const padL=36, padR=10, padT=10, padB=34;
  const x0=padL, x1=W-padR, y0=padT, y1=H-padB;
  const t0=DATA[0].t, t1=DATA[DATA.length-1].t;
  let lo=Infinity, hi=-Infinity;
  for(const d of DATA){ lo=Math.min(lo,d.room,d.sp); hi=Math.max(hi,d.room,d.sp); }
  lo=Math.floor(lo-1); hi=Math.ceil(hi+1);
  const grid="rgba(128,128,128,.2)";
  const X=t=>x0+(t-t0)/(t1-t0)*(x1-x0);
  const Y=v=>y1-(v-lo)/(hi-lo)*(y1-y0);

  // shade intervals where the unit was OFF
  g.fillStyle="rgba(128,128,128,.13)";
  for(let i=1;i<DATA.length;i++) if(!DATA[i-1].power){
    const xa=X(DATA[i-1].t); g.fillRect(xa, y0, X(DATA[i].t)-xa, y1-y0);
  }

  g.font="11px system-ui"; g.fillStyle="#8a8a8e"; g.strokeStyle=grid; g.lineWidth=1;
  for(let v=lo; v<=hi; v += (hi-lo)<=8 ? 1 : 2){
    const y=Y(v); g.beginPath(); g.moveTo(x0,y); g.lineTo(x1,y); g.stroke();
    g.fillText(v+"\\u00b0", 6, y+3);
  }
  // nice local-time x ticks (aligned to the clock) with dates
  const MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const spanMin=(t1-t0)/60000;
  const stepMin = spanMin<=90 ? 15 : spanMin<=180 ? 30 : spanMin<=540 ? 60
                : spanMin<=1080 ? 180 : spanMin<=2160 ? 360 : spanMin<=4800 ? 720 : 1440;
  const start=new Date(t0); start.setHours(0,0,0,0);   // local midnight, ticks step from here
  g.textAlign="center"; let prevDay=null;
  for(let k=0;;k++){
    const td=new Date(start); td.setMinutes(k*stepMin); const tk=td.getTime();
    if(tk>t1) break;
    if(tk<t0) continue;
    const x=X(tk);
    g.strokeStyle=grid; g.beginPath(); g.moveTo(x,y0); g.lineTo(x,y1); g.stroke();
    g.fillStyle="#8a8a8e";
    const tx=Math.min(Math.max(x,x0+16),x1-16);
    const hh=td.getHours().toString().padStart(2,"0"), mm=td.getMinutes().toString().padStart(2,"0");
    g.fillText(hh+":"+mm, tx, y1+14);
    const day=td.getDate();
    if(day!==prevDay) g.fillText(day+" "+MON[td.getMonth()], tx, y1+26);
    prevDay=day;
  }
  g.textAlign="left";
  // setpoint stepped line
  g.strokeStyle="#ff9f0a"; g.lineWidth=2; g.beginPath();
  DATA.forEach((d,i)=>{ const x=X(d.t), y=Y(d.sp);
    if(i===0) g.moveTo(x,y); else { g.lineTo(x, Y(DATA[i-1].sp)); g.lineTo(x,y); } });
  g.stroke();
  // room temp line
  g.strokeStyle="#0a84ff"; g.lineWidth=2; g.beginPath();
  DATA.forEach((d,i)=>{ const x=X(d.t), y=Y(d.room); i?g.lineTo(x,y):g.moveTo(x,y); });
  g.stroke();

  // hover: guide line + tooltip with the full state at that moment
  if(hoverX != null){
    const t = t0 + (hoverX - x0)/(x1 - x0)*(t1 - t0);
    let best = DATA[0]; for(const d of DATA) if(Math.abs(d.t-t) < Math.abs(best.t-t)) best = d;
    const x = X(best.t);
    g.strokeStyle=grid; g.lineWidth=1; g.beginPath(); g.moveTo(x,y0); g.lineTo(x,y1); g.stroke();
    g.fillStyle="#0a84ff"; g.beginPath(); g.arc(x,Y(best.room),3,0,7); g.fill();
    g.fillStyle="#ff9f0a"; g.beginPath(); g.arc(x,Y(best.sp),3,0,7); g.fill();
    const d=new Date(best.t);
    const time = d.getHours()+":"+String(d.getMinutes()).padStart(2,"0");
    const l1 = `${time}  ${best.room}\\u00b0  target ${best.sp}\\u00b0`;
    let l2 = `${best.power ? "On" : "Off"} \\u00b7 ${MODES[best.mode]||"?"} \\u00b7 fan ${FANS[best.fan]||"?"}`;
    if(best.filter) l2 += " \\u00b7 filter!";
    g.font="12px system-ui";
    const tw = Math.max(g.measureText(l1).width, g.measureText(l2).width) + 10;
    const bx = Math.min(Math.max(x - tw/2, x0), x1 - tw);
    g.fillStyle="rgba(0,0,0,.78)"; g.fillRect(bx, y0, tw, 34);
    g.fillStyle="#fff"; g.fillText(l1, bx+5, y0+14); g.fillText(l2, bx+5, y0+28);
  }
  cv.onmousemove = ev => { const r=cv.getBoundingClientRect(); hoverX = ev.clientX - r.left; draw(); };
  cv.onmouseleave = () => { hoverX = null; draw(); };
}

el("ranges").addEventListener("click", e=>{
  if(e.target.tagName!=="BUTTON") return;
  document.querySelectorAll("#ranges button").forEach(b=>b.classList.remove("on"));
  e.target.classList.add("on");
  rangeH = +e.target.dataset.h;
  load();
});
window.addEventListener("resize", draw);
load();
setInterval(load, 60000);
</script>
</body>
</html>"""


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


def main():
    p = argparse.ArgumentParser(description="Shared web control for a Daikin BRC1H.")
    p.add_argument("--address", help="BLE address / CoreBluetooth UUID of the controller")
    p.add_argument("--host", default="0.0.0.0", help="bind host (default 0.0.0.0 = all interfaces)")
    p.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    p.add_argument("--db", default="aircon_history.db",
                   help="SQLite history file (default aircon_history.db)")
    p.add_argument("--selftest", action="store_true", help="run offline encoder checks and exit")
    args = p.parse_args()

    if args.selftest:
        raise SystemExit(selftest())

    if not args.address:
        p.error("--address is required (get it from: python brc1h_spike.py --scan)")

    import uvicorn
    controller = Controller(args.address)
    store = HistoryStore(args.db)
    app = build_app(controller, store)
    print(f"\nOffice Aircon server starting.")
    print(f"Open http://<this-mac-ip>:{args.port}  (find the IP with: ipconfig getifaddr en0)\n")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
