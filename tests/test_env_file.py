""".env loading test.

The server reads <repo>/.env itself so that launchd - which runs with a minimal
environment and never sources a shell profile - still gets DAIKIN_ADDRESS.
"""
import os
import daikin_server as d


def _clean_env(monkeypatch, *names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_missing_file_is_not_an_error(tmp_path):
    assert d.load_env_file(str(tmp_path / "nope.env")) == {}


def test_parses_values_and_skips_noise(tmp_path, monkeypatch):
    _clean_env(monkeypatch, "DAIKIN_ADDRESS", "QUOTED", "SPACED")
    path = tmp_path / ".env"
    path.write_text(
        "\n"
        "# a comment\n"
        "DAIKIN_ADDRESS=PLAIN-VALUE\n"
        'QUOTED="double-quoted"\n'
        "SPACED  =  spaced-out  \n"
        "not-a-pair\n"
    )
    loaded = d.load_env_file(str(path))
    assert loaded == {
        "DAIKIN_ADDRESS": "PLAIN-VALUE",
        "QUOTED": "double-quoted",
        "SPACED": "spaced-out",
    }
    assert os.environ["DAIKIN_ADDRESS"] == "PLAIN-VALUE"


def test_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIKIN_ADDRESS", "FROM-REAL-ENV")
    path = tmp_path / ".env"
    path.write_text("DAIKIN_ADDRESS=FROM-DOTENV\n")
    assert d.load_env_file(str(path)) == {}          # nothing overridden
    assert os.environ["DAIKIN_ADDRESS"] == "FROM-REAL-ENV"
