"""The Juice Shop readiness probe / oracle must talk to the target directly,
never through a proxy — a set HTTP_PROXY/HTTPS_PROXY (for image pulls) must not
hijack the loopback probe. See scenario._DIRECT_OPENER."""
import urllib.request as u

from bench.scenarios.juice_shop import scenario as js


def _active_proxy(opener):
    """True if the opener has a ProxyHandler that would route through a proxy."""
    return any(isinstance(h, u.ProxyHandler) and h.proxies for h in opener.handlers)


def test_probe_opener_never_proxies():
    # An explicit empty ProxyHandler leaves the opener with no active proxy, so
    # urllib connects directly no matter what proxy env vars are set.
    assert not _active_proxy(js._DIRECT_OPENER)


def test_empty_proxyhandler_beats_proxy_env(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(var, "http://proxy.example:8080")
    # A default opener WOULD pick up the env proxy (this is the bug we avoid)...
    assert _active_proxy(u.build_opener())
    # ...but the explicit empty ProxyHandler the scenario uses does not.
    assert not _active_proxy(u.build_opener(u.ProxyHandler({})))
