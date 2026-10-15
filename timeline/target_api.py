"""Minimal Adobe Target Admin API client (read-only).

Auth: OAuth Server-to-Server credentials from Adobe Developer Console.
Endpoints used:
  GET https://mc.adobe.io/{tenant}/target/activities
  GET https://mc.adobe.io/{tenant}/target/activities/{id}/changelog
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
DEFAULT_SCOPES = "openid,AdobeID,target_sdk,additional_info.projectedProductContext,read_organizations,additional_info.roles"


class TargetAPIError(RuntimeError):
    pass


@dataclass
class TargetConfig:
    tenant: str
    client_id: str
    client_secret: str
    scopes: str = DEFAULT_SCOPES
    base_url: str = "https://mc.adobe.io"
    # Path to the per-activity history endpoint. Swap this if you move to the
    # newer Revisions endpoint on developer.adobe.com.
    history_path: str = "/{tenant}/target/activities/{id}/changelog"
    accept_header: str = "application/vnd.adobe.target.v1+json"
    page_size: int = 100
    timeout: int = 30


class TargetClient:
    def __init__(self, cfg: TargetConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expires: float = 0

    # ---------------------------------------------------------------- auth
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        resp = self.session.post(
            IMS_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "scope": self.cfg.scopes,
            },
            timeout=self.cfg.timeout,
        )
        if resp.status_code != 200:
            raise TargetAPIError(f"Adobe login failed ({resp.status_code}): {resp.text[:300]}")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 86400))
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "X-Api-Key": self.cfg.client_id,
            "Accept": self.cfg.accept_header,
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.cfg.base_url.rstrip("/") + path
        for attempt in range(5):
            resp = self.session.get(url, headers=self._headers(), params=params, timeout=self.cfg.timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 30))
                continue
            if resp.status_code == 401 and attempt == 0:
                self._token = None  # token expired early; retry once
                continue
            raise TargetAPIError(f"GET {path} failed ({resp.status_code}): {resp.text[:300]}")
        raise TargetAPIError(f"GET {path} failed after retries")

    # ------------------------------------------------------------ endpoints
    def list_activities(self) -> list[dict]:
        """All activities in the account (every workspace the credential can see)."""
        out, offset = [], 0
        while True:
            body = self._get(
                f"/{self.cfg.tenant}/target/activities",
                params={"limit": self.cfg.page_size, "offset": offset},
            )
            batch = body.get("activities") or []
            out.extend(batch)
            total = int(body.get("total") or 0)
            offset += len(batch)
            if not batch or (total and offset >= total):
                break
        return out

    def activity_history(self, activity_id) -> list[dict]:
        """Change log entries for one activity, oldest and newest included."""
        path = self.cfg.history_path.format(tenant=self.cfg.tenant, id=activity_id)
        out, offset = [], 0
        while True:
            body = self._get(path, params={"limit": self.cfg.page_size, "offset": offset})
            batch = (
                body.get("activityChangelogs")
                or body.get("revisions")
                or body.get("items")
                or (body if isinstance(body, list) else [])
            )
            out.extend(batch)
            total = int(body.get("total") or 0) if isinstance(body, dict) else 0
            offset += len(batch)
            if not batch or not total or offset >= total:
                break
        return out
