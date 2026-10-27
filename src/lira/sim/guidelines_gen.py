"""Write a client's outside-counsel billing guidelines as a document, from the parameters in config/world.yaml.

Each tenant writes in its own style (numbering, headings, wording), and each document also contains the
policy text that carries no checkable rule (confidentiality, conflicts, reporting), so a compiler has to find
the rules rather than read them off a template. The answer key (which rule is stated in which clause) is
returned separately and only goes to data/truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from lira.utbms import EXPENSE_CODES

ROLE_LABEL = {"summer associate": "summer associates", "law clerk": "law clerks", "first-year associate": "first-year associates",
              "trainee": "trainees"}


def _money(x: float, cur: str) -> str:
    sym = {"EUR": "EUR ", "GBP": "£", "USD": "$"}[cur]
    return f"{sym}{x:.2f}"


def _pct(f: float) -> str:
    return f"{int(round(f * 100))}%"


@dataclass
class Doc:
    tenant: str
    version: int
    effective_from: date
    style: str
    sections: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)  # (heading, [(clause_id, text)])
    answer: list[dict[str, Any]] = field(default_factory=list)

    def render(self, title: str) -> str:
        out = [f"# {title}", ""]
        if self.style == "numbered":
            out += [f"Version {self.version}. Effective {self.effective_from:%d %B %Y}.", ""]
        elif self.style == "uk":
            out += [f"Issue {self.version}, in force from {self.effective_from:%d %B %Y}", ""]
        else:
            out += [f"Revision {self.version} (effective {self.effective_from:%B %d, %Y})", ""]
        for heading, clauses in self.sections:
            out += [f"## {heading}", ""]
            for cid, text in clauses:
                if self.style == "letters" and cid.endswith(")"):
                    out.append(f"({cid.split('(', 1)[1]} {text}")
                elif self.style == "uk":
                    out.append(f"§ {cid} {text}")
                else:
                    out.append(f"{cid} {text}")
                out.append("")
        return "\n".join(out).rstrip() + "\n"


def build(tenant: str, name: str, currency: str, style: str, version: int, effective_from: date, p: dict[str, Any]) -> Doc:
    """p holds the guideline parameters for this version (see config/world.yaml)."""
    d = Doc(tenant, version, effective_from, style)
    sec = 0

    def section(title_numbered: str, title_uk: str, title_letters: str) -> list[tuple[str, str]]:
        nonlocal sec
        sec += 1
        heading = {"numbered": f"{sec}. {title_numbered}", "uk": f"Part {sec} - {title_uk}",
                   "letters": f"Section {sec}: {title_letters}"}[style]
        clauses: list[tuple[str, str]] = []
        d.sections.append((heading, clauses))
        return clauses

    def add(clauses: list[tuple[str, str]], text: str, rule: dict[str, Any] | None = None) -> str:
        n = len(clauses) + 1
        # the "letters" style writes 4.1 followed by (b), (c), ... which the parser cites as 4.1(b), 4.1(c)
        cid = f"{sec}.1({chr(96 + n)})" if style == "letters" and n > 1 else f"{sec}.{n}"
        clauses.append((cid, text))
        if rule:
            d.answer.append({**rule, "clause_id": cid})
        return cid

    # ---- 1 scope (no rules)
    c = section("Purpose and scope", "Scope and application", "General Provisions")
    add(c, f"These guidelines apply to all outside counsel engaged by {name} and its affiliates. They form part of every "
           "engagement letter, and a conflict between an engagement letter and these guidelines is resolved in favour of these guidelines "
           "unless the engagement letter expressly says otherwise.")
    add(c, "Counsel must confirm in writing that they have read these guidelines before any work is billed. Questions about "
           "the guidelines should be addressed to the Legal Operations team before the work is performed.")
    add(c, "We expect counsel to staff matters efficiently, avoid duplication of effort and keep the responsible in-house lawyer "
           "informed of significant developments, including any change in the assessment of risk.")

    # ---- 2 staffing and rates
    c = section("Staffing and rates", "Staffing and hourly rates", "Staffing and Rates")
    rate_text = {
        "numbered": "Only the hourly rates approved in the rate schedule for the matter may be billed. Rates are fixed for the life "
                    "of the matter; a rate increase is effective only once Legal Operations has approved it in writing.",
        "uk": "Fees must be charged at the hourly rates agreed for each fee earner on the matter. Any increase in rates requires our "
              "prior written approval and will not be applied retrospectively.",
        "letters": "Timekeepers may bill only at the rates approved for them in the matter's rate schedule. Rate increases are not "
                   "accepted unless approved in advance in writing by the Law Department.",
    }[style]
    add(c, rate_text, {"kind": "rate_cap"})
    if p.get("nonbillable_roles"):
        roles = p["nonbillable_roles"]
        names = [ROLE_LABEL[r] for r in roles]
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        text = {
            "numbered": f"Time recorded by {listed} is not billable to {name}, whatever the task.",
            "uk": f"We will not pay for time spent by {listed}.",
            "letters": f"The Company will not be charged for work performed by {listed}.",
        }[style]
        add(c, text, {"kind": "nonbillable_role", "roles": roles})
    add(c, "Counsel must notify us before adding a new timekeeper to a matter. We may decline to pay for time spent bringing "
           "new team members up to speed on the matter.")
    if p.get("max_attendees"):
        for ev, n in p["max_attendees"].items():
            n_word = {1: "one", 2: "two", 3: "three"}[n]
            evs = {"deposition": "deposition", "hearing": "hearing or court appearance"}[ev]
            text = {
                "numbered": f"No more than {n_word} ({n}) timekeepers may bill for attending the same {evs} without prior approval.",
                "uk": f"Attendance at a {evs} is limited to {n_word} fee earner{'s' if n > 1 else ''}; time of additional attendees will not be paid.",
                "letters": f"The Company will pay for at most {n} attorney{'s' if n > 1 else ''} attending any {evs}.",
            }[style]
            add(c, text, {"kind": "max_attendees", "event": ev, "max_timekeepers": n})

    # ---- 3 time recording
    c = section("Time recording", "Recording of time", "Time Entries")
    inc = p["increment"]
    text = {
        "numbered": "Time must be recorded in increments of one-tenth of an hour (0.1). Minimum charges and rounding up to larger units are not accepted.",
        "uk": "All time must be recorded in units of six minutes (0.1 hour); we do not accept minimum charges for letters, emails or calls.",
        "letters": "Time shall be billed in tenth-of-an-hour (0.1) increments. Quarter-hour billing will not be accepted.",
    }[style]
    assert str(inc) == "0.1"
    add(c, text, {"kind": "billing_increment", "hours": inc})
    blk = p["block_billing"]
    if blk["action"] == "reduce":
        text = {
            "numbered": f"Each time entry must describe a single task with the time spent on it. Block billing (several tasks in one entry) "
                        f"is not permitted; block-billed entries will be reduced by {_pct(blk['factor'])}.",
            "uk": f"Block billing is not acceptable. Where several tasks are recorded in one entry, we will reduce the entry by {_pct(blk['factor'])}.",
            "letters": f"Block billing is prohibited. Any entry that combines multiple tasks will be reduced by {_pct(blk['factor'])}.",
        }[style]
    else:
        text = {
            "numbered": "Each time entry must describe a single task with the time spent on it. Block-billed entries (several tasks in one entry) will be rejected in full.",
            "uk": "Block billing is not acceptable, and entries combining several tasks will not be paid.",
            "letters": "Block billing is prohibited. Entries combining multiple tasks will be rejected.",
        }[style]
    add(c, text, {"kind": "block_billing", "action": blk["action"], "reduce_factor": blk.get("factor")})
    if p.get("vague"):
        text = {
            "numbered": "Descriptions must say what was done and why. Vague entries such as \"attention to file\", \"work on matter\" or \"research\" "
                        "without a subject will be rejected.",
            "uk": "Narratives must be sufficiently detailed to show the work done. Generic descriptions (for example \"review documents\" or "
                  "\"work on matter\") will not be paid.",
            "letters": "Each entry must contain a specific description of the services. Vague descriptions will be rejected.",
        }[style]
        add(c, text, {"kind": "vague_description"})
    if p.get("max_daily_hours"):
        h = p["max_daily_hours"]
        text = {
            "numbered": f"A timekeeper may not bill more than {h} hours in a single day on our matters without prior written approval.",
            "uk": f"We will not pay for more than {h} hours charged by one fee earner on any one day.",
            "letters": f"No timekeeper may bill more than {h} hours per day.",
        }[style]
        add(c, text, {"kind": "max_daily_hours", "hours": h})
    add(c, "Time entries must be recorded on the day the work is performed. Reconstructed time is not acceptable.")

    # ---- 4 non-billable work
    c = section("Non-billable work", "Work we will not pay for", "Non-Billable Services")
    if p.get("clerical"):
        text = {
            "numbered": "Administrative and clerical work is overhead and is not billable, whoever performs it. This includes filing, copying, "
                        "scanning, scheduling, calendaring, organising files and assembling binders.",
            "uk": "Secretarial and administrative tasks, such as photocopying, filing, diary management, scheduling and arranging couriers, "
                  "are part of your overheads and must not be charged.",
            "letters": "Clerical tasks (including filing, copying, scheduling, organizing files, labeling and binder preparation) are not billable, "
                       "even when performed by attorneys or paralegals.",
        }[style]
        add(c, text, {"kind": "clerical_work"})
    ic = p.get("internal_conference")
    if ic == "not_billable":
        text = {
            "numbered": "Internal conferences between timekeepers of the firm are not billable.",
            "uk": "Internal meetings and discussions between fee earners of your firm will not be paid.",
            "letters": "Intra-firm conferences are not billable to the Company.",
        }[style]
        add(c, text, {"kind": "internal_conference", "policy": "not_billable"})
    elif ic == "one_attendee":
        text = {
            "numbered": "Where timekeepers of the firm confer internally, only the most senior attendee may bill the time; time recorded by other attendees of the same conference is not billable.",
            "uk": "For internal meetings between fee earners, only one attendee (the most senior) may charge for the meeting.",
            "letters": "For intra-firm conferences, the Company will pay only one attendee, the most senior timekeeper present.",
        }[style]
        add(c, text, {"kind": "internal_conference", "policy": "one_attendee"})
    if p.get("research_cap"):
        h = p["research_cap"]
        text = {
            "numbered": f"Legal research exceeding {h} hours per matter in any calendar month requires prior approval; research time above that limit will not be paid.",
            "uk": f"Research of more than {h} hours on a matter in a month must be approved in advance; unapproved research above {h} hours will not be paid.",
            "letters": f"Research time above {h} hours per matter per month is not billable without prior written approval.",
        }[style]
        add(c, text, {"kind": "research_cap", "hours_per_month": h})
    add(c, "Time spent preparing invoices, responding to invoice queries or discussing these guidelines is not billable.")

    # ---- 5 travel
    tf = p.get("travel_factor")
    if tf is not None:
        c = section("Travel", "Travel", "Travel")
        if tf == 0:
            text = {
                "numbered": "Travel time is not billable. Counsel should work while travelling where possible and bill only that work.",
                "uk": "Travel time will not be paid. Work done while travelling may be charged in the normal way.",
                "letters": "Non-working travel time is not billable to the Company.",
            }[style]
        else:
            text = {
                "numbered": f"Non-working travel time may be billed at {_pct(tf)} of the timekeeper's approved rate.",
                "uk": f"Travel time may be charged at {_pct(tf)} of the agreed hourly rate.",
                "letters": f"Non-working travel time will be paid at {_pct(tf)} of the approved hourly rate.",
            }[style]
        add(c, text, {"kind": "travel_time", "rate_factor": tf})
        add(c, "Air travel must be economy class for flights under six hours, and hotel costs must be reasonable for the location.")

    # ---- 6 expenses
    c = section("Expenses and disbursements", "Disbursements", "Expenses")
    nr = p.get("nonreimbursable") or []
    if nr:
        names = [EXPENSE_CODES[x].lower() for x in nr]
        listed = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
        text = {
            "numbered": f"The following are firm overheads and will not be reimbursed: {listed}.",
            "uk": f"We will not reimburse {listed}.",
            "letters": f"The Company will not reimburse charges for {listed}.",
        }[style]
        add(c, text, {"kind": "nonreimbursable_expense", "codes": nr})
    for code, cap in (p.get("expense_caps") or {}).items():
        cur = {"kestrel": "EUR", "brightwater": "GBP"}.get(tenant, "USD")
        text = {
            "numbered": f"Photocopying is reimbursed at no more than {_money(cap, cur)} per page.",
            "uk": f"Copying charges must not exceed {_money(cap, cur)} per page.",
            "letters": f"Copying will be reimbursed at a maximum of {_money(cap, cur)} per page.",
        }[style]
        assert code == "E101"
        add(c, text, {"kind": "expense_unit_cap", "code": code, "max_unit_cost": cap})
    add(c, "Expenses must be billed at actual cost without mark-up, and receipts must be retained and provided on request.")

    # ---- 7 invoicing
    c = section("Invoicing", "Invoices", "Invoice Submission")
    add(c, "Invoices must be submitted electronically in LEDES 1998B format with UTBMS task, activity and expense codes, one invoice per matter per month.")
    days = p["submission_days"]
    text = {
        "numbered": f"Invoices must be submitted within {days} days of the end of the billing period. Late invoices may be rejected.",
        "uk": f"Invoices received more than {days} days after the end of the month in which the work was done will not be paid.",
        "letters": f"Invoices submitted more than {days} days after the end of the billing period will be rejected.",
    }[style]
    add(c, text, {"kind": "submission_deadline", "days": days})
    text = {
        "numbered": "Fees beyond the approved matter budget require written approval from the responsible in-house lawyer before the work is performed.",
        "uk": "Fees in excess of the agreed budget will not be paid unless we approved the increase before the work was done.",
        "letters": "Billing in excess of the approved budget requires prior written approval of the Law Department.",
    }[style]
    add(c, text, {"kind": "budget"})

    # ---- 8 confidentiality (no rules)
    c = section("Confidentiality and data protection", "Confidentiality", "Confidentiality")
    add(c, "Counsel must keep all information received in connection with a matter confidential and process personal data only as "
           "required for the engagement, in line with applicable data protection law.")
    add(c, "Counsel may not use generative AI tools on our documents without our prior written consent, and must disclose any such use on the invoice.")
    return d
