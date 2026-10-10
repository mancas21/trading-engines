"""HTTP transport for the venue adapters.

WHY THIS EXISTS — MEASURED, NOT GUESSED
=======================================
On this machine urllib's proxy resolution returns
`{'https': 'http://127.0.0.1:54626'}` (a local node process, PID 23820, from
the Windows internet settings). Through that proxy:

    urllib + certifi ctx -> OKX   CERTIFICATE_VERIFY_FAILED (verify_code=20)
    urllib + certifi ctx -> Bybit CERTIFICATE_VERIFY_FAILED (verify_code=20)
    urllib + certifi ctx -> Binance OK 200
    urllib + certifi ctx -> Kraken  OK 200

The proxy mangles the certificate chain for exactly those two hosts. It is NOT
a certificate problem: a raw socket and a direct `http.client.HTTPSConnection`
BOTH verify OKX fine, and `curl --noproxy '*'` returns 200 for it. 24/24
attempts failed, so it is deterministic, not intermittent — an earlier
Cloudflare hypothesis was wrong.

The fix is therefore to NOT use the proxy for exchange market data. This module
builds one shared opener with `ProxyHandler({})` so every adapter inherits it,
and a single `get()` replaces the per-adapter `urllib.request.urlopen` calls.

Trade-off, stated plainly: exchange market data is public and unencrypted in
transit beyond TLS, and TLS is verified against the real certifi bundle. The
traffic bypasses a local proxy that was presumably installed for other
(reasoned) purposes. If this host's egress must go through that proxy, set
SCANNER_USE_PROXY=1 and this module will honour it instead — the SSL failure
returns for OKX/Bybit in that mode, so `min_venues` will correctly drop to
whatever venues remain reachable.
"""
from __future__ import annotations

import json
import os
import urllib.request

USE_PROXY = os.environ.get("SCANNER_USE_PROXY", "").strip().lower() in ("1", "true", "yes")

SSL_CONTEXT = None
try:
    import certifi
    import ssl
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:                                       # noqa: BLE001
    # certifi is not a hard dependency; the default context still verifies.
    pass

TIMEOUT_S = 15.0

if USE_PROXY:
    _opener = urllib.request.build_opener()          # honour system proxy
else:
    # Direct: bypass the local proxy whose TLS chain breaks OKX/Bybit.
    #
    # The context MUST be installed on HTTPSHandler. `OpenerDirector.open()`
    # does not accept a `context=` kwarg (TypeError), so a per-request context
    # is not an option -- verified, not assumed.
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=SSL_CONTEXT),
    )


def get(url: str, timeout: float = TIMEOUT_S):
    """GET a JSON document. Returns the parsed body.

    Raises the underlying urllib error on failure — callers route that into
    the poller's failure path, which distinguishes a permanent rejection from a
    transient one. Never returns a zero or an empty dict on error.
    """
    headers = {"User-Agent": "scanner-engine/0.1"}
    req = urllib.request.Request(url, headers=headers)
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def transport_report() -> dict:
    """What this module resolved, for /health and for the record."""
    return {
        "use_proxy": USE_PROXY,
        "system_proxies": urllib.request.getproxies() if USE_PROXY else None,
        "bypass_reason": None if USE_PROXY else
        "local proxy breaks TLS chain verification for OKX/Bybit "
        "(verify_code=20); direct transport verifies cleanly",
        "certifi": bool(SSL_CONTEXT is not None),
    }


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(transport_report(), indent=2))
    for name, url in (
        ("binance", "https://fapi.binance.com/fapi/v1/ping"),
        ("okx", "https://www.okx.com/api/v5/public/time"),
        ("kraken", "https://api.kraken.com/0/public/Time"),
        ("bybit", "https://api.bybit.com/v5/market/time"),
    ):
        try:
            get(url)
            print(f"  {name:8} OK")
        except Exception as e:                           # noqa: BLE001
            print(f"  {name:8} FAIL {type(e).__name__}: {str(e)[:55]}")