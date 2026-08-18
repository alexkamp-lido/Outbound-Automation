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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from .origami_client import (
    OrigamiAPIError,
    OrigamiCampaign,
    OrigamiClient,
    OrigamiPerson,
)

logger = logging.getLogger(__name__)

# Origami's list endpoint doesn't give reply timestamps — addedAt is a proxy for
# "how recent." Widened so late-cycle replies still surface.
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


def _within_lookback(added_at: Optional[str], hours: int) -> bool:
    """Origami stamps addedAt as ISO 8601 UTC. Missing timestamps err on the side of including."""
    if not added_at:
        return True
    try:
        cleaned = added_at.rstrip("Z")
        ts = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return ts >= cutoff


def collect_origami(
    client: OrigamiClient,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> tuple[list[Reply], list[ActivePerson]]:
    """Walk every discovered campaign; return (recent replies, still-active roster)."""
    replies: list[Reply] = []
    active: list[ActivePerson] = []

    try:
        campaigns = client.discover_campaigns()
    except OrigamiAPIError as e:
        logger.error("Origami campaign discovery failed: %s", e)
        return replies, active

    for c in campaigns:
        # If workspace enumeration returned a status field, skip inactive/draft; when
        # we hydrated from campaign IDs, get_campaign returns status too.
        if c.status and c.status not in ("active", "paused"):
            continue
        try:
            people = list(client.iter_people(c.id, statuses=["sent", "replied"]))
        except OrigamiAPIError as e:
            logger.warning("Origami people fetch failed for %s: %s", c.name or c.id, e)
            continue

        for p in people:
            if p.send_status == "replied":
                if _within_lookback(p.added_at, lookback_hours):
                    replies.append(_reply_from_origami(p, c))
            elif p.send_status == "sent" and not p.stop_reason:
                active.append(
                    ActivePerson(
                        platform="origami",
                        recipient=(p.recipient or "").strip().lower(),
                        campaign_name=c.name,
                        campaign_id=c.id,
                        sequence_id=p.sequence_id,
                    )
                )
    return replies, active


def _reply_from_origami(p: OrigamiPerson, c: OrigamiCampaign) -> Reply:
    channel = p.channels[0] if p.channels else ""
    return Reply(
        platform="origami",
        recipient=(p.recipient or "").strip().lower(),
        campaign_name=c.name,
        campaign_id=c.id,
        channel=channel,
        replied_at=p.added_at,
        sequence_id=p.sequence_id,
    )


def collect_plusvibe(lookback_hours: int = DEFAULT_LOOKBACK_HOURS) -> tuple[list[Reply], list[ActivePerson], bool]:
    """
    Placeholder collector for Plusvibe.

    The Plusvibe REST API does not currently expose a replies-with-labels endpoint
    that's reachable from a headless service, and MCP isn't reachable from a Railway
    process. Returns empty + connected=False so the digest can render an honest
    banner. When a read source lands, populate this function — the digest and
    reconciliation code are already generic.
    """
    return [], [], False


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
        origami_replies, origami_active = collect_origami(origami_client, lookback_hours)

    plusvibe_replies, plusvibe_active, plusvibe_connected = collect_plusvibe(lookback_hours)

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
