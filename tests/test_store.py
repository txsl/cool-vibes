"""HistoryStore tests: insert/query, device info, and the on-disk migration."""
import sqlite3
import daikin_server as d

SAMPLE = {"power_on": True, "mode_id": 3, "cooling_setpoint": 24.0,
          "heating_setpoint": 24.0, "room_temp": 23, "outdoor_temp": None,
          "fan_id": 3, "filter_dirty": False}


def test_insert_and_query(tmp_path):
    store = d.HistoryStore(str(tmp_path / "h.db"))
    store.insert_sample(1000, SAMPLE)
    store.insert_sample(1060, {**SAMPLE, "room_temp": 24, "filter_dirty": True})
    rows = store.query_since(0)
    assert len(rows) == 2
    assert rows[0] == (1000, 1, 3, 24.0, 24.0, 23, None, 3, 0)
    assert rows[1][-1] == 1      # filter_dirty True -> 1
    store.close()


def test_query_since_filters_by_time(tmp_path):
    store = d.HistoryStore(str(tmp_path / "h.db"))
    store.insert_sample(100, SAMPLE)
    store.insert_sample(500, SAMPLE)
    assert [r[0] for r in store.query_since(200)] == [500]
    store.close()


def test_filter_none_when_unknown(tmp_path):
    store = d.HistoryStore(str(tmp_path / "h.db"))
    store.insert_sample(1, {**SAMPLE, "filter_dirty": None})
    assert store.query_since(0)[0][-1] is None
    store.close()


def test_device_info_roundtrip(tmp_path):
    store = d.HistoryStore(str(tmp_path / "h.db"))
    assert store.get_device_info() == {}
    info = {"model": "FXFQ32AVEB", "model_aux": "A9P/028",
            "controller_version": "3.6.0", "comm_version": "5.17"}
    store.set_device_info(info, 1234)
    assert store.get_device_info() == info
    store.close()


def test_migration_adds_filter_column(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE samples (ts INTEGER PRIMARY KEY, power INTEGER, mode INTEGER, "
                "cooling_setpoint REAL, heating_setpoint REAL, room_temp INTEGER, "
                "outdoor_temp INTEGER, fan INTEGER)")
    con.execute("INSERT INTO samples VALUES (50,1,3,24.0,24.0,23,NULL,3)")
    con.commit()
    con.close()

    store = d.HistoryStore(path)   # opening should ALTER TABLE to add filter_dirty
    cols = [r[1] for r in store.db.execute("PRAGMA table_info(samples)").fetchall()]
    assert "filter_dirty" in cols
    rows = store.query_since(0)
    assert rows[0][0] == 50 and rows[0][-1] is None   # old row preserved, filter NULL
    store.insert_sample(60, SAMPLE)                    # new inserts still work
    assert len(store.query_since(0)) == 2
    store.close()
