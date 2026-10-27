"""Checks against the client's records before any guideline is applied: the matter exists, the timekeeper is
authorised on it, the codes are allowed. These are facts in the system of record, so code decides them.
"""

from __future__ import annotations

from dataclasses import dataclass

from lira import ledes
from lira.domain import Evidence, Finding, FindingKind, Invoice, Matter
from lira.reference import Reference
from lira.utbms import ACTIVITY_CODES


@dataclass
class Validation:
    fatal: list[ledes.Issue]
    findings: list[Finding]
    matter: Matter | None


def validate(inv: Invoice, ref: Reference) -> Validation:
    fatal = [i for i in ledes.check(inv) if i.fatal and i.line_no is None]
    findings: list[Finding] = []
    for i in ledes.check(inv):
        if i.line_no is not None:
            findings.append(
                Finding(
                    id=f"{inv.number}:{i.line_no}:format:{i.code}",
                    invoice=inv.number,
                    line_no=i.line_no,
                    kind=FindingKind.FORMAT,
                    source="validation",
                    decided_by="rule",
                    action="flag" if not i.fatal else "reject",
                    rationale=i.message,
                    evidence=[Evidence(source="rule", text=i.message)],
                )
            )
    tenant = ref.tenant_for_client(inv.client_id)
    matter = ref.matters.get(inv.client_matter_id)
    if tenant is None:
        fatal.append(ledes.Issue("client", f"unknown CLIENT_ID {inv.client_id}"))
    if matter is None or (tenant is not None and matter.tenant != tenant.id):
        fatal.append(ledes.Issue("matter", f"CLIENT_MATTER_ID {inv.client_matter_id} is not a matter of this client"))
        return Validation(fatal, findings, None)
    if matter.firm_id != inv.law_firm_id:
        fatal.append(ledes.Issue("firm", f"matter {matter.id} is not assigned to firm {inv.law_firm_id}"))
    firm = ref.firms.get(matter.firm_id)
    for li in inv.lines:
        if li.is_fee:
            tk = firm.timekeeper(li.timekeeper_id) if firm else None
            if tk is None or li.timekeeper_id not in matter.authorized_timekeepers:
                findings.append(
                    Finding(
                        id=f"{inv.number}:{li.line_no}:unauthorized",
                        invoice=inv.number,
                        line_no=li.line_no,
                        kind=FindingKind.UNAUTHORIZED,
                        source="validation",
                        decided_by="rule",
                        action="reject",
                        adjustment=-li.total,
                        rationale=f"Timekeeper {li.timekeeper_id} is not authorised on matter {matter.id}.",
                        evidence=[Evidence(source="rule", text=f"authorised timekeepers: {len(matter.authorized_timekeepers)}")],
                    )
                )
            if li.task_code not in matter.authorized_task_codes:
                findings.append(
                    Finding(
                        id=f"{inv.number}:{li.line_no}:format:task_code",
                        invoice=inv.number,
                        line_no=li.line_no,
                        kind=FindingKind.FORMAT,
                        source="validation",
                        decided_by="rule",
                        action="flag",
                        rationale=f"Task code {li.task_code} is not authorised for this matter.",
                    )
                )
            if li.activity_code and li.activity_code not in ACTIVITY_CODES:
                findings.append(
                    Finding(
                        id=f"{inv.number}:{li.line_no}:format:activity_code",
                        invoice=inv.number,
                        line_no=li.line_no,
                        kind=FindingKind.FORMAT,
                        source="validation",
                        decided_by="rule",
                        action="flag",
                        rationale=f"Activity code {li.activity_code} is not a UTBMS activity code.",
                    )
                )
        elif li.is_expense and li.expense_code not in matter.authorized_expense_codes:
            findings.append(
                Finding(
                    id=f"{inv.number}:{li.line_no}:format:expense_code",
                    invoice=inv.number,
                    line_no=li.line_no,
                    kind=FindingKind.FORMAT,
                    source="validation",
                    decided_by="rule",
                    action="flag",
                    rationale=f"Expense code {li.expense_code} is not authorised for this matter.",
                )
            )
    return Validation(fatal, findings, matter)
