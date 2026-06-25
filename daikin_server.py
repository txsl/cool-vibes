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
# Command (write) function ids
CMD_SET_POWER    = 0x4020
CMD_SET_MODE     = 0x4030
CMD_SET_SETPOINT = 0x4040
CMD_SET_FAN      = 0x4050

MODE_NAMES = {0: "Fan", 1: "Dry", 2: "Auto", 3: "Cool", 4: "Heat", 5: "Ventilation"}
MODE_IDS = {v.lower(): k for k, v in MODE_NAMES.items()}
FAN_NAMES = {1: "Low", 2: "Low-Med", 3: "Medium", 4: "Med-High", 5: "High"}


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
        is_heat = mode_id == 4
        cool_sp = temp_from_bytes(setp[0x20]) if 0x20 in setp else None
        heat_sp = temp_from_bytes(setp[0x21]) if 0x21 in setp else None
        fan_cool = fan.get(0x20, b"\x00")[0]
        fan_heat = fan.get(0x21, b"\x00")[0]

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
            "room_temp": room,
            "outdoor_temp": outdoor,
            "fan_id": fan_heat if is_heat else fan_cool,
            "fan": FAN_NAMES.get(fan_heat if is_heat else fan_cool, "?"),
        }

    # ---- writes ------------------------------------------------------------
    async def set_power(self, on: bool):
        await self.request(CMD_SET_POWER, [(0x20, bytes([1 if on else 0]))])

    async def set_mode(self, mode_id: int):
        await self.request(CMD_SET_MODE, [(0x20, bytes([mode_id]))])

    async def set_setpoint(self, celsius: float):
        v = temp_to_bytes(celsius)
        await self.request(CMD_SET_SETPOINT, [(0x20, v), (0x21, v)])

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
# Web app
# ----------------------------------------------------------------------------
def build_app(controller: "Controller"):
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

    async def safe_status():
        try:
            return await controller.get_status()
        except Exception as e:
            log.warning("status read failed: %s", e)
            return {"connected": False, "error": str(e)}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(PAGE)

    @app.get("/api/status")
    async def status():
        return JSONResponse(await safe_status())

    @app.post("/api/power")
    async def power(body: PowerBody):
        await controller.set_power(body.on)
        return JSONResponse(await safe_status())

    @app.post("/api/mode")
    async def mode(body: ModeBody):
        if body.mode not in MODE_NAMES:
            raise HTTPException(400, "invalid mode")
        await controller.set_mode(body.mode)
        return JSONResponse(await safe_status())

    @app.post("/api/setpoint")
    async def setpoint(body: SetpointBody):
        if not (16.0 <= body.temp <= 32.0):
            raise HTTPException(400, "temp out of range (16-32C)")
        await controller.set_setpoint(body.temp)
        return JSONResponse(await safe_status())

    @app.post("/api/fan")
    async def fan(body: FanBody):
        if not (1 <= body.speed <= 5):
            raise HTTPException(400, "invalid fan speed")
        await controller.set_fan(body.speed)
        return JSONResponse(await safe_status())

    @app.on_event("shutdown")
    async def _shutdown():
        log.info("Shutting down - disconnecting from controller cleanly ...")
        try:
            await controller.disconnect()
            log.info("Disconnected cleanly. Safe to close this window now.")
        except Exception as e:
            log.warning("Disconnect hit an error: %s", e)

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
        <button class="round" onclick="bumpTemp(-0.5)">-</button>
        <span class="val" id="setpoint">--</span>
        <button class="round" onclick="bumpTemp(0.5)">+</button>
      </div>
    </div>

    <div class="row"><span class="label">Mode</span></div>
    <div class="seg" id="modes"></div>

    <div class="row" style="margin-top:18px;"><span class="label">Fan</span></div>
    <div class="seg" id="fans"></div>
  </div>

  <div class="status" id="status"></div>
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
    controls.classList.add("dim");
    return;
  }
  controls.classList.remove("dim");
  el("room").textContent = (s.room_temp ?? "--") + "°";
  const p = el("power");
  p.textContent = s.power_on ? "On" : "Off";
  p.className = "power " + (s.power_on ? "on" : "off");
  el("setpoint").textContent = (s.setpoint ?? "--") + "°";

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
  const t = Math.min(32, Math.max(16, state.setpoint + d));
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

    # SetSetpoint(22.0): func 0x4040, 0x20 + 0x21 each 2 bytes = 22*128 = 0x0B00
    sp = temp_to_bytes(22.0)
    check("temp 22.0C -> bytes", sp, bytes([0x0B, 0x00]))
    check("SetSetpoint 22.0", bytes(build_command(CMD_SET_SETPOINT, [(0x20, sp), (0x21, sp)])[0]),
          bytes([0x00, 0x0C, 0x00, 0x40, 0x40, 0x20, 0x02, 0x0B, 0x00, 0x21, 0x02, 0x0B, 0x00]))

    # SetFanSpeed(High=5): func 0x4050, 0x20 + 0x21 each size1 = 5
    check("SetFan High", bytes(build_command(CMD_SET_FAN, [(0x20, bytes([5])), (0x21, bytes([5]))])[0]),
          bytes([0x00, 0x0A, 0x00, 0x40, 0x50, 0x20, 0x01, 0x05, 0x21, 0x01, 0x05]))

    # round-trip temperature decode
    check("temp decode 22.0", temp_from_bytes(bytes([0x0B, 0x00])), 22.0)

    print("\nSelf-test:", "ALL PASSED" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="Shared web control for a Daikin BRC1H.")
    p.add_argument("--address", help="BLE address / CoreBluetooth UUID of the controller")
    p.add_argument("--host", default="0.0.0.0", help="bind host (default 0.0.0.0 = all interfaces)")
    p.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    p.add_argument("--selftest", action="store_true", help="run offline encoder checks and exit")
    args = p.parse_args()

    if args.selftest:
        raise SystemExit(selftest())

    if not args.address:
        p.error("--address is required (get it from: python brc1h_spike.py --scan)")

    import uvicorn
    controller = Controller(args.address)
    app = build_app(controller)
    print(f"\nOffice Aircon server starting.")
    print(f"Open http://<this-mac-ip>:{args.port}  (find the IP with: ipconfig getifaddr en0)\n")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
