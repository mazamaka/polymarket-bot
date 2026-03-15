"""Standalone модуль для управления OAuth-токенами Claude Code CLI.

Автоматически обновляет access token через refresh token.
Предоставляет FastAPI роутер для статуса и ручного управления.
Идентичный файл для polymarket-bot и alpha-scout.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]
REFRESH_THRESHOLD_SEC = 1800
_OAUTH_KEY = "claudeAiOauth"


class TokenStatus(str, Enum):
    ACTIVE = "active"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"
    MISSING = "missing"


class ClaudeAuthStatus(BaseModel):
    status: TokenStatus
    expires_at: str | None
    ttl_seconds: int
    subscription_type: str
    last_refresh: str | None
    has_refresh_token: bool
    message: str


class AuthCompleteRequest(BaseModel):
    code: str
    state: str


def _parse_expires_at(tokens: dict[str, Any]) -> int | None:
    """Извлечь expiresAt (unix ms) из ответа сервера."""
    if "expiresAt" in tokens:
        return int(tokens["expiresAt"])
    if "expires_at" in tokens:
        return int(tokens["expires_at"])
    if "expires_in" in tokens:
        return int((time.time() + int(tokens["expires_in"])) * 1000)
    return None


def _parse_token_pair(tokens: dict[str, Any]) -> tuple[str | None, str | None]:
    """Извлечь (access_token, refresh_token) из ответа."""
    access = tokens.get("accessToken") or tokens.get("access_token")
    refresh = tokens.get("refreshToken") or tokens.get("refresh_token")
    return access, refresh


def _extract_code(raw: str) -> str:
    """Извлечь auth code из строки (голый код, код#state, или URL)."""
    raw = raw.strip()
    if "code=" in raw:
        codes = parse_qs(urlparse(raw).query).get("code", [])
        if codes:
            return codes[0]
    if "#" in raw:
        raw = raw.split("#")[0]
    return raw


class ClaudeAuth:
    """Управление OAuth токенами Claude."""

    def __init__(self, credentials_path: Path | None = None) -> None:
        self._creds_path = credentials_path or (
            Path.home() / ".claude" / ".credentials.json"
        )
        self._last_refresh: datetime | None = None
        self._pending_file = self._creds_path.parent / ".pending_auth.json"
        self._pending_auth: dict[str, str] = self._load_pending()

    # ---- Credentials I/O ----

    def _load_pending(self) -> dict[str, str]:
        try:
            if self._pending_file.exists():
                return json.loads(self._pending_file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_pending(self) -> None:
        try:
            self._pending_file.write_text(json.dumps(self._pending_auth), "utf-8")
        except OSError as exc:
            logger.warning("Failed to save pending auth: %s", exc)

    def _read_credentials(self) -> dict[str, Any] | None:
        if not self._creds_path.exists():
            return None
        try:
            return json.loads(self._creds_path.read_text("utf-8"))  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read credentials: %s", exc)
            return None

    def _read_oauth(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Прочитать (oauth_dict, full_data). oauth=None если нет токенов."""
        data = self._read_credentials()
        if data is None:
            return None, {}
        oauth = data.get(_OAUTH_KEY)
        if not oauth or not oauth.get("accessToken"):
            return None, data
        return oauth, data

    def _write_credentials(self, data: dict[str, Any]) -> None:
        """Запись credentials с flock (совместимо с Docker bind mount)."""
        with open(self._creds_path, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

    # ---- Expiry ----

    @staticmethod
    def _get_expiry(oauth: dict[str, Any]) -> tuple[datetime, int]:
        """Вернуть (expires_at_datetime, ttl_seconds)."""
        now = time.time()
        expires_at_ms = oauth.get("expiresAt")
        if expires_at_ms is not None:
            expires_at_sec = float(expires_at_ms) / 1000.0
        else:
            expires_in = oauth.get("expires_in")
            if expires_in is not None:
                expires_at_sec = now + float(expires_in)
            else:
                return datetime.now(timezone.utc), 0
        ttl = int(expires_at_sec - now)
        return datetime.fromtimestamp(expires_at_sec, tz=timezone.utc), ttl

    # ---- Public API ----

    def get_status(self) -> ClaudeAuthStatus:
        """Полный статус токена без сетевых вызовов."""
        oauth, _ = self._read_oauth()
        if oauth is None:
            return ClaudeAuthStatus(
                status=TokenStatus.MISSING,
                expires_at=None,
                ttl_seconds=0,
                subscription_type="unknown",
                last_refresh=None,
                has_refresh_token=False,
                message="No OAuth tokens found",
            )
        expires_dt, ttl = self._get_expiry(oauth)
        if ttl <= 0:
            status, msg = TokenStatus.EXPIRED, "Token expired"
        elif ttl <= REFRESH_THRESHOLD_SEC:
            status = TokenStatus.EXPIRING_SOON
            msg = f"Token expiring in {ttl // 60}m"
        else:
            status = TokenStatus.ACTIVE
            msg = f"Token valid, expires in {ttl // 3600}h {(ttl % 3600) // 60}m"
        return ClaudeAuthStatus(
            status=status,
            expires_at=expires_dt.isoformat(),
            ttl_seconds=max(ttl, 0),
            subscription_type=oauth.get("subscriptionType", "unknown"),
            last_refresh=self._last_refresh.isoformat() if self._last_refresh else None,
            has_refresh_token=bool(oauth.get("refreshToken")),
            message=msg,
        )

    def is_token_valid(self) -> bool:
        """Quick check: токен есть и не истёк."""
        oauth, _ = self._read_oauth()
        if oauth is None:
            return False
        _, ttl = self._get_expiry(oauth)
        return ttl > 0

    def ensure_valid_token(self) -> bool:
        """Проверить и обновить токен если нужно. Вызывать перед каждым API-запросом."""
        oauth, _ = self._read_oauth()
        if oauth is None:
            return False
        _, ttl = self._get_expiry(oauth)
        if ttl > REFRESH_THRESHOLD_SEC:
            return True
        rt = oauth.get("refreshToken")
        if not rt:
            logger.warning("No refresh token, TTL=%ds", ttl)
            return ttl > 0
        logger.info("Token TTL=%ds, refreshing", ttl)
        return self._do_refresh(rt, oauth.get("scopes"))

    def force_refresh(self) -> tuple[bool, str]:
        """Принудительное обновление. Возвращает (ok, error_or_empty)."""
        oauth, _ = self._read_oauth()
        if oauth is None:
            return False, "No OAuth tokens found"
        rt = oauth.get("refreshToken")
        if not rt:
            return False, "No refresh token available"
        if self._do_refresh(rt, oauth.get("scopes")):
            return True, ""
        return False, "Refresh failed (token expired or invalid)"

    # ---- OAuth flows ----

    def start_auth_flow(self) -> tuple[str, str]:
        """Начать OAuth PKCE flow. Возвращает (auth_url, state)."""
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(32)
        self._pending_auth[state] = verifier
        self._save_pending()
        params = {
            "code": "true",
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": self._s256_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}", state

    def complete_auth_flow(self, code: str, state: str) -> bool:
        """Обменять authorization code на токены."""
        code = _extract_code(code)
        verifier = self._pending_auth.get(state)
        if verifier is None:
            logger.warning("Unknown or expired state: %s", state)
            return False
        try:
            resp = httpx.post(
                TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                    "state": state,
                },
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            logger.warning("Code exchange HTTP error: %s", exc)
            return False
        if resp.status_code != 200:
            logger.warning(
                "Code exchange failed: %d %s", resp.status_code, resp.text[:200]
            )
            return False
        tokens: dict[str, Any] = resp.json()
        access, refresh = _parse_token_pair(tokens)
        if not access:
            logger.warning("No access token in exchange response")
            return False
        self._save_tokens(tokens, access, refresh)
        self._pending_auth.pop(state, None)
        self._save_pending()
        logger.info("OAuth code exchange successful")
        return True

    # ---- Internal ----

    def _do_refresh(
        self, refresh_token: str, saved_scopes: list[str] | None = None
    ) -> bool:
        """POST refresh_token grant."""
        scope_str = " ".join(saved_scopes) if saved_scopes else " ".join(SCOPES)
        try:
            resp = httpx.post(
                TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                    "scope": scope_str,
                },
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            logger.warning("Refresh HTTP error: %s", exc)
            return False
        if resp.status_code != 200:
            logger.warning("Refresh failed: %d %s", resp.status_code, resp.text[:200])
            return False
        tokens: dict[str, Any] = resp.json()
        access, refresh = _parse_token_pair(tokens)
        if not access:
            logger.warning("No access token in refresh response")
            return False
        data = self._read_credentials() or {}
        oauth = data.get(_OAUTH_KEY, {})
        oauth["accessToken"] = access
        if refresh:
            oauth["refreshToken"] = refresh
        expires_at = _parse_expires_at(tokens)
        if expires_at is not None:
            oauth["expiresAt"] = expires_at
        data[_OAUTH_KEY] = oauth
        self._write_credentials(data)
        self._last_refresh = datetime.now(timezone.utc)
        logger.info("Token refreshed successfully")
        return True

    def _save_tokens(
        self, tokens: dict[str, Any], access: str, refresh: str | None
    ) -> None:
        """Сохранить новые токены из code exchange."""
        data = self._read_credentials() or {}
        oauth: dict[str, Any] = {"accessToken": access}
        if refresh:
            oauth["refreshToken"] = refresh
        expires_at = _parse_expires_at(tokens)
        if expires_at is not None:
            oauth["expiresAt"] = expires_at
        # CLI requires these fields to recognize login
        oauth["scopes"] = tokens.get("scopes", SCOPES)
        oauth["subscriptionType"] = tokens.get("subscriptionType", "max")
        oauth["rateLimitTier"] = tokens.get("rateLimitTier", "default_claude_max_20x")
        data[_OAUTH_KEY] = oauth
        self._write_credentials(data)
        self._last_refresh = datetime.now(timezone.utc)

    @staticmethod
    def _s256_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---- FastAPI Router ----

claude_auth_router = APIRouter(prefix="/api/claude-auth", tags=["claude-auth"])
_auth = ClaudeAuth()


@claude_auth_router.get("/status", response_model=ClaudeAuthStatus)
async def get_claude_auth_status() -> ClaudeAuthStatus:
    return _auth.get_status()


@claude_auth_router.post("/refresh")
async def api_force_refresh() -> dict[str, Any]:
    ok, error = _auth.force_refresh()
    if ok:
        return {"ok": True, "status": _auth.get_status().model_dump()}
    return {"ok": False, "error": error}


@claude_auth_router.get("/start")
async def api_start_auth() -> dict[str, str]:
    auth_url, state = _auth.start_auth_flow()
    return {"auth_url": auth_url, "state": state}


@claude_auth_router.post("/complete")
async def api_complete_auth(request: AuthCompleteRequest) -> dict[str, Any]:
    if _auth.complete_auth_flow(request.code, request.state):
        return {"ok": True, "status": _auth.get_status().model_dump()}
    return {"ok": False, "error": "Invalid state or code exchange failed"}
