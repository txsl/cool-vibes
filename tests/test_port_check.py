"""Port pre-flight test.

main() checks the bind before starting uvicorn because uvicorn handles a bind
PermissionError itself (logs, then SystemExit) - catching it around
uvicorn.run never fires.
"""
import errno
import daikin_server as d


class _FakeSocket:
    def __init__(self, exc):
        self.exc = exc

    def setsockopt(self, *args):
        pass

    def bind(self, addr):
        if self.exc:
            raise self.exc

    def close(self):
        pass


def _bind_raises(monkeypatch, exc):
    monkeypatch.setattr(d.socket, "socket", lambda *a, **k: _FakeSocket(exc))


def test_bindable_when_bind_succeeds(monkeypatch):
    _bind_raises(monkeypatch, None)
    assert d.port_bindable("0.0.0.0", 80)


def test_not_bindable_on_permission_denied(monkeypatch):
    _bind_raises(monkeypatch, PermissionError(errno.EACCES, "Permission denied"))
    assert not d.port_bindable("127.0.0.1", 80)


def test_other_bind_errors_are_left_to_uvicorn(monkeypatch):
    _bind_raises(monkeypatch, OSError(errno.EADDRINUSE, "Address already in use"))
    assert d.port_bindable("0.0.0.0", 80)
