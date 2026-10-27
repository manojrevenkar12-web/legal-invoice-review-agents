"""UTBMS code sets (ABA Litigation Code Set): task, activity and expense codes.

Law firms put these codes on every LEDES line; clients authorise which ones a firm may use.
"""

from __future__ import annotations

TASK_CODES: dict[str, str] = {
    "L110": "Fact Investigation/Development",
    "L120": "Analysis/Strategy",
    "L130": "Experts/Consultants",
    "L140": "Document/File Management",
    "L150": "Budgeting",
    "L160": "Settlement/Non-Binding ADR",
    "L190": "Other Case Assessment, Development and Administration",
    "L210": "Pleading",
    "L220": "Preliminary Injunctions/Provisional Remedies",
    "L230": "Court-Mandated Conferences",
    "L240": "Dispositive Motions",
    "L250": "Other Written Motions and Submissions",
    "L260": "Class Action Certification and Notice",
    "L310": "Written Discovery",
    "L320": "Document Production",
    "L330": "Depositions",
    "L340": "Expert Discovery",
    "L350": "Discovery Motions",
    "L390": "Other Discovery",
    "L410": "Fact Witnesses",
    "L420": "Expert Witnesses",
    "L430": "Written Motions and Submissions",
    "L440": "Other Trial Preparation and Support",
    "L450": "Trial and Hearing Attendance",
    "L460": "Post-Trial Motions and Submissions",
    "L470": "Enforcement",
    "L510": "Appellate Motions and Submissions",
    "L520": "Appellate Briefs",
    "L530": "Oral Argument",
}

PHASES: dict[str, str] = {
    "L100": "Case Assessment, Development and Administration",
    "L200": "Pre-Trial Pleadings and Motions",
    "L300": "Discovery",
    "L400": "Trial Preparation and Trial",
    "L500": "Appeal",
}

ACTIVITY_CODES: dict[str, str] = {
    "A101": "Plan and prepare for",
    "A102": "Research",
    "A103": "Draft/revise",
    "A104": "Review/analyze",
    "A105": "Communicate (in firm)",
    "A106": "Communicate (with client)",
    "A107": "Communicate (other outside counsel)",
    "A108": "Communicate (other external)",
    "A109": "Appear for/attend",
    "A110": "Manage data/files",
    "A111": "Other",
}

EXPENSE_CODES: dict[str, str] = {
    "E101": "Copying",
    "E102": "Outside printing",
    "E103": "Word processing",
    "E104": "Facsimile",
    "E105": "Telephone",
    "E106": "Online research",
    "E107": "Delivery services/messengers",
    "E108": "Postage",
    "E109": "Local travel",
    "E110": "Out-of-town travel",
    "E111": "Meals",
    "E112": "Court fees",
    "E113": "Subpoena fees",
    "E114": "Witness fees",
    "E115": "Deposition transcripts",
    "E116": "Trial transcripts",
    "E117": "Trial exhibits",
    "E118": "Litigation support vendors",
    "E119": "Experts",
    "E120": "Private investigators",
    "E121": "Arbitrators/mediators",
    "E122": "Local counsel",
    "E123": "Other professionals",
    "E124": "Other",
}


def phase_of(task_code: str) -> str:
    """L330 -> L300."""
    return task_code[:2] + "00" if task_code.startswith("L") and len(task_code) == 4 else ""


def is_known(code: str) -> bool:
    return code in TASK_CODES or code in ACTIVITY_CODES or code in EXPENSE_CODES
