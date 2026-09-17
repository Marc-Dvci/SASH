"""Ring OAuth 2.0 account linking and token lifecycle.

Ring account linking is a server-to-server flow: browser JavaScript cannot call
the Ring API (CORS), and the client secret and HMAC signing key must never reach
a browser. SASH therefore keeps the whole flow in the backend and hands the
frontend nothing but its own session.

Access tokens live about four hours and refresh tokens about thirty days, so the
service refreshes proactively rather than waiting for a 401, and marks the
household as ``relink_required`` the moment a refresh is rejected.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import RingConfig
from ..store import Store

#: Refresh this many seconds before the access token actually expires.
REFRESH_MARGIN_S = 600.0


class RelinkRequired(RuntimeError):
    """The refresh token is no longer usable; the household must re-authorise."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""
    account_id: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - REFRESH_MARGIN_S


def _obfuscate(value: str, key: str) -> str:
    """Reversible at-rest transform for the local single-file deployment.

    This is deliberately not presented as encryption. In the AWS deployment the
    token set is held in Secrets Manager under a KMS customer-managed key and
    this function is unused; see docs/AWS.md. It exists so that a developer
    running the simulator locally does not leave a bearer token in plaintext in
    a SQLite file they may well commit by accident.
    """
    stream = hashlib.blake2b(key.encode(), digest_size=32).digest()
    raw = value.encode()
    extended = (stream * (len(raw) // len(stream) + 1))[: len(raw)]
    return base64.b64encode(bytes(a ^ b for a, b in zip(raw, extended))).decode()


def _deobfuscate(value: str, key: str) -> str:
    stream = hashlib.blake2b(key.encode(), digest_size=32).digest()
    raw = base64.b64decode(value.encode())
    extended = (stream * (len(raw) // len(stream) + 1))[: len(raw)]
    return bytes(a ^ b for a, b in zip(raw, extended)).decode()


class TokenService:
    """Holds the household's Ring tokens and keeps them current."""

    def __init__(self, config: RingConfig, store: Store, *, http: httpx.AsyncClient | None = None):
        self.config = config
        self.store = store
        self._http = http
        self._pending_states: dict[str, float] = {}

    # --- linking ----------------------------------------------------------
    def authorization_url(self, scopes: str = "ava") -> tuple[str, str]:
        """Build the consent URL. Returns ``(url, state)``."""
        state = secrets.token_urlsafe(24)
        self._pending_states[state] = time.time()
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "response_type": "code",
                "redirect_uri": self.config.redirect_uri,
                "scope": scopes,
                "state": state,
            }
        )
        return f"{self.config.oauth_authorize_url}?{query}", state

    def validate_state(self, state: str, max_age_s: float = 900.0) -> bool:
        issued = self._pending_states.pop(state, None)
        return issued is not None and (time.time() - issued) <= max_age_s

    async def exchange_code(self, code: str) -> TokenSet:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.redirect_uri,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        }
        return await self._token_request(payload)

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        }
        try:
            return await self._token_request(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401):
                self.store.set_meta("relink_required", True)
                raise RelinkRequired("Ring rejected the refresh token") from exc
            raise

    async def _token_request(self, payload: dict[str, Any]) -> TokenSet:
        client = self._http or httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.post(self.config.oauth_token_url, data=payload)
            response.raise_for_status()
            body = response.json()
        finally:
            if self._http is None:
                await client.aclose()

        tokens = TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", payload.get("refresh_token", "")),
            expires_at=time.time() + float(body.get("expires_in", 14400)),
            scope=body.get("scope", ""),
            account_id=str(body.get("account_id", "")),
        )
        self.save(tokens)
        self.store.set_meta("relink_required", False)
        return tokens

    # --- persistence ------------------------------------------------------
    def save(self, tokens: TokenSet) -> None:
        blob = asdict(tokens)
        key = self.config.client_secret or self.config.hmac_key
        blob["access_token"] = _obfuscate(tokens.access_token, key)
        blob["refresh_token"] = _obfuscate(tokens.refresh_token, key)
        self.store.set_meta("ring_tokens", blob)

    def load(self) -> TokenSet | None:
        blob = self.store.get_meta("ring_tokens")
        if not blob:
            return None
        key = self.config.client_secret or self.config.hmac_key
        return TokenSet(
            access_token=_deobfuscate(blob["access_token"], key),
            refresh_token=_deobfuscate(blob["refresh_token"], key),
            expires_at=blob["expires_at"],
            scope=blob.get("scope", ""),
            account_id=blob.get("account_id", ""),
        )

    # --- token provider for RingClient -----------------------------------
    async def bearer(self) -> str:
        tokens = self.load()
        if tokens is None:
            raise RelinkRequired("no Ring tokens stored; link the account first")
        if tokens.expired:
            tokens = await self.refresh(tokens)
        return tokens.access_token


class StaticTokenService:
    """Token provider for the simulator and for tests.

    The Ring Playground issues a one-click token valid for thirty minutes; that
    token drops straight into this class, so the same client code path runs
    against the Playground with no OAuth round trip.
    """

    def __init__(self, token: str = "sim-token"):
        self.token = token

    async def bearer(self) -> str:
        return self.token
