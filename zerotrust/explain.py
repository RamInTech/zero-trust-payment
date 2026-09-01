"""Reconstruct why a request was approved or denied, from the audit log alone.

Phase 4's completion test asked whether a reviewer holding only the log could
explain any decision. This is that question answered as an API rather than as a
human exercise.

The output is deliberately structured -- WHY / WHAT / EVIDENCE -- rather than
prose. A parseable explanation can be asserted on, diffed and rendered; free
text can only be read and hoped over. The optional narrative is a convenience
laid on top, never the substance.

Read-only by construction: it takes an AuditLog and calls `for_request()`. It
holds no store, no engine, and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from zerotrust.audit import AuditEntry, AuditLog, EventType
from zerotrust.narrate import ExplanationWriter

#: The event that settles a request, most specific first.
_VERDICT_EVENTS = (EventType.POLICY_DENIED, EventType.POLICY_APPROVED)

#: How a request ended, in the order we prefer to report it.
_OUTCOME_EVENTS = [
    (EventType.PAYMENT_PENDING_VERIFICATION, "PENDING_VERIFICATION"),
    (EventType.PAYMENT_FAILED, "FAILED"),
    (EventType.USER_DECLINED, "DECLINED"),
    (EventType.IDEMPOTENCY_CONFLICT, "REJECTED_CONFLICT"),
    (EventType.IDEMPOTENCY_REPLAYED, "REPLAYED"),
    (EventType.PAYMENT_CAPTURED, "COMPLETED"),
    (EventType.POLICY_DENIED, "DENIED"),
]


class UnknownRequest(LookupError):
    """No audit entries exist for this request id."""


@dataclass(frozen=True)
class Explanation:
    request_id: str
    what: dict
    why: dict
    evidence: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "what": self.what,
            "why": self.why,
            "evidence": self.evidence,
        }


def explain(
    audit: AuditLog,
    request_id: str,
    narrator: Optional[ExplanationWriter] = None,
) -> Explanation:
    entries = audit.for_request(request_id)
    if not entries:
        raise UnknownRequest(f"no audit entries for request '{request_id}'")

    types = {e.event_type for e in entries}
    outcome = "IN_PROGRESS"
    for event_type, label in _OUTCOME_EVENTS:
        if event_type in types:
            outcome = label
            break

    opening = entries[0]
    verdict = next((e for e in entries if e.event_type in _VERDICT_EVENTS), None)
    confirmed = any(e.event_type is EventType.USER_CONFIRMED for e in entries)

    what = {
        "agent_id": opening.agent_id,
        "sku": opening.details.get("sku"),
        "amount_paise": opening.details.get("amount_paise")
        or opening.details.get("displayed_amount_paise"),
        "idempotency_key": next(
            (e.idempotency_key for e in entries if e.idempotency_key), None),
        "outcome": outcome,
        "started_at": opening.occurred_at,
        "settled_at": entries[-1].occurred_at,
        "money_moved": EventType.PAYMENT_CAPTURED in types,
    }

    if verdict is None:
        why = {
            "decision": "UNDECIDED",
            "decided_by": None,
            "rule": None,
            "reason": "the request never reached the policy engine",
            "mandate_id": None,
            "human_confirmed": confirmed,
            "figures": {},
        }
    else:
        approved = verdict.event_type is EventType.POLICY_APPROVED
        why = {
            "decision": "APPROVED" if approved else "DENIED",
            "decided_by": verdict.actor.value,
            "rule": verdict.rule,
            "reason": verdict.reason,
            "mandate_id": verdict.mandate_id,
            # Recorded because it is the project's central claim: a human
            # saying yes and the policy engine saying yes are separate events.
            "human_confirmed": confirmed,
            "figures": {
                k: v for k, v in verdict.details.items()
                if isinstance(v, (int, float, str))
            },
        }

    evidence = []
    for entry in entries:
        item = {
            "event_id": entry.event_id,
            "event_type": entry.event_type.value,
            "actor": entry.actor.value,
            "occurred_at": entry.occurred_at,
            "rule": entry.rule,
            "reason": entry.reason,
        }
        if narrator is not None:
            item["narrative"] = narrator.narrate(entry)
        evidence.append(item)

    return Explanation(request_id=request_id, what=what, why=why,
                       evidence=evidence)
