"""Patch py-clob-client HTTP layer to route through proxy.

Must be called BEFORE any ClobClient usage.
Patches both httpx (used by helpers.py) and requests.Session (used by auth).
Supports hot-swapping proxy URL via re-calling apply_proxy().
"""

import logging
import random
import string

import httpx
import requests as _req

import py_clob_client.http_helpers.helpers as _helpers
from py_clob_client.exceptions import PolyApiException

logger = logging.getLogger(__name__)

_current_client: httpx.Client | None = None
_orig_session: type | None = None


def apply_proxy(proxy_url_template: str) -> None:
    """Apply or replace proxy for py-clob-client internals.

    Args:
        proxy_url_template: URL with {session} placeholder for sticky sessions.
            Example: http://user-zone-ca:pass@proxy:2333
    """
    global _current_client, _orig_session

    # Close previous client if re-patching
    if _current_client is not None:
        try:
            _current_client.close()
        except Exception:
            pass

    session_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    proxy_url = proxy_url_template.replace("{session}", f"bot{session_id}")
    logger.info("CLOB proxy: %s", proxy_url.split("@")[-1])

    _current_client = httpx.Client(proxy=proxy_url, timeout=30)
    px = _current_client

    def _request(
        endpoint: str, method: str, headers: dict | None = None, data: object = None
    ) -> object:
        if headers is None:
            headers = {}
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        )
        headers["Accept"] = "*/*"
        headers["Connection"] = "keep-alive"
        headers["Content-Type"] = "application/json"
        if method == "GET":
            headers["Accept-Encoding"] = "gzip"
        try:
            if isinstance(data, str):
                resp = px.request(
                    method=method,
                    url=endpoint,
                    headers=headers,
                    content=data.encode("utf-8"),
                )
            else:
                resp = px.request(
                    method=method, url=endpoint, headers=headers, json=data
                )
            if resp.status_code != 200:
                raise PolyApiException(resp)
            try:
                return resp.json()
            except ValueError:
                return resp.text
        except httpx.RequestError as e:
            logger.error("Proxy request error: %s %s -> %s", method, endpoint[:60], e)
            raise PolyApiException(error_msg=str(e))

    _helpers.request = _request
    _helpers.post = lambda ep, h=None, d=None: _request(ep, "POST", h, d)
    _helpers.get = lambda ep, h=None, d=None: _request(ep, "GET", h, d)
    _helpers.delete = lambda ep, h=None, d=None: _request(ep, "DELETE", h, d)
    _helpers.put = lambda ep, h=None, d=None: _request(ep, "PUT", h, d)

    # Save original Session class on first patch
    if _orig_session is None:
        _orig_session = _req.Session

    orig = _orig_session

    class ProxiedSession(orig):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.proxies = {"http": proxy_url, "https": proxy_url}
            self.headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )

    _req.Session = ProxiedSession
    logger.info("CLOB proxy patch applied")
