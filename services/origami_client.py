"""
Origami Sequencer API client (v2).

Read-only surface used by the Sequence Reviewer: enumerate campaigns and their
enrolled people.

Full API reference: ~/.claude/skills/origami-sequencer/SKILL.md
Base URL: https://origami.chat/api/v2
Auth: Authorization: Bearer $ORIGAMI_API_KEY
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Iterator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE_URL = "https://origami.chat/api/v2"
DEFAULT_PAGE_SIZE = 100


class OrigamiAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class OrigamiAuthenticationError(OrigamiAPIError):
    pass


class OrigamiNotFoundError(OrigamiAPIError):
    pass


@dataclass
class OrigamiCampaign:
    id: str
    slug: str
    name: str
    status: str
    people_count: int = 0
    workspace_id: Optional[str] = None


@dataclass
class OrigamiPerson:
    """One person enrolled in a campaign — the campaign_person object."""

    sequence_id: str
    row_id: Optional[str]
    recipient: str
    send_status: str
    stop_reason: Optional[str]
    sender_id: Optional[str]
    channels: list[str] = field(default_factory=list)
    added_at: Optional[str] = None
    campaign_id: Optional[str] = None


class OrigamiClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        workspace_id: Optional[str] = None,
        campaign_ids: Optional[list[str]] = None,
        bootstrap_campaign_id: Optional[str] = None,
        timeout: int = 30,
    ):
        self.api_key = api_key or os.getenv("ORIGAMI_API_KEY")
        if not self.api_key:
            raise OrigamiAuthenticationError(
                "Set ORIGAMI_API_KEY or pass api_key."
            )
        self.workspace_id = workspace_id or os.getenv("ORIGAMI_WORKSPACE_ID") or None
        self.bootstrap_campaign_id = (
            bootstrap_campaign_id or os.getenv("ORIGAMI_BOOTSTRAP_CAMPAIGN_ID") or None
        )
        env_campaigns = os.getenv("ORIGAMI_CAMPAIGN_IDS", "")
        self.campaign_ids = campaign_ids or [c.strip() for c in env_campaigns.split(",") if c.strip()]
        self.timeout = timeout
        self.base_url = BASE_URL
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        })
        return session

    def _handle(self, r: requests.Response) -> dict:
        if r.status_code == 200:
            return r.json()
        try:
            body = r.json()
            msg = body.get("message") or body.get("error") or r.text
        except ValueError:
            msg = r.text or f"HTTP {r.status_code}"
        if r.status_code == 401:
            raise OrigamiAuthenticationError(msg, r.status_code)
        if r.status_code == 404:
            raise OrigamiNotFoundError(msg, r.status_code)
        raise OrigamiAPIError(msg, r.status_code)

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.get(url, params=params or {}, timeout=self.timeout)
        except requests.RequestException as e:
            raise OrigamiAPIError(f"Network error: {e}") from e
        return self._handle(r)

    def get_campaign(self, campaign_id: str) -> OrigamiCampaign:
        """Fetch a single campaign; useful for discovering workspace_id or basic metadata."""
        payload = self._get(f"/campaigns/{campaign_id}")
        return OrigamiCampaign(
            id=payload.get("id", campaign_id),
            slug=payload.get("slug", ""),
            name=payload.get("name", ""),
            status=payload.get("status", ""),
            people_count=payload.get("peopleCount", 0),
            workspace_id=payload.get("workspaceId"),
        )

    def list_workspace_campaigns(self, workspace_id: Optional[str] = None) -> list[OrigamiCampaign]:
        """List campaigns in a workspace. Origami returns a single page (nextCursor is null)."""
        ws = workspace_id or self.workspace_id
        if not ws:
            raise OrigamiAPIError("workspace_id required (set ORIGAMI_WORKSPACE_ID or pass it in).")
        payload = self._get(f"/workspaces/{ws}/campaigns")
        items = payload.get("items", [])
        return [
            OrigamiCampaign(
                id=item["id"],
                slug=item.get("slug", ""),
                name=item.get("name", ""),
                status=item.get("status", ""),
                people_count=item.get("peopleCount", 0),
                workspace_id=ws,
            )
            for item in items
            if item.get("id")
        ]

    def discover_campaigns(self) -> list[OrigamiCampaign]:
        """
        Return the campaigns the reviewer should scan.

        Priority:
          1. ORIGAMI_WORKSPACE_ID set → enumerate every campaign in that workspace.
          2. ORIGAMI_BOOTSTRAP_CAMPAIGN_ID set → fetch that one campaign, learn its
             workspaceId from the response, then enumerate every campaign in the
             workspace. Discovered workspace_id is cached on the instance so
             subsequent calls skip the extra GET.
          3. ORIGAMI_CAMPAIGN_IDS set → hydrate each listed campaign via /campaigns/:id.
             (Manual, does not auto-pick up new campaigns.)

        Callers get a uniform OrigamiCampaign list either way.
        """
        if self.workspace_id:
            return self.list_workspace_campaigns()

        if self.bootstrap_campaign_id:
            bootstrap = self.get_campaign(self.bootstrap_campaign_id)
            if bootstrap.workspace_id:
                self.workspace_id = bootstrap.workspace_id  # cache for future calls
                logger.info(
                    "Discovered workspace_id=%s from bootstrap campaign %s",
                    bootstrap.workspace_id,
                    self.bootstrap_campaign_id,
                )
                return self.list_workspace_campaigns()
            logger.warning(
                "Bootstrap campaign %s did not carry workspaceId; falling back to manual list.",
                self.bootstrap_campaign_id,
            )

        if not self.campaign_ids:
            raise OrigamiAPIError(
                "No campaigns to review: set ORIGAMI_WORKSPACE_ID, "
                "ORIGAMI_BOOTSTRAP_CAMPAIGN_ID, or ORIGAMI_CAMPAIGN_IDS."
            )
        campaigns: list[OrigamiCampaign] = []
        for cid in self.campaign_ids:
            try:
                campaigns.append(self.get_campaign(cid))
            except OrigamiAPIError as e:
                logger.warning("Skipping campaign %s: %s", cid, e)
        return campaigns

    def iter_people(
        self,
        campaign_id: str,
        statuses: Optional[list[str]] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[OrigamiPerson]:
        """
        Yield all campaign_person rows for a campaign, paging through nextCursor.

        Args:
            campaign_id: campaign to enumerate.
            statuses: CSV of send-status buckets (e.g. ["sent"], ["replied"], or ["sent","replied"]).
            page_size: max 100 per Origami's pagination contract.
        """
        cursor: Optional[str] = None
        params: dict = {"limit": page_size}
        if statuses:
            params["status"] = ",".join(statuses)

        while True:
            req_params = dict(params)
            if cursor:
                req_params["cursor"] = cursor
            payload = self._get(f"/campaigns/{campaign_id}/people", req_params)
            for item in payload.get("items", []):
                senders = item.get("senders") or []
                channels = [s.get("channel") for s in senders if s.get("channel")]
                yield OrigamiPerson(
                    sequence_id=item.get("sequenceId", ""),
                    row_id=item.get("rowId"),
                    recipient=item.get("recipient", ""),
                    send_status=item.get("sendStatus", ""),
                    stop_reason=item.get("stopReason"),
                    sender_id=item.get("senderId"),
                    channels=channels,
                    added_at=item.get("addedAt"),
                    campaign_id=campaign_id,
                )
            cursor = payload.get("nextCursor")
            if not cursor:
                return

    def stop_sequence(self, sequence_id: str, dry_run: bool = False) -> dict:
        """
        Stop one person's sequence. Idempotent; sent history is preserved.
        Not wired to any auto-execution today — kept here so a future reviewer
        can add a Slack-approve → stop path without another client.
        """
        params = {"dryRun": "true"} if dry_run else None
        url = f"{self.base_url}/sequences/{sequence_id}/stop"
        try:
            r = self.session.post(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise OrigamiAPIError(f"Network error: {e}") from e
        return self._handle(r)
