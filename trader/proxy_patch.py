"""Patch py-clob-client HTTP layer with direct-first + proxy fallback.

Strategy: try direct request first (fast). If it fails — retry through proxy.
This works both on servers (DE, direct is fast) and locally (where CLOB may be blocked).

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

_direct_client: httpx.Client | None = None
_proxy_client: httpx.Client | None = None
_proxy_url: str = ""
_orig_session: type | None = None

_HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Connection": "keep-alive",
    "Content-Type": "application/json",
}

# Track which mode is working to avoid unnecessary retries
_direct_ok = True  # optimistic start


def apply_proxy(proxy_url_template: str) -> None:
    """Apply or replace proxy as fallback for py-clob-client internals.

    After calling this, all CLOB requests will:
    1. Try direct connection first
    2. If direct fails → retry through proxy

    Args:
        proxy_url_template: URL with {session} placeholder for sticky sessions.
    """
    global _direct_client, _proxy_client, _proxy_url, _orig_session, _direct_ok

    # Close previous proxy client if re-patching
    if _proxy_client is not None:
        try:
            _proxy_client.close()
        except Exception:
            pass

    session_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    _proxy_url = proxy_url_template.replace("{session}", f"bot{session_id}")
    logger.info("CLOB proxy fallback: %s", _proxy_url.split("@")[-1])

    # Direct client (no proxy, fast)
    if _direct_client is None:
        _direct_client = httpx.Client(timeout=30)

    # Proxy client (fallback)
    _proxy_client = httpx.Client(proxy=_proxy_url, timeout=30)

    _direct_ok = True  # reset: try direct first

    def _do_request(
        client: httpx.Client,
        endpoint: str,
        method: str,
        headers: dict | None = None,
        data: object = None,
    ) -> httpx.Response:
        """Execute HTTP request with given client."""
        if headers is None:
            headers = {}
        headers.update(_HEADERS_BASE)
        if method == "GET":
            headers["Accept-Encoding"] = "gzip"

        if isinstance(data, str):
            return client.request(
                method=method,
                url=endpoint,
                headers=headers,
                content=data.encode("utf-8"),
            )
        return client.request(
            method=method,
            url=endpoint,
            headers=headers,
            json=data,
        )

    def _request(
        endpoint: str, method: str, headers: dict | None = None, data: object = None
    ) -> object:
        global _direct_ok

        # Strategy: try direct first if it was working, else go proxy first
        clients = (
            [("direct", _direct_client), ("proxy", _proxy_client)]
            if _direct_ok
            else [("proxy", _proxy_client), ("direct", _direct_client)]
        )

        last_error = None
        for label, client in clients:
            try:
                resp = _do_request(client, endpoint, method, headers, data)
                if resp.status_code == 200:
                    # Update preference based on what worked
                    _direct_ok = label == "direct"
                    try:
                        return resp.json()
                    except ValueError:
                        return resp.text

                # Non-200 but not a connection issue — don't fallback
                # (e.g. 400 bad request, 422 validation — proxy won't help)
                if resp.status_code in (400, 401, 422):
                    raise PolyApiException(resp)

                # 403/429/5xx — try fallback
                last_error = PolyApiException(resp)
                if label == clients[0][0]:
                    logger.info(
                        "CLOB %s %d, trying %s", label, resp.status_code, clients[1][0]
                    )
                    continue
                raise last_error

            except httpx.RequestError as e:
                last_error = e
                if label == clients[0][0]:
                    logger.info(
                        "CLOB %s failed (%s), trying %s",
                        label,
                        type(e).__name__,
                        clients[1][0],
                    )
                    _direct_ok = label != "direct"
                    continue
                raise PolyApiException(error_msg=str(e))

        if last_error:
            raise PolyApiException(error_msg=str(last_error))

    _helpers.request = _request
    _helpers.post = lambda ep, h=None, d=None: _request(ep, "POST", h, d)
    _helpers.get = lambda ep, h=None, d=None: _request(ep, "GET", h, d)
    _helpers.delete = lambda ep, h=None, d=None: _request(ep, "DELETE", h, d)
    _helpers.put = lambda ep, h=None, d=None: _request(ep, "PUT", h, d)

    # Patch requests.Session for auth endpoints
    if _orig_session is None:
        _orig_session = _req.Session

    orig = _orig_session
    proxy_url_for_session = _proxy_url

    class FallbackSession(orig):
        """Session that tries direct first, proxy on failure."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.headers["User-Agent"] = _HEADERS_BASE["User-Agent"]
            self._proxy_map = {
                "http": proxy_url_for_session,
                "https": proxy_url_for_session,
            }

        def send(self, request, **kwargs):
            global _direct_ok
            if _direct_ok:
                try:
                    return super().send(request, **kwargs)
                except Exception:
                    self.proxies = self._proxy_map
                    _direct_ok = False
                    return super().send(request, **kwargs)
            else:
                self.proxies = self._proxy_map
                return super().send(request, **kwargs)

    _req.Session = FallbackSession
    logger.info("CLOB direct+proxy fallback patch applied")
