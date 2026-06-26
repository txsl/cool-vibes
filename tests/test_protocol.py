"""Protocol encode/decode tests.

These assert ground-truth values (known-correct on-wire bytes from the
reverse-engineering / pymadoka, and the device responses captured from the real
unit), so they validate behaviour independently of the implementation. They
import from the modules where the helpers live today; after the protocol helpers
are factored into a shared module, those modules re-export the same names, so
these tests stay valid unchanged.
"""
import daikin_server as d
import brc1h_spike as spike


# --- command framing (server) ---------------------------------------------
def test_getsetpoint_query_framing():
    assert bytes(d.build_command(d.CMD_GET_SETPOINT)[0]) == \
        bytes([0x00, 0x06, 0x00, 0x00, 0x40, 0x00, 0x00])


def test_setpower_on_framing():
    assert bytes(d.build_command(d.CMD_SET_POWER, [(0x20, bytes([1]))])[0]) == \
        bytes([0x00, 0x07, 0x00, 0x40, 0x20, 0x20, 0x01, 0x01])


def test_setmode_cool_framing():
    assert bytes(d.build_command(d.CMD_SET_MODE, [(0x20, bytes([3]))])[0]) == \
        bytes([0x00, 0x07, 0x00, 0x40, 0x30, 0x20, 0x01, 0x03])


def test_setfan_high_framing():
    assert bytes(d.build_command(d.CMD_SET_FAN, [(0x20, bytes([5])), (0x21, bytes([5]))])[0]) == \
        bytes([0x00, 0x0A, 0x00, 0x40, 0x50, 0x20, 0x01, 0x05, 0x21, 0x01, 0x05])


# --- temperature codec ------------------------------------------------------
def test_temp_to_bytes():
    assert d.temp_to_bytes(22.0) == bytes([0x0B, 0x00])
    assert d.temp_to_bytes(24.0) == bytes([0x0C, 0x00])


def test_temp_from_bytes_roundtrip():
    assert d.temp_from_bytes(bytes([0x0B, 0x00])) == 22.0
    assert d.temp_from_bytes(d.temp_to_bytes(21.0)) == 21.0


def test_round_setpoint():
    assert d.round_setpoint(21.0) == 21
    assert d.round_setpoint(21.4) == 21
    assert d.round_setpoint(21.5) == 22   # half rounds up
    assert d.round_setpoint(20.5) == 21


# --- full SetSetpoint field block ------------------------------------------
def test_setpoint_args_full_block():
    args = d.build_setpoint_args(22.0, 22.0)
    assert len(args) == 2 + len(d.SETPOINT_EXTRA_FIELDS)
    # reassemble all chunks and parse back into objects
    payload = bytearray()
    for chunk in d.build_command(d.CMD_SET_SETPOINT, args):
        payload += chunk[1:]
    assert payload[0] == len(payload)              # length byte counts itself
    assert d.cmd_id_of(payload) == d.CMD_SET_SETPOINT
    objs = d.parse_objects(payload)
    assert objs[0x20] == bytes([0x0B, 0x00])       # cooling 22C
    assert objs[0x21] == bytes([0x0B, 0x00])       # heating 22C
    assert objs[0x31] == bytes([0x02])             # setpoint mode hardcoded 2
    assert objs[0xB2] == bytes([0x00, 0x00])       # a zeroed limit field
    # every extra field is present
    for arg_id, _size, _val in d.SETPOINT_EXTRA_FIELDS:
        assert arg_id in objs


def test_setpoint_independent_cool_heat():
    args = dict(d.build_setpoint_args(20.0, 27.0))
    assert args[0x20] == d.temp_to_bytes(20.0)
    assert args[0x21] == d.temp_to_bytes(27.0)


# --- chunking + reassembly --------------------------------------------------
def test_single_chunk_reassembly():
    body = bytes([0x40, 0x01, 24, 0x41, 0x02, 0xFF, 0xFF])
    payload = bytes([1 + 3 + len(body), 0x00, 0x01, 0x10]) + body
    r = d.Reassembler()
    out = r.feed(bytearray([0x00]) + payload)
    assert bytes(out) == payload
    assert d.cmd_id_of(out) == 0x0110


def test_multi_chunk_reassembly():
    sp_body = (bytes([0x20, 0x02]) + (22 * 128).to_bytes(2, "big")
               + bytes([0x21, 0x02]) + (22 * 128).to_bytes(2, "big")
               + bytes([0x30, 0x01, 0x00, 0x31, 0x01, 0x00, 0x32, 0x01, 0x00]))
    payload = bytes([1 + 3 + len(sp_body), 0x00, 0x00, 0x40]) + sp_body
    chunks = d.split_in_chunks(bytearray(payload))
    assert len(chunks) == 2
    r = d.Reassembler()
    out = None
    for ch in chunks:
        out = r.feed(ch) or out
    assert bytes(out) == payload


def test_parse_objects():
    body = bytes([0x40, 0x01, 24, 0x41, 0x02, 0xFF, 0xFF])
    payload = bytes([1 + 3 + len(body), 0x00, 0x01, 0x10]) + body
    objs = d.parse_objects(payload)
    assert objs == {0x40: bytes([24]), 0x41: bytes([0xFF, 0xFF])}


# --- device info parsing (captured from the real unit) ---------------------
def test_parse_device_info_from_captured_response():
    di = d.parse_device_info({
        0x40: bytes.fromhex("00000000000000000000000000000000465846513332415645420000000000"
                            "4139502f303238000000000000000000"),
        0x45: bytes([0x03, 0x06, 0x00]),
        0x46: bytes([0x05, 0x11]),
    })
    assert di["model"] == "FXFQ32AVEB"
    assert di["model_aux"] == "A9P/028"
    assert di["controller_version"] == "3.6.0"
    assert di["comm_version"] == "5.17"


def test_parse_device_info_empty():
    di = d.parse_device_info({})
    assert di == {"model": None, "model_aux": None,
                  "controller_version": None, "comm_version": None}


# --- spike helpers (locked so the refactor preserves them too) -------------
def test_spike_build_query():
    assert bytes(spike.build_query(0x0040)[0]) == \
        bytes([0x00, 0x06, 0x00, 0x00, 0x40, 0x00, 0x00])


def test_spike_interpret_sensor():
    payload = bytes([0x00, 0x01, 0x10]) + bytes([0x40, 0x01, 24, 0x41, 0x02, 0xFF, 0xFF])
    payload = bytes([len(payload) + 1]) + payload
    assert spike.interpret(0x0110, spike.parse_objects(payload)) == "indoor 24C, outdoor n/a"
