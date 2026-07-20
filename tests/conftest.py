import pytest

import linkcheck.aia


@pytest.fixture(autouse=True)
def _no_real_sockets_for_aia_leaf_cert(monkeypatch):
    """The only real-network primitive in linkcheck.aia is _leaf_cert_der (a raw TLS
    socket), used by chase() only when a caller doesn't already have the cert (i.e.
    every caller except test_aia.py, which always passes leaf_cert_der= explicitly).
    Block it so any test that incidentally goes down the bad_ssl_cert -> AIA-chase
    path (e.g. via checker.check_link) can't touch the network - the test suite is
    offline by design (see CLAUDE.md). chase() treats this the same as a real
    connection failure and gives up, falling back to the plain bad_ssl_cert result.
    """

    def _no_sockets(host, port, timeout):
        raise OSError("real network access is disabled in tests")

    monkeypatch.setattr(linkcheck.aia, "_leaf_cert_der", _no_sockets)
