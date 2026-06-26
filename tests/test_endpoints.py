"""Endpoint tests via FastAPI TestClient with a stub controller (no BLE).

The stub's refresh() returns a disconnected snapshot so the background poller
launched at startup is a harmless no-op during tests; /api/status is asserted
against the separately-set cache (controller.latest).
"""
import time
import pytest
from fastapi.testclient import TestClient
import daikin_server as d

CANNED = {"connected": True, "power_on": True, "mode_id": 3, "mode": "Cool",
          "setpoint": 22.0, "cooling_setpoint": 22.0, "heating_setpoint": 22.0,
          "min_setpoint": 16.0, "max_setpoint": 32.0, "room_temp": 22,
          "outdoor_temp": None, "fan_id": 3, "fan": "Medium", "filter_dirty": False}


class StubController:
    def __init__(self):
        self.latest = dict(CANNED)
        self.device_info = {"model": "X"}   # non-empty so the poller skips the read
        self.filter_dirty = False
        self.calls = []

    async def refresh(self):
        return {"connected": False}         # keeps the poller from logging/reading

    async def read_device_info(self):
        return self.device_info

    async def read_filter(self):
        return self.filter_dirty

    async def set_power(self, on):
        self.calls.append(("power", on))

    async def set_mode(self, m):
        self.calls.append(("mode", m))

    async def set_setpoint(self, t):
        self.calls.append(("setpoint", t))

    async def set_fan(self, s):
        self.calls.append(("fan", s))

    async def disconnect(self):
        pass


@pytest.fixture
def client(tmp_path):
    store = d.HistoryStore(str(tmp_path / "h.db"))
    ctrl = StubController()
    app = d.build_app(ctrl, store)
    with TestClient(app) as c:
        c.ctrl = ctrl
        c.store = store
        yield c


def test_status_serves_cache(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json() == CANNED


def test_history_shape_and_active_setpoint(client):
    now = int(time.time())
    client.store.insert_sample(now - 10, {"power_on": True, "mode_id": 3, "cooling_setpoint": 24.0,
        "heating_setpoint": 20.0, "room_temp": 23, "outdoor_temp": None, "fan_id": 3, "filter_dirty": False})
    client.store.insert_sample(now - 5, {"power_on": False, "mode_id": 4, "cooling_setpoint": 24.0,
        "heating_setpoint": 27.0, "room_temp": 22, "outdoor_temp": None, "fan_id": 1, "filter_dirty": True})
    j = client.get("/api/history?hours=1").json()
    assert j["t"] == [now - 10, now - 5]
    assert j["sp"] == [24.0, 27.0]          # Cool row -> cooling, Heat row -> heating
    assert j["room"] == [23, 22]
    assert j["power"] == [1, 0]
    assert j["mode"] == [3, 4]
    assert j["fan"] == [3, 1]
    assert j["filter"] == [0, 1]


def test_setpoint_in_range_calls_controller(client):
    r = client.post("/api/setpoint", json={"temp": 23})
    assert r.status_code == 200
    assert ("setpoint", 23) in client.ctrl.calls


def test_setpoint_out_of_range_rejected(client):
    r = client.post("/api/setpoint", json={"temp": 99})
    assert r.status_code == 400
    assert all(c[0] != "setpoint" for c in client.ctrl.calls)   # never reached the controller


def test_fan_validation(client):
    assert client.post("/api/fan", json={"speed": 6}).status_code == 400
    assert client.post("/api/fan", json={"speed": 3}).status_code == 200
    assert ("fan", 3) in client.ctrl.calls


def test_mode_validation(client):
    assert client.post("/api/mode", json={"mode": 99}).status_code == 400
    assert client.post("/api/mode", json={"mode": 3}).status_code == 200
