"""Shared BLE protocol for the Daikin BRC1H ("Madoka") controller.

Pure, hardware-agnostic constants and encode/decode helpers used by both
daikin_server.py (the web bridge) and brc1h_spike.py (the connectivity tester).
No Bluetooth or I/O here - everything is covered by the pytest suite and by each
script's --selftest.

Protocol: emulated UART-over-BLE, reverse-engineered by Benjamin Lafois and
codified in pymadoka.
"""
import math

# ----------------------------------------------------------------------------
# GATT service / characteristics (BRC1H AC-management service)
# ----------------------------------------------------------------------------
SERVICE_UUID = "2141e110-213a-11e6-b67b-9e71128cae77"
NOTIFY_CHAR  = "2141e111-213a-11e6-b67b-9e71128cae77"  # RX (device -> us)
WRITE_CHAR   = "2141e112-213a-11e6-b67b-9e71128cae77"  # TX (us -> device)

MAX_CHUNK_SIZE = 20
CHUNK_DATA_LEN = 19

# Query (read) function ids
CMD_GET_POWER       = 0x0020
CMD_GET_MODE        = 0x0030
CMD_GET_SETPOINT    = 0x0040
CMD_GET_FAN         = 0x0050
CMD_GET_FILTER      = 0x0100  # clean-filter indicator (field 0x62, bit 0)
CMD_GET_SENSOR      = 0x0110
CMD_GET_MAINTENANCE = 0x0130  # model + firmware versions
# Command (write) function ids
CMD_SET_POWER    = 0x4020
CMD_SET_MODE     = 0x4030
CMD_SET_SETPOINT = 0x4040
CMD_SET_FAN      = 0x4050

MODE_NAMES = {0: "Fan", 1: "Dry", 2: "Auto", 3: "Cool", 4: "Heat", 5: "Ventilation"}
MODE_IDS = {v.lower(): k for k, v in MODE_NAMES.items()}
MODE_HEAT = 4
FAN_NAMES = {1: "Low", 2: "Low-Med", 3: "Medium", 4: "Med-High", 5: "High"}

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
# Encode / decode (pure)
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


def build_query(cmd_id: int) -> list:
    """A no-argument read query (alias for build_command with no args)."""
    return build_command(cmd_id)


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
