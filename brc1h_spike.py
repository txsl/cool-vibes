#!/usr/bin/env python3
"""
brc1h_spike.py - Connectivity test for a Daikin BRC1H "Madoka" controller from macOS.

PURPOSE
    Prove that this Mac (e.g. an office Mac mini) can:
      1. discover the BRC1H over Bluetooth Low Energy,
      2. connect to it,
      3. subscribe to its notify characteristic, and
      4. run a few READ-ONLY queries (power state, mode, setpoint, indoor temp).

    If this works, the whole stack (BLE bridge + web app) can live on one Mac.
    If macOS pairing fights us, fall back to a small Raspberry Pi running pymadoka.

    This script is READ-ONLY. It never changes a setting on the unit.

PROTOCOL
    Emulated UART-over-BLE, reverse-engineered by Benjamin Lafois
    (github.com/blafois/Daikin-Madoka-BRC1H-BLE-Reverse) and codified in pymadoka
    (github.com/mduran80/pymadoka). Two GATT characteristics on the AC-management
    service act as RX (Notify) and TX (WriteWithoutResponse). Messages are framed
    in <=20-byte chunks and reassembled.

USAGE
    pip3 install bleak
    python3 brc1h_spike.py                  # scan, auto-detect a BRC1H, then query it
    python3 brc1h_spike.py --scan           # just list nearby BLE devices and exit
    python3 brc1h_spike.py --name Office    # match the controller by name substring
    python3 brc1h_spike.py --address <UUID> # connect to a specific CoreBluetooth UUID
    python3 brc1h_spike.py --selftest       # offline check of encode/decode (no BLE)

macOS NOTES
    * On macOS, BLE peripherals are identified by a CoreBluetooth UUID, not a MAC
      address. Use --scan to see the UUID, then pass it with --address if needed.
    * If the controller is currently connected to a phone running the Madoka app,
      it STOPS advertising and you won't find it. Close the app / forget the device
      on the phone first.
    * The first connection may trigger a macOS pairing prompt; accept it. A PIN may
      appear on the controller's screen.
    * Clean disconnect matters: an improper termination has been reported to wedge
      the controller (needing a full AC power-cycle). This script always tries to
      stop notifications and disconnect cleanly in a finally block.
"""

import argparse
import asyncio
import sys

# ----------------------------------------------------------------------------
# Protocol constants (BRC1H AC-management service)
# ----------------------------------------------------------------------------
SERVICE_UUID = "2141e110-213a-11e6-b67b-9e71128cae77"
NOTIFY_CHAR  = "2141e111-213a-11e6-b67b-9e71128cae77"  # RX (device -> us)
WRITE_CHAR   = "2141e112-213a-11e6-b67b-9e71128cae77"  # TX (us -> device)

MAX_CHUNK_SIZE = 20          # device limit, bytes per BLE packet
CHUNK_DATA_LEN = 19          # 20 - 1 byte chunk index

# Read-only function IDs we will query (name -> 16-bit command id)
QUERIES = {
    "Power (on/off)":   0x0020,  # GetSettingStatus
    "Operation mode":   0x0030,  # GetOperationMode
    "Setpoint":         0x0040,  # GetSetpoint
    "Indoor temp":      0x0110,  # GetSensorInformation
}

MODE_NAMES = {0: "Fan", 1: "Dry", 2: "Auto", 3: "Cool", 4: "Heat", 5: "Ventilation"}


# ----------------------------------------------------------------------------
# Pure protocol helpers (no Bluetooth required - covered by --selftest)
# ----------------------------------------------------------------------------
def build_query(cmd_id: int) -> list:
    """Build the on-wire chunk(s) for a no-argument query of `cmd_id`.

    Payload layout (before chunking):
        [total_len][0x00][cmd_hi][cmd_lo][arg_id=0x00][arg_size=0x00]
    `total_len` counts itself plus everything after it. Then the payload is split
    into <=19-byte pieces, each prefixed with a chunk index byte.
    """
    payload = bytearray([0x00, 0x00]) + cmd_id.to_bytes(2, "big") + bytearray([0x00, 0x00])
    payload[0] = len(payload)
    return split_in_chunks(payload)


def split_in_chunks(data: bytearray) -> list:
    """Split `data` into chunks of <=19 data bytes, each prefixed with its index."""
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
    """Reassembles inbound chunks into a full payload, mirroring pymadoka's Transport."""

    def __init__(self):
        self.chunks = []

    def feed(self, chunk: bytearray):
        """Add a chunk; return the reassembled payload bytes if complete, else None."""
        if len(chunk) < 2:
            return None
        chunk_id = chunk[0]
        if chunk_id == 0:
            self.chunks = []  # start of a new message
        self.chunks.append(chunk)
        total_len = self.chunks[0][1]
        expected = -(-total_len // MAX_CHUNK_SIZE)  # ceil
        if len(self.chunks) == expected:
            out = bytearray()
            for c in self.chunks:
                out.extend(c[1:])  # drop the chunk index byte
            self.chunks = []
            return out
        return None


def parse_objects(payload: bytearray) -> dict:
    """Parse a reassembled response payload into {object_id: value_bytes}.

    Layout: [total_len][0x00][cmd_hi][cmd_lo] then repeating [id][size][value...].
    """
    objects = {}
    i = 4  # skip length, 0x00, and the 2-byte command id
    while i + 1 < len(payload):
        oid = payload[i]
        size = payload[i + 1]
        value = bytes(payload[i + 2:i + 2 + size])
        if len(value) < size:
            break
        objects[oid] = value
        i += 2 + size
    return objects


def cmd_id_of(payload: bytearray) -> int:
    """Extract the 16-bit command id from a reassembled response payload."""
    return int.from_bytes(payload[2:4], "big")


def interpret(cmd_id: int, objects: dict) -> str:
    """Turn parsed objects into a human-readable line for a known query."""
    if cmd_id == 0x0020 and 0x20 in objects:
        return "ON" if objects[0x20][0] else "OFF"
    if cmd_id == 0x0030 and 0x20 in objects:
        return MODE_NAMES.get(objects[0x20][0], f"unknown ({objects[0x20][0]})")
    if cmd_id == 0x0040:
        parts = []
        if 0x20 in objects:
            parts.append(f"cooling {int.from_bytes(objects[0x20], 'big') / 128.0:.1f}C")
        if 0x21 in objects:
            parts.append(f"heating {int.from_bytes(objects[0x21], 'big') / 128.0:.1f}C")
        return ", ".join(parts) if parts else "(no setpoint fields)"
    if cmd_id == 0x0110:
        parts = []
        if 0x40 in objects:
            parts.append(f"indoor {int.from_bytes(objects[0x40], 'big', signed=True)}C")
        if 0x41 in objects:
            raw = objects[0x41]
            if raw and raw[0] == 0xFF:
                parts.append("outdoor n/a")
            else:
                parts.append(f"outdoor {int.from_bytes(raw, 'big', signed=True)}C")
        return ", ".join(parts) if parts else "(no temperature fields)"
    return f"objects: {{ {', '.join(f'0x{k:02x}={v.hex()}' for k, v in objects.items())} }}"


# ----------------------------------------------------------------------------
# Offline self-test (run with --selftest, no hardware needed)
# ----------------------------------------------------------------------------
def selftest() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        passed = got == want
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        if not passed:
            print(f"        got : {got}")
            print(f"        want: {want}")

    print("Encode:")
    check("query 0x0040 (setpoint)", bytes(build_query(0x0040)[0]),
          bytes([0x00, 0x06, 0x00, 0x00, 0x40, 0x00, 0x00]))
    check("query 0x0110 (sensor)", bytes(build_query(0x0110)[0]),
          bytes([0x00, 0x06, 0x00, 0x01, 0x10, 0x00, 0x00]))

    print("Decode (single chunk, sensor info: indoor 24C, outdoor n/a):")
    body = bytes([0x40, 0x01, 24, 0x41, 0x02, 0xFF, 0xFF])
    payload = bytes([1 + 3 + len(body), 0x00, 0x01, 0x10]) + body
    r = Reassembler()
    out = r.feed(bytearray([0x00]) + payload)
    check("reassembled equals payload", bytes(out), payload)
    check("cmd id", cmd_id_of(out), 0x0110)
    check("interpret", interpret(0x0110, parse_objects(out)), "indoor 24C, outdoor n/a")

    print("Decode (two chunks, setpoint cooling/heating 22.0C):")
    sp_body = bytes([0x20, 0x02]) + (22 * 128).to_bytes(2, "big") + \
              bytes([0x21, 0x02]) + (22 * 128).to_bytes(2, "big") + \
              bytes([0x30, 0x01, 0x00, 0x31, 0x01, 0x00, 0x32, 0x01, 0x00])  # pad to force 2 chunks
    sp_payload = bytes([1 + 3 + len(sp_body), 0x00, 0x00, 0x40]) + sp_body
    chunks = split_in_chunks(bytearray(sp_payload))
    check("forced into 2 chunks", len(chunks), 2)
    r2 = Reassembler()
    out2 = None
    for ch in chunks:
        out2 = r2.feed(ch) or out2
    check("two-chunk reassembly", bytes(out2), sp_payload)
    check("interpret setpoint", interpret(0x0040, parse_objects(out2)),
          "cooling 22.0C, heating 22.0C")

    print("\nSelf-test:", "ALL PASSED" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# BLE driver (requires `pip3 install bleak`)
# ----------------------------------------------------------------------------
async def scan_devices(timeout=8.0):
    from bleak import BleakScanner

    print(f"Scanning for {timeout:.0f}s ... (controllers must NOT be connected to a phone)")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    rows = []
    for dev, adv in found.values():
        svcs = [u.lower() for u in (adv.service_uuids or [])]
        rows.append((dev, adv, SERVICE_UUID in svcs))
    # Strongest signal first = nearest = most likely the unit in the room you're in.
    return sorted(rows, key=lambda r: -(r[1].rssi if r[1].rssi is not None else -999))


def print_devices(rows):
    print(f"\n{'RSSI':>5}  {'BRC1H?':<7} {'Name':<22} Address (pass to --address)")
    print("-" * 78)
    for dev, adv, is_brc in rows:
        rssi = adv.rssi if adv.rssi is not None else 0
        print(f"{rssi:>5}  {'YES' if is_brc else '-':<7} {str(dev.name):<22} {dev.address}")
    print("\nHigher RSSI (top) = closer. Stand next to the OFFICE unit and the strongest")
    print("'YES' row is almost certainly it. Lock onto it with: --address <that address>")


async def find_device(name_filter=None, address=None, timeout=8.0):
    rows = await scan_devices(timeout)

    if address:
        for dev, adv, _ in rows:
            if dev.address.lower() == address.lower():
                print(f"Selected: {dev.address}  name={dev.name!r}  rssi={adv.rssi}")
                return dev
        print(f"No device with address {address} found this scan. Seen:")
        print_devices(rows)
        return None

    candidates = [r for r in rows if r[2]]  # advertise the BRC1H service
    if name_filter:
        nf = name_filter.lower()
        candidates = [r for r in rows if r[0].name and nf in r[0].name.lower()]

    if not candidates:
        print("\nNo BRC1H detected. All devices seen:")
        print_devices(rows)
        return None

    if len(candidates) > 1:
        print(f"\nFound {len(candidates)} matching controllers - too many to auto-pick.")
        print("Choose the office unit by signal strength and re-run with --address:")
        print_devices(candidates)
        return None

    dev, adv, _ = candidates[0]
    print(f"Selected: {dev.address}  name={dev.name!r}  rssi={adv.rssi}")
    return dev


async def connect_and_subscribe(client, on_notify, pair_window=120.0):
    """Connect, bond if needed, and subscribe - tolerating the macOS pairing flow.

    The BRC1H requires an encrypted link before it permits notifications. On macOS
    the first encrypted operation triggers an OS pairing request that appears as a
    NOTIFICATION BANNER (top-right), not a modal dialog, while the controller shows
    a PIN. Two quirks we handle:
      * bleak raises "Encryption is insufficient" instantly instead of waiting for
        you to accept -> we retry for up to `pair_window` seconds.
      * completing the bond TEARS DOWN the current link, so the next call fails with
        "Not connected" -> we reconnect and try again. The bond itself persists, so
        once paired this converges quickly (and future runs skip the prompt).
    """
    deadline = asyncio.get_event_loop().time() + pair_window
    announced = False
    while True:
        if not client.is_connected:
            try:
                await client.connect()
            except Exception as e:
                if asyncio.get_event_loop().time() >= deadline:
                    raise
                print(f"    ...connect retry ({e}); waiting 2s")
                await asyncio.sleep(2.0)
                continue

        try:
            await client.start_notify(NOTIFY_CHAR, on_notify)
            print("Notifications enabled (link is bonded).")
            return
        except Exception as e:
            msg = str(e)
            timed_out = asyncio.get_event_loop().time() >= deadline

            # Clear any half-registered subscription left by a failed attempt, so the
            # next try doesn't trip over "notifications already started".
            try:
                await client.stop_notify(NOTIFY_CHAR)
            except Exception:
                pass

            if "already started" in msg:
                # Stale bleak state from a prior failure - cleared above, retry now.
                await asyncio.sleep(0.5)
            elif "Not connected" in msg:
                # The bond completing dropped the link - reconnect and retry.
                print("    ...link dropped during pairing; reconnecting")
                await asyncio.sleep(2.0)
            elif "ncryption" in msg or "nsufficient" in msg:
                if not announced:
                    print("\n>>> PAIRING REQUIRED <<<")
                    print("    1. On the controller's Bluetooth menu, start pairing so it")
                    print("       shows a PIN.")
                    print("    2. Look TOP-RIGHT on the Mac for a 'Bluetooth Pairing Request'")
                    print("       banner (also check Notification Center / the clock).")
                    print("    3. Enter the PIN, click Pair. This script keeps retrying.\n")
                    announced = True
                print("    ...waiting for pairing to complete, retrying in 3s")
                await asyncio.sleep(3.0)
            else:
                raise  # an unexpected failure - surface it

            if timed_out:
                raise TimeoutError(
                    "Could not establish a bonded connection in time. If NO macOS "
                    "pairing banner ever appeared, this Mac won't bond with the "
                    "controller cleanly - use the Raspberry Pi bridge route instead."
                )


async def run_queries(dev):
    from bleak import BleakClient

    loop = asyncio.get_event_loop()
    reasm = Reassembler()
    pending = {}  # cmd_id -> Future

    def on_notify(_sender, data: bytearray):
        payload = reasm.feed(bytearray(data))
        if payload is None or len(payload) <= 4:
            return
        cid = cmd_id_of(payload)
        fut = pending.get(cid)
        if fut and not fut.done():
            fut.set_result(payload)

    print(f"Connecting to {dev.address} ...")
    client = BleakClient(dev)
    try:
        await connect_and_subscribe(client, on_notify)
        for label, cmd_id in QUERIES.items():
            fut = loop.create_future()
            pending[cmd_id] = fut
            for chunk in build_query(cmd_id):
                await client.write_gatt_char(WRITE_CHAR, bytes(chunk), response=False)
            try:
                payload = await asyncio.wait_for(fut, timeout=5.0)
                print(f"  {label:<16}: {interpret(cmd_id, parse_objects(payload))}")
            except asyncio.TimeoutError:
                print(f"  {label:<16}: (no response within 5s)")
            finally:
                pending.pop(cmd_id, None)
    finally:
        print("Cleaning up (stop notify + disconnect) ...")
        try:
            if client.is_connected:
                await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass
    print("Done. Connection closed cleanly.")


async def amain(args):
    if args.scan:
        rows = await scan_devices(timeout=args.timeout)
        print_devices(rows)  # show EVERYTHING, don't auto-select
        return 0
    dev = await find_device(name_filter=args.name, address=args.address, timeout=args.timeout)
    if dev is None:
        return 2
    await run_queries(dev)
    return 0


def main():
    p = argparse.ArgumentParser(description="Read-only BLE connectivity test for a Daikin BRC1H (Madoka).")
    p.add_argument("--scan", action="store_true", help="just list nearby BLE devices and exit")
    p.add_argument("--name", help="match the controller by a substring of its advertised name")
    p.add_argument("--address", help="connect to a specific address / CoreBluetooth UUID")
    p.add_argument("--timeout", type=float, default=8.0, help="scan duration in seconds (default 8)")
    p.add_argument("--selftest", action="store_true", help="run offline encode/decode checks (no Bluetooth)")
    args = p.parse_args()

    if args.selftest:
        sys.exit(selftest())

    try:
        sys.exit(asyncio.run(amain(args)))
    except ImportError:
        print("This needs the 'bleak' package. Install it with:\n    pip3 install bleak")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
