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
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
DEFAULT_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = [
    "org:create_api_key",
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
    REFRESH_FAILED = "refresh_failed"
    NEEDS_REAUTH = "needs_reauth"
    MISSING = "missing"


class ClaudeAuthStatus(BaseModel):
    status: TokenStatus
    expires_at: str | None
    ttl_seconds: int
    subscription_type: str
    last_refresh: str | None
    message: str


class AuthCompleteRequest(BaseModel):
    code: str
    state: str


def _parse_expires_at(tokens: dict[str, Any]) -> int | None:
    """Извлечь expiresAt (unix ms) из ответа сервера (camelCase или snake_case)."""
    if "expiresAt" in tokens:
        return int(tokens["expiresAt"])
    if "expires_at" in tokens:
        return int(tokens["expires_at"])
    if "expires_in" in tokens:
        return int((time.time() + int(tokens["expires_in"])) * 1000)
    return None


def _parse_token_pair(tokens: dict[str, Any]) -> tuple[str | None, str | None]:
    """Извлечь (access_token, refresh_token) из ответа (camelCase или snake_case)."""
    access = tokens.get("accessToken") or tokens.get("access_token")
    refresh = tokens.get("refreshToken") or tokens.get("refresh_token")
    return access, refresh


class ClaudeAuth:
    """Управление OAuth токенами Claude."""

    def __init__(
        self,
        credentials_path: Path | None = None,
        redirect_uri: str | None = None,
    ) -> None:
        self._creds_path: Path = credentials_path or (
            Path.home() / ".claude" / ".credentials.json"
        )
        self._redirect_uri = redirect_uri or DEFAULT_REDIRECT_URI
        self._last_refresh: datetime | None = None
        self._pending_file = self._creds_path.parent / ".pending_auth.json"
        self._pending_auth: dict[str, str] = self._load_pending()

    def _load_pending(self) -> dict[str, str]:
        """Загрузить pending auth из файла (переживает рестарт контейнера)."""
        try:
            if self._pending_file.exists():
                return json.loads(self._pending_file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_pending(self) -> None:
        """Сохранить pending auth в файл."""
        try:
            self._pending_file.write_text(json.dumps(self._pending_auth), "utf-8")
        except OSError as exc:
            logger.warning("Failed to save pending auth: %s", exc)

    def _read_credentials(self) -> dict[str, Any] | None:
        """Прочитать credentials JSON. Возвращает None если файл отсутствует."""
        if not self._creds_path.exists():
            return None
        try:
            return json.loads(self._creds_path.read_text("utf-8"))  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read credentials: %s", exc)
            return None

    def _write_credentials(self, data: dict[str, Any]) -> None:
        """Atomic write с fcntl.flock для защиты от гонок между контейнерами."""
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._creds_path.parent),
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(self._creds_path))
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _get_expiry_info(self, oauth: dict[str, Any]) -> tuple[datetime, int]:
        """Вернуть (expires_at_datetime, ttl_seconds) из oauth данных."""
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

    def get_status(self) -> ClaudeAuthStatus:
        """Полный статус токена без сетевых вызовов."""
        data = self._read_credentials()
        if data is None:
            return ClaudeAuthStatus(
                status=TokenStatus.MISSING,
                expires_at=None,
                ttl_seconds=0,
                subscription_type="unknown",
                last_refresh=None,
                message="Credentials file not found",
            )
        oauth = data.get(_OAUTH_KEY)
        if not oauth or not oauth.get("accessToken"):
            return ClaudeAuthStatus(
                status=TokenStatus.MISSING,
                expires_at=None,
                ttl_seconds=0,
                subscription_type="unknown",
                last_refresh=None,
                message="No OAuth tokens in credentials",
            )
        sub_type: str = oauth.get("subscriptionType", "unknown")
        expires_dt, ttl = self._get_expiry_info(oauth)
        last_ref = self._last_refresh.isoformat() if self._last_refresh else None
        if ttl <= 0:
            status, message = TokenStatus.EXPIRED, "Token expired"
        elif ttl <= REFRESH_THRESHOLD_SEC:
            status = TokenStatus.EXPIRING_SOON
            message = f"Token expiring in {ttl // 60}m, auto-refresh soon"
        else:
            status = TokenStatus.ACTIVE
            message = f"Token valid, expires in {ttl // 3600}h {(ttl % 3600) // 60}m"
        return ClaudeAuthStatus(
            status=status,
            expires_at=expires_dt.isoformat(),
            ttl_seconds=max(ttl, 0),
            subscription_type=sub_type,
            last_refresh=last_ref,
            message=message,
        )

    def is_token_valid(self) -> bool:
        """Quick check: токен есть и не истек."""
        data = self._read_credentials()
        if data is None:
            return False
        oauth = data.get(_OAUTH_KEY)
        if not oauth or not oauth.get("accessToken"):
            return False
        _, ttl = self._get_expiry_info(oauth)
        return ttl > 0

    def ensure_valid_token(self) -> bool:
        """Главный метод - вызывать перед каждым _call_claude(). Возвращает True если ОК."""
        data = self._read_credentials()
        if data is None:
            logger.warning("Credentials file not found: %s", self._creds_path)
            return False
        oauth = data.get(_OAUTH_KEY)
        if not oauth or not oauth.get("accessToken"):
            logger.warning("No OAuth tokens in credentials")
            return False
        _, ttl = self._get_expiry_info(oauth)
        if ttl > REFRESH_THRESHOLD_SEC:
            return True
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            logger.warning("No refresh token available")
            return False
        logger.info("Token TTL=%ds, attempting refresh", ttl)
        return self._refresh_token(refresh_token)

    def _refresh_token(self, refresh_token: str) -> bool:
        """POST refresh_token grant на TOKEN_URL. Возвращает True при успехе."""
        try:
            resp = httpx.post(
                TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                    "scope": " ".join(SCOPES),
                },
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            logger.warning("Token refresh HTTP error: %s", exc)
            return False
        if resp.status_code != 200:
            logger.warning(
                "Token refresh failed: status=%d body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        tokens: dict[str, Any] = resp.json()
        new_access, new_refresh = _parse_token_pair(tokens)
        if not new_access:
            logger.warning("No access token in refresh response")
            return False
        new_expires_at = _parse_expires_at(tokens)
        data = self._read_credentials() or {}
        oauth = data.get(_OAUTH_KEY, {})
        oauth["accessToken"] = new_access
        if new_refresh:
            oauth["refreshToken"] = new_refresh
        if new_expires_at is not None:
            oauth["expiresAt"] = new_expires_at
        data[_OAUTH_KEY] = oauth
        self._write_credentials(data)
        self._last_refresh = datetime.now(timezone.utc)
        logger.info("Token refreshed successfully")
        return True

    def start_auth_flow(self) -> tuple[str, str]:
        """Начать OAuth PKCE flow. Возвращает (auth_url, state)."""
        state = secrets.token_urlsafe(24)
        verifier = self._generate_code_verifier()
        self._pending_auth[state] = verifier
        self._save_pending()
        params = {
            "code": "true",
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": self._redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": self._generate_code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}", state

    def complete_auth_flow(self, code: str, state: str) -> bool:
        """Обменять authorization code на токены. Возвращает True при успехе."""
        code_verifier = self._pending_auth.pop(state, None)
        self._save_pending()
        if code_verifier is None:
            logger.warning("Unknown or expired state: %s", state)
            return False
        try:
            resp = httpx.post(
                TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": CLIENT_ID,
                    "redirect_uri": self._redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            logger.warning("Code exchange HTTP error: %s", exc)
            return False
        if resp.status_code != 200:
            logger.warning(
                "Code exchange failed: status=%d body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        tokens: dict[str, Any] = resp.json()
        new_access, new_refresh = _parse_token_pair(tokens)
        if not new_access:
            logger.warning("No access token in code exchange response")
            return False
        data = self._read_credentials() or {}
        oauth: dict[str, Any] = {"accessToken": new_access}
        if new_refresh:
            oauth["refreshToken"] = new_refresh
        expires_at = _parse_expires_at(tokens)
        if expires_at is not None:
            oauth["expiresAt"] = expires_at
        for key in ("scopes", "subscriptionType", "rateLimitTier"):
            if key in tokens:
                oauth[key] = tokens[key]
        data[_OAUTH_KEY] = oauth
        self._write_credentials(data)
        self._last_refresh = datetime.now(timezone.utc)
        logger.info("OAuth code exchange successful")
        return True

    @staticmethod
    def _generate_code_verifier() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _generate_code_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---- FastAPI Router ----

_CALLBACK_PATH = "/api/claude-auth/callback"


def create_claude_auth_router(
    public_url: str | None = None,
) -> tuple[APIRouter, "ClaudeAuth"]:
    """Создать роутер и экземпляр ClaudeAuth.

    Args:
        public_url: Публичный URL сервиса (например https://poly.maxbob.xyz).
                    Если задан — redirect_uri указывает на наш callback.
    """
    redirect_uri = (
        f"{public_url}{_CALLBACK_PATH}" if public_url else DEFAULT_REDIRECT_URI
    )
    auth = ClaudeAuth(redirect_uri=redirect_uri)
    router = APIRouter(prefix="/api/claude-auth", tags=["claude-auth"])

    @router.get("/status", response_model=ClaudeAuthStatus)
    async def get_claude_auth_status() -> ClaudeAuthStatus:
        return auth.get_status()

    @router.post("/refresh")
    async def force_refresh() -> dict[str, Any]:
        data = auth._read_credentials()
        if data is None:
            return {"ok": False, "error": "Credentials file not found"}
        oauth = data.get(_OAUTH_KEY)
        if not oauth:
            return {"ok": False, "error": "No OAuth data in credentials"}
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            return {"ok": False, "error": "No refresh token available"}
        if auth._refresh_token(refresh_token):
            return {"ok": True, "status": auth.get_status().model_dump()}
        return {"ok": False, "error": "Refresh token expired or invalid"}

    @router.get("/start")
    async def start_auth() -> dict[str, str]:
        auth_url, state = auth.start_auth_flow()
        return {"auth_url": auth_url, "state": state}

    @router.post("/complete")
    async def complete_auth(request: AuthCompleteRequest) -> dict[str, Any]:
        if auth.complete_auth_flow(request.code, request.state):
            return {"ok": True, "status": auth.get_status().model_dump()}
        return {"ok": False, "error": "Invalid state or code exchange failed"}

    @router.get("/callback")
    async def oauth_callback(code: str, state: str) -> RedirectResponse:
        """OAuth callback — Claude редиректит сюда с code и state."""
        if auth.complete_auth_flow(code, state):
            return RedirectResponse(url="/?auth=ok")
        return RedirectResponse(url="/?auth=failed")

    return router, auth


# Обратная совместимость: если не нужен custom redirect
claude_auth_router, _auth = create_claude_auth_router(
    public_url=os.environ.get("PUBLIC_URL"),
)
