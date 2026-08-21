"""
Sequence Reviewer — daily digest builder.

Assembles a Slack Block Kit digest showing:
  1. Today's Origami replies (per campaign).
  2. Today's Plusvibe replies (non-OOO) — stub until a Plusvibe read source is chosen.
  3. Today's OOO auto-reply count (collapsed).
  4. Reconciliation suggestions: prospects who replied on one platform but are still
     active in the other → the recipient row to stop manually.

Pure logic lives in build_digest(). The Plusvibe half is behind a stub until the
read endpoint is resolved.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from .event_store import (
    StoredPlusvibeReply,
    StoredReply,
    list_recent_plusvibe_replies,
    list_recent_replies,
    open_store,
)
from .origami_client import (
    OrigamiAPIError,
    OrigamiCampaign,
    OrigamiClient,
)

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_HOURS = int(os.getenv("REVIEWER_LOOKBACK_HOURS", "36"))


@dataclass
class Reply:
    """Cross-platform reply row used by the digest."""

    platform: str  # "origami" | "plusvibe"
    recipient: str
    campaign_name: str
    campaign_id: str
    channel: str = ""  # "email" | "linkedin" | ""
    subject: str = ""
    snippet: str = ""
    replied_at: Optional[str] = None
    is_ooo: bool = False
    sequence_id: Optional[str] = None  # only Origami


@dataclass
class ActivePerson:
    """A person still receiving messages on a platform, used for the cross-reference join."""

    platform: str
    recipient: str
    campaign_name: str
    campaign_id: str
    sequence_id: Optional[str] = None


@dataclass
class ReconciliationRow:
    """A prospect who replied on one platform but is still active on the other."""

    recipient: str
    replied_on: str  # "origami" | "plusvibe"
    still_active_on: str
    active_campaign_name: str
    active_sequence_id: Optional[str] = None
    ooo: bool = False


@dataclass
class DigestData:
    origami_replies: list[Reply] = field(default_factory=list)
    plusvibe_replies: list[Reply] = field(default_factory=list)
    reconciliations: list[ReconciliationRow] = field(default_factory=list)
    ooo_count: int = 0
    plusvibe_connected: bool = False
    generated_at: str = ""


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect_origami_active(client: OrigamiClient) -> list[ActivePerson]:
    """Walk active/paused campaigns; return the still-in-sequence roster."""
    active: list[ActivePerson] = []
    try:
        campaigns = client.discover_campaigns()
    except OrigamiAPIError as e:
        logger.error("Origami campaign discovery failed: %s", e)
        return active

    for c in campaigns:
        if c.status and c.status not in ("active", "paused"):
            continue
        try:
            for p in client.iter_people(c.id, statuses=["sent"]):
                if p.send_status == "sent" and not p.stop_reason:
                    active.append(
                        ActivePerson(
                            platform="origami",
                            recipient=(p.recipient or "").strip().lower(),
                            campaign_name=c.name,
                            campaign_id=c.id,
                            sequence_id=p.sequence_id,
                        )
                    )
        except OrigamiAPIError as e:
            logger.warning("Origami people fetch failed for %s: %s", c.name or c.id, e)
            continue
    return active


def collect_origami_replies(
    store: sqlite3.Connection,
    client: Optional[OrigamiClient],
    lookback_hours: int,
    campaigns_by_id: Optional[dict[str, OrigamiCampaign]] = None,
) -> list[Reply]:
    """
    Read reply events landed by the webhook receiver within the lookback window,
    then resolve each row's sequence_id → campaign name (cached per-run).
    """
    stored = list_recent_replies(store, lookback_hours)
    if not stored:
        return []

    campaign_name_cache: dict[str, str] = {}
    campaigns_by_id = campaigns_by_id or {}

    def _campaign_name_for(sequence_id: str) -> str:
        if sequence_id in campaign_name_cache:
            return campaign_name_cache[sequence_id]
        campaign_id = ""
        if client is not None:
            try:
                seq = client.get_sequence(sequence_id)
                campaign_id = seq.get("campaignId") or ""
            except OrigamiAPIError as e:
                logger.warning("Sequence %s lookup failed: %s", sequence_id, e)
        name = campaigns_by_id.get(campaign_id).name if campaign_id in campaigns_by_id else ""
        if not name and campaign_id and client is not None:
            try:
                name = client.get_campaign(campaign_id).name or ""
            except OrigamiAPIError:
                name = ""
        campaign_name_cache[sequence_id] = name
        return name

    replies: list[Reply] = []
    for s in stored:
        replies.append(_reply_from_stored(s, _campaign_name_for(s.sequence_id)))
    return replies


def _reply_from_stored(s: StoredReply, campaign_name: str) -> Reply:
    return Reply(
        platform="origami",
        recipient=s.recipient,
        campaign_name=campaign_name,
        campaign_id="",
        channel=s.channel,
        subject=s.subject or "",
        snippet=s.snippet or "",
        replied_at=s.received_at,
        sequence_id=s.sequence_id,
    )


def collect_plusvibe_replies(
    store: sqlite3.Connection, lookback_hours: int
) -> list[Reply]:
    """Read Plusvibe reply events (persisted from Slack notifications) within lookback."""
    stored = list_recent_plusvibe_replies(store, lookback_hours)
    return [_reply_from_plusvibe(s) for s in stored]


def _reply_from_plusvibe(s: StoredPlusvibeReply) -> Reply:
    return Reply(
        platform="plusvibe",
        recipient=s.recipient,
        campaign_name=s.campaign_name,
        campaign_id="",
        channel="email",
        subject=(s.label or ""),  # label surfaced as subject-ish in the digest
        snippet="",  # notification carries no body
        replied_at=s.received_at,
        is_ooo=s.is_ooo,
    )


# ---------------------------------------------------------------------------
# Reconciliation join
# ---------------------------------------------------------------------------


def compute_reconciliations(
    origami_replies: Iterable[Reply],
    plusvibe_replies: Iterable[Reply],
    origami_active: Iterable[ActivePerson],
    plusvibe_active: Iterable[ActivePerson],
) -> list[ReconciliationRow]:
    """Cross-reference replied-on-one-side against active-on-the-other."""
    plusvibe_active_by_email = {a.recipient: a for a in plusvibe_active if a.recipient}
    origami_active_by_email = {a.recipient: a for a in origami_active if a.recipient}

    rows: list[ReconciliationRow] = []

    # Origami replies → still active in Plusvibe?
    for r in origami_replies:
        active = plusvibe_active_by_email.get(r.recipient)
        if active:
            rows.append(
                ReconciliationRow(
                    recipient=r.recipient,
                    replied_on="origami",
                    still_active_on="plusvibe",
                    active_campaign_name=active.campaign_name,
                    active_sequence_id=None,
                )
            )

    # Plusvibe replies (non-OOO) → still active in Origami?
    for r in plusvibe_replies:
        if r.is_ooo:
            continue
        active = origami_active_by_email.get(r.recipient)
        if active:
            rows.append(
                ReconciliationRow(
                    recipient=r.recipient,
                    replied_on="plusvibe",
                    still_active_on="origami",
                    active_campaign_name=active.campaign_name,
                    active_sequence_id=active.sequence_id,
                )
            )

    return rows


# ---------------------------------------------------------------------------
# Block Kit rendering
# ---------------------------------------------------------------------------


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _divider() -> dict:
    return {"type": "divider"}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _truncate(s: str, n: int = 140) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_digest(data: DigestData) -> list[dict]:
    blocks: list[dict] = []
    generated = data.generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    blocks.append(_header("Sequence Reviewer — daily rundown"))
    blocks.append(_context(f"Generated {generated}"))

    # --- Reconciliation section (action item, first) ---
    blocks.append(_divider())
    blocks.append(_header("Reconcile these sequences"))
    if data.reconciliations:
        for row in data.reconciliations:
            if row.still_active_on == "plusvibe":
                action = (
                    f"*Stop in Plusvibe:* `{row.recipient}` — replied on Origami. "
                    f"Still active in Plusvibe campaign _{row.active_campaign_name}_."
                )
            else:
                action = (
                    f"*Stop in Origami:* `{row.recipient}` — replied on Plusvibe. "
                    f"Still active in Origami campaign _{row.active_campaign_name}_."
                )
                if row.active_sequence_id:
                    action += f" (`sequence_id={row.active_sequence_id}`)"
            blocks.append(_section(action))
    else:
        blocks.append(_section("_No cross-platform overlap today._"))

    # --- Origami replies ---
    blocks.append(_divider())
    blocks.append(_header(f"Origami replies today ({len(data.origami_replies)})"))
    if data.origami_replies:
        for r in data.origami_replies[:25]:
            ch = f" · _{r.channel}_" if r.channel else ""
            blocks.append(
                _section(
                    f"`{r.recipient}`{ch} — _{r.campaign_name}_"
                    + (f"\n> {_truncate(r.snippet)}" if r.snippet else "")
                )
            )
        if len(data.origami_replies) > 25:
            blocks.append(_context(f"+{len(data.origami_replies) - 25} more not shown."))
    else:
        blocks.append(_section("_No Origami replies in the last day._"))

    # --- Plusvibe replies ---
    blocks.append(_divider())
    if data.plusvibe_connected:
        non_ooo = [r for r in data.plusvibe_replies if not r.is_ooo]
        blocks.append(_header(f"Plusvibe replies today ({len(non_ooo)})"))
        if non_ooo:
            for r in non_ooo[:25]:
                subject = f" — _{r.subject}_" if r.subject else ""
                blocks.append(
                    _section(
                        f"`{r.recipient}`{subject}\n_{r.campaign_name}_"
                        + (f"\n> {_truncate(r.snippet)}" if r.snippet else "")
                    )
                )
            if len(non_ooo) > 25:
                blocks.append(_context(f"+{len(non_ooo) - 25} more not shown."))
        else:
            blocks.append(_section("_No Plusvibe replies in the last day._"))
        if data.ooo_count:
            blocks.append(_context(f":sleeping: {data.ooo_count} out-of-office auto-replies hidden."))
    else:
        blocks.append(_header("Plusvibe replies today"))
        blocks.append(
            _section(
                "_Plusvibe read source not connected yet._ "
                "Reconciliation against Plusvibe activity is not included in this digest."
            )
        )

    return blocks


# ---------------------------------------------------------------------------
# Orchestrator (entrypoint used by app.py)
# ---------------------------------------------------------------------------


def run_reviewer(
    origami_client: Optional[OrigamiClient] = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> tuple[list[dict], DigestData]:
    """Collect, cross-reference, and return (block_kit_blocks, digest_data)."""
    origami_replies: list[Reply] = []
    origami_active: list[ActivePerson] = []

    if origami_client is None:
        try:
            origami_client = OrigamiClient()
        except OrigamiAPIError as e:
            logger.warning("Origami client unavailable: %s", e)
            origami_client = None

    if origami_client is not None:
        origami_active = collect_origami_active(origami_client)

    store = open_store()
    try:
        origami_replies = collect_origami_replies(store, origami_client, lookback_hours)
        plusvibe_replies = collect_plusvibe_replies(store, lookback_hours)
    finally:
        store.close()

    plusvibe_active: list[ActivePerson] = []  # no Plusvibe active-roster source yet
    plusvibe_connected = True  # Slack channel is the source of truth

    reconciliations = compute_reconciliations(
        origami_replies, plusvibe_replies, origami_active, plusvibe_active
    )
    ooo_count = sum(1 for r in plusvibe_replies if r.is_ooo)

    data = DigestData(
        origami_replies=origami_replies,
        plusvibe_replies=plusvibe_replies,
        reconciliations=reconciliations,
        ooo_count=ooo_count,
        plusvibe_connected=plusvibe_connected,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    return build_digest(data), data
