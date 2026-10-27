"""Generate the simulated legal-spend world: tenants, guideline documents, firms, matters, rate agreements,
a year of LEDES invoices, past reviewer decisions, and the hidden truth for every line.

Writes the observable world to data/world and the answers to data/truth. Deterministic for a seed.
Simulation only: the review pipeline reads data/world and never imports this package.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from lira import ledes
from lira.domain import Firm, Invoice, LineItem, LineType, Matter, RateAgreement, Tenant, Timekeeper, money
from lira.sim import narratives as N
from lira.sim import truth as T
from lira.sim.appeals import training_set as appeal_training_set
from lira.sim.guidelines_gen import build as build_guidelines
from lira.utbms import PHASES, TASK_CODES

FIRST_NAMES = [
    "Mira",
    "Daniel",
    "Sophie",
    "Kenji",
    "Amara",
    "Lucas",
    "Hannah",
    "Rafael",
    "Chloe",
    "Tobias",
    "Leila",
    "Martin",
    "Zara",
    "Viktor",
    "Elena",
    "Noah",
    "Ines",
    "Oliver",
    "Aisha",
    "Jakob",
    "Mei",
    "Pedro",
    "Freya",
    "Ibrahim",
    "Clara",
    "Stefan",
    "Nkechi",
    "Arjun",
    "Laura",
    "Mateo",
    "Johanna",
    "Kofi",
    "Anna",
    "Ravi",
    "Emma",
    "Diego",
]
LAST_NAMES = [
    "Okafor",
    "Lindqvist",
    "Harlow",
    "Marchetti",
    "Ashdown",
    "Becker",
    "Nakamura",
    "Ferreira",
    "Novak",
    "Haddad",
    "Schreiber",
    "Osei",
    "Kowalski",
    "Moreau",
    "Iyer",
    "Castillo",
    "Weber",
    "Achterberg",
    "Sato",
    "Mensah",
    "Olsen",
    "Rinaldi",
    "Kaplan",
    "Duarte",
    "Brandt",
    "Szabo",
    "Quinn",
    "Varga",
    "Hoffmann",
    "Adeyemi",
]

ROSTER_SHAPE = [  # (title, LEDES class, seniority, base rate, count)
    ("Partner", "PT", 6, 780, 2),
    ("Of Counsel", "OC", 5, 640, 1),
    ("Senior Associate", "AS", 4, 520, 2),
    ("Associate", "AS", 3, 430, 2),
    ("First-Year Associate", "AS", 2, 330, 1),
    ("Paralegal", "LA", 1, 210, 2),
]
EXTRA_ROLES = {  # firm -> extra timekeepers whose time some clients will not pay
    "harlow": [("Summer Associate", "OT", 0, 240), ("Trainee Solicitor", "OT", 0, 220)],
    "ashdown": [("Summer Associate", "OT", 0, 230)],
    "marchetti": [("Law Clerk", "OT", 0, 190)],
    "okafor": [("Law Clerk", "OT", 0, 200)],
    "lindqvist": [],
}
CURRENCY_FACTOR = {"EUR": 1.0, "GBP": 0.85, "USD": 1.1}


def _month(first: str, i: int) -> tuple[date, date]:
    y, m = map(int, first.split("-"))
    m0 = (y * 12 + (m - 1)) + i
    start = date(m0 // 12, m0 % 12 + 1, 1)
    nxt = date((m0 + 1) // 12, (m0 + 1) % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def _business_days(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass
class Ctx:
    cfg: dict[str, Any]
    rng: np.random.Generator
    tenants: dict[str, dict[str, Any]] = field(default_factory=dict)
    firms: dict[str, Firm] = field(default_factory=dict)
    firm_cfg: dict[str, dict[str, Any]] = field(default_factory=dict)
    rates: list[RateAgreement] = field(default_factory=list)
    matters: list[Matter] = field(default_factory=list)


class World:
    def __init__(self, config_path: Path, out: Path):
        self.cfg = yaml.safe_load(config_path.read_text())
        self.out = out
        self.rng = np.random.default_rng(self.cfg["seed"])

    # ---------------------------------------------------------------- reference data
    def _firms(self) -> dict[str, Firm]:
        used: set[str] = set()
        firms: dict[str, Firm] = {}
        for fid, fc in self.cfg["firms"].items():
            tks: list[Timekeeper] = []
            n = 0
            shape = [(t, c, s, r, k) for (t, c, s, r, k) in ROSTER_SHAPE] + [(t, c, s, r, 1) for (t, c, s, r) in EXTRA_ROLES[fid]]
            for title, cls, sen, _rate, count in shape:
                for _ in range(count):
                    while True:
                        name = f"{self.rng.choice(FIRST_NAMES)} {self.rng.choice(LAST_NAMES)}"
                        if name not in used:
                            used.add(name)
                            break
                    n += 1
                    tks.append(
                        Timekeeper(id=f"{fid[:3].upper()}{n:03d}", firm_id=fid, name=name, classification=cls, title=title, seniority=sen)  # type: ignore[arg-type]
                    )
            firms[fid] = Firm(id=fid, name=fc["name"], timekeepers=tuple(tks))
        return firms

    def _base_rate(self, tk: Timekeeper, currency: str) -> Decimal:
        base = {t: r for (t, _c, _s, r, _k) in ROSTER_SHAPE} | {t: r for f in EXTRA_ROLES.values() for (t, _c, _s, r) in f}
        jitter = 1 + (_stable(tk.id) % 7 - 3) / 100
        return Decimal(str(round(base[tk.title] * CURRENCY_FACTOR[currency] * jitter / 5) * 5))

    # ---------------------------------------------------------------- build
    def build(self) -> dict[str, Any]:
        cfg = self.cfg
        world, truth = self.out / "world", self.out / "truth"
        for p in (world, truth):
            if p.exists():
                shutil.rmtree(p)  # a rebuild never mixes files from two generations
            p.mkdir(parents=True)
        firms = self._firms()
        tenants: list[Tenant] = []
        rates: list[RateAgreement] = []
        matters: list[Matter] = []
        guideline_truth: dict[str, list[dict[str, Any]]] = {}
        params_by_tenant: dict[str, list[tuple[date, dict[str, Any]]]] = {}

        for tc in cfg["tenants"]:
            t = Tenant(id=tc["id"], name=tc["name"], client_id=tc["client_id"], currency=tc["currency"])
            tenants.append(t)
            versions = []
            for v in tc["versions"]:
                eff = v["effective_from"] if isinstance(v["effective_from"], date) else date.fromisoformat(v["effective_from"])
                doc = build_guidelines(t.id, t.name, t.currency, tc["style"], v["version"], eff, v["params"])
                gdir = world / "guidelines" / t.id
                gdir.mkdir(parents=True, exist_ok=True)
                (gdir / f"v{v['version']}.md").write_text(doc.render(f"{t.name} - Outside Counsel Billing Guidelines"))
                (gdir / f"v{v['version']}.json").write_text(
                    json.dumps({"tenant": t.id, "version": v["version"], "effective_from": eff.isoformat()}, indent=2)
                )
                guideline_truth[f"{t.id}@v{v['version']}"] = [_jsonable(a) for a in doc.answer]
                versions.append((eff, v["params"]))
            params_by_tenant[t.id] = sorted(versions, key=lambda x: x[0])

            # rate agreements: approved at the start; a 5% increase from 2026-01 approved for every firm but Marchetti
            for fid in tc["firms"]:
                for tk in firms[fid].timekeepers:
                    r0 = self._base_rate(tk, t.currency)
                    if fid == "marchetti":
                        rates.append(RateAgreement(tenant=t.id, firm_id=fid, timekeeper_id=tk.id, rate=r0, effective_from=date(2025, 1, 1)))
                    else:
                        rates.append(
                            RateAgreement(
                                tenant=t.id,
                                firm_id=fid,
                                timekeeper_id=tk.id,
                                rate=r0,
                                effective_from=date(2025, 1, 1),
                                effective_to=date(2025, 12, 31),
                            )
                        )
                        r1 = Decimal(str(round(float(r0) * 1.05 / 5) * 5))
                        rates.append(RateAgreement(tenant=t.id, firm_id=fid, timekeeper_id=tk.id, rate=r1, effective_from=date(2026, 1, 1)))

            # matters
            for i in range(tc["matters"]):
                fid = tc["firms"][i % len(tc["firms"])]
                firm = firms[fid]
                core = [
                    tk
                    for tk in firm.timekeepers
                    if tk.title in ("Partner", "Senior Associate", "Associate", "Paralegal", "Of Counsel", "First-Year Associate")
                ]
                pick = list(self.rng.choice(len(core), size=min(len(core), int(self.rng.integers(5, 8))), replace=False))
                staff = [core[j] for j in sorted(pick)]
                if not any(tk.classification == "PT" for tk in staff):
                    staff.insert(0, next(tk for tk in core if tk.classification == "PT"))
                if not any(tk.classification == "LA" for tk in staff):
                    staff.append(next(tk for tk in core if tk.classification == "LA"))
                staff += [tk for tk in firm.timekeepers if tk.seniority == 0]  # firms put juniors on matters too
                budget = Decimal(int(self.rng.integers(18, 60)) * 10000)
                matters.append(
                    Matter(
                        id=f"{t.id[:3].upper()}-{2025 + i % 2}-{i + 1:03d}",
                        tenant=t.id,
                        firm_id=fid,
                        law_firm_matter_id=f"{fid[:3].upper()}{40000 + 17 * i + len(t.id)}",
                        name=f"{N.SLOTS['party'][i % len(N.SLOTS['party'])].replace('the ', '').title()} - {N.SLOTS['topic'][(i * 3) % len(N.SLOTS['topic'])]}",
                        budget=budget,
                        opened=date(2025, 1 + i % 9, 1 + (i * 5) % 27),
                        authorized_timekeepers=tuple(tk.id for tk in staff),
                        authorized_task_codes=tuple(TASK_CODES),
                        authorized_expense_codes=tuple(f"E{n}" for n in range(101, 125)),
                    )
                )

        (world / "tenants.json").write_text(json.dumps([_jsonable(t.model_dump()) for t in tenants], indent=2))
        (world / "firms.json").write_text(json.dumps([_jsonable(f.model_dump()) for f in firms.values()], indent=2))
        (world / "rate_agreements.json").write_text(json.dumps([_jsonable(r.model_dump()) for r in rates], indent=1))
        (world / "matters.json").write_text(json.dumps([_jsonable(m.model_dump()) for m in matters], indent=1))
        (truth / "guidelines.json").write_text(json.dumps(guideline_truth, indent=2))

        gen = _InvoiceGenerator(self, firms, {t.id: t for t in tenants}, rates, params_by_tenant)
        line_truth: list[dict[str, Any]] = []
        invoice_truth: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        spent: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        n_invoices = 0
        for mi in range(cfg["months"]):
            start, end = _month(cfg["first_month"], mi)
            for m in matters:
                if m.opened > end or self.rng.random() < 0.08:
                    continue
                inv, lt, it = gen.invoice(m, mi, start, end, spent)
                n_invoices += 1
                folder = world / "invoices" / m.tenant / f"{start:%Y-%m}"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{inv.number}.ledes").write_text(ledes.write(inv))
                line_truth += lt
                invoice_truth.append(it)
                if mi < cfg["history_months"]:
                    history += gen.reviewer_decisions(inv, lt)

        (world / "history").mkdir(exist_ok=True)
        with (world / "history" / "decisions.jsonl").open("w") as f:
            for h in history:
                f.write(json.dumps(h) + "\n")
        with (world / "history" / "appeals.jsonl").open("w") as f:  # past appeals, intent labelled by reviewers
            for a in appeal_training_set(seed=self.cfg["seed"] % 1000):
                f.write(json.dumps(a) + "\n")
        with (truth / "lines.jsonl").open("w") as f:
            for r in line_truth:
                f.write(json.dumps(_jsonable(r)) + "\n")
        with (truth / "invoices.jsonl").open("w") as f:
            for r in invoice_truth:
                f.write(json.dumps(_jsonable(r)) + "\n")
        drift = {fid: fc["drift_from"] for fid, fc in cfg["firms"].items() if fc.get("drift_from")}
        (truth / "drift.json").write_text(json.dumps(drift, indent=2))
        (world / "world.json").write_text(
            json.dumps(
                {
                    "first_month": cfg["first_month"],
                    "months": cfg["months"],
                    "history_months": cfg["history_months"],
                },
                indent=2,
            )
        )
        summary = {
            "invoices": n_invoices,
            "lines": len(line_truth),
            "history_decisions": len(history),
            "violating_lines": sum(1 for r in line_truth if r["kinds"]),
        }
        (truth / "summary.json").write_text(json.dumps(summary, indent=2))
        return summary


class _InvoiceGenerator:
    def __init__(
        self,
        w: World,
        firms: dict[str, Firm],
        tenants: dict[str, Tenant],
        rates: list[RateAgreement],
        params: dict[str, list[tuple[date, dict[str, Any]]]],
    ):
        self.w, self.cfg, self.rng = w, w.cfg, w.rng
        self.firms, self.tenants, self.rates, self.params = firms, tenants, rates, params
        self.ev = self.cfg["events"]
        self.counter = 0
        # timekeepers that bill an unapproved 2026 rate (Marchetti's rate creep)
        self.creep = {tk.id for tk in firms["marchetti"].timekeepers if self.rng.random() < self.cfg["firms"]["marchetti"]["rate_creep"]}

    # ---------------------------------------------------------------- helpers
    def params_at(self, tenant: str, d: date) -> dict[str, Any]:
        cur = self.params[tenant][0][1]
        for eff, p in self.params[tenant]:
            if eff <= d:
                cur = p
        return cur

    def approved_rate(self, tenant: str, tk: str, d: date) -> Decimal | None:
        for r in self.rates:
            if r.tenant == tenant and r.timekeeper_id == tk and r.covers(d):
                return r.rate
        return None

    def billed_rate(self, tenant: str, firm: str, tk: Timekeeper, d: date) -> Decimal:
        ok = self.approved_rate(tenant, tk.id, d)
        if ok is None:  # unauthorised or unknown: the firm uses its own list rate
            ok = self.w._base_rate(tk, self.tenants[tenant].currency)
        if firm == "marchetti" and tk.id in self.creep and d >= date(2026, 1, 1):
            return Decimal(str(round(float(ok) * 1.08 / 5) * 5))
        return ok

    def fill(self, template: str, m: Matter, colleague: str = "") -> str:
        r = self.rng

        def pick(xs: list[str]) -> str:
            return str(xs[int(r.integers(len(xs)))])

        out = template
        for slot, values in N.SLOTS.items():
            out = out.replace("{" + slot + "}", pick(values))
        out = out.replace("{witness}", pick(N.WITNESSES)).replace("{expert}", pick(N.EXPERTS))
        out = out.replace("{opposing}", pick(N.OPPOSING)).replace("{client_contact}", pick(N.CLIENT_CONTACTS[m.tenant]))
        out = out.replace("{colleague}", colleague).replace("{n}", str(int(r.integers(1, 6))))
        return out

    def style(self, text: str, firm: str, d: date, task: str = "", activity: str = "") -> str:
        fc = self.cfg["firms"][firm]
        st = fc["style"]
        drift = fc.get("drift_from") and f"{d:%Y-%m}" >= fc["drift_from"]
        r = self.rng
        if drift:
            t = text
            for a, b in N.ABBREVIATIONS:
                t = re.sub(rf"\b{re.escape(a)}\b", b, t)
            t = re.sub(r"\b(the|a|an|of|for|to)\b ", "", t)
            return f"{task}/{activity} {t.upper()}".strip()
        if st == "terse":
            t = re.sub(r"\b(the|a|an)\b ", "", text)
            return t[0].lower() + t[1:] if r.random() < 0.5 else t
        if st == "abbreviated":
            t = text
            for a, b in N.ABBREVIATIONS:
                if r.random() < 0.6:
                    t = re.sub(rf"\b{re.escape(a)}\b", b, t)
            return t
        return text + ("." if st == "verbose" and not text.endswith(".") else "")

    def name_form(self, tk: Timekeeper, firm: str) -> str:
        first, last = tk.name.split(" ", 1)
        st = self.cfg["firms"][firm]["style"]
        forms = {
            "verbose": [tk.name, f"{first[0]}. {last}"],
            "terse": [last, f"{first[0]}{last[0]}"],
            "semicolons": [f"{first[0]}. {last}", tk.name],
            "abbreviated": [f"{first[0]}. {last}", last],
        }[st]
        return forms[int(self.rng.integers(len(forms)))]

    def hours(self, median: float) -> Decimal:
        h = float(self.rng.lognormal(math.log(median), 0.4))
        return Decimal(str(min(round(median * 2.2, 1), max(0.1, round(h, 1)))))

    # ---------------------------------------------------------------- one invoice
    def invoice(
        self, m: Matter, mi: int, start: date, end: date, spent: dict[str, Decimal]
    ) -> tuple[Invoice, list[dict[str, Any]], dict[str, Any]]:
        r, cfg = self.rng, self.cfg
        firm = self.firms[m.firm_id]
        fc = cfg["firms"][m.firm_id]
        tenant = self.tenants[m.tenant]
        staff = [firm.timekeeper(t) for t in m.authorized_timekeepers]
        staff_tk = [t for t in staff if t is not None]
        lawyers = [t for t in staff_tk if t.classification in ("PT", "AS", "OC")]
        paralegals = [t for t in staff_tk if t.classification == "LA"]
        juniors = [
            t
            for t in staff_tk
            if t.seniority == 0 and (("Summer" in t.title and start.month in fc.get("summer_months", [])) or "Summer" not in t.title)
        ]
        days = _business_days(start, end)
        phase_idx = min(4, max(0, (mi + (_stable(m.id) % 3)) // 3))
        phases = list(PHASES)[: phase_idx + 1]
        atoms = [a for a in N.PROFESSIONAL if a.task[:2] + "00" in phases[-2:] or (a.task[:2] + "00" in phases and r.random() < 0.15)]
        if not atoms:
            atoms = N.PROFESSIONAL
        entries: list[dict[str, Any]] = []  # each becomes a line; truth facts collected alongside

        def add_fee(tk: Timekeeper, d: date, text: str, task: str, activity: str, units: Decimal, facts: dict[str, Any]) -> dict[str, Any]:
            e = {"tk": tk, "date": d, "text": text, "task": task, "activity": activity, "units": units, "type": "F", "facts": facts}
            entries.append(e)
            return e

        n_lines = int(np.clip(r.lognormal(math.log(20), 0.4), 6, 48))
        for _ in range(n_lines):
            d = days[int(r.integers(len(days)))]
            u = r.random()
            if u < 0.1:
                self._expense(entries, m, fc, d)
                continue
            if juniors and r.random() < 0.025:
                tk = juniors[int(r.integers(len(juniors)))]
            elif paralegals and r.random() < 0.22:
                tk = paralegals[int(r.integers(len(paralegals)))]
            else:
                tk = lawyers[int(r.integers(len(lawyers)))]
            is_para = tk.classification == "LA"
            roll = r.random()
            p_vague = fc["vague"] * (0.3 if is_para else 1)
            p_clerical = fc["clerical"] * (2.5 if is_para else 0.6)
            if roll < p_vague:
                text = N.VAGUE[int(r.integers(len(N.VAGUE)))]
                a = atoms[int(r.integers(len(atoms)))]
                activity = "A102" if "research" in text.lower() else str(r.choice(["A104", "A103", "A111", "A101"]))
                add_fee(tk, d, text, a.task, activity, self.hours(1.1), {"vague": True})
            elif roll < p_vague + p_clerical:
                a = N.CLERICAL[int(r.integers(len(N.CLERICAL)))]
                add_fee(tk, d, self.fill(a.text, m), a.task, a.activity, self.hours(a.hours), {"clerical": True})
            elif roll < p_vague + p_clerical + fc["block"] and not is_para:
                k = int(r.integers(2, 5))
                parts = [atoms[int(r.integers(len(atoms)))] for _ in range(k)]
                parts = [p for p in parts if "deposition" not in p.tags and "hearing" not in p.tags] or [
                    N.PROFESSIONAL[0],
                    N.PROFESSIONAL[3],
                ]
                if len(parts) < 2:
                    parts.append(N.PROFESSIONAL[7])
                texts = [self.fill(p.text, m) for p in parts]
                sep = {"verbose": "; ", "terse": " / ", "semicolons": "; ", "abbreviated": "; "}[fc["style"]]
                if fc["style"] == "verbose" and r.random() < 0.5:
                    text = ", ".join(texts[:-1]) + " and " + texts[-1][0].lower() + texts[-1][1:]
                else:
                    text = sep.join(texts)
                units = sum((self.hours(p.hours) for p in parts), Decimal("0"))
                add_fee(tk, d, text, parts[0].task, parts[0].activity, units, {"block": True, "tasks": len(parts)})
            elif r.random() < self.ev["travel"]:
                city = str(r.choice(N.SLOTS["city"]))
                text = self.fill(N.TRAVEL[int(r.integers(len(N.TRAVEL)))], m).replace("{city}", city)
                halved = m.firm_id in ("harlow", "lindqvist") and r.random() < 0.7
                if halved:
                    text += " (billed at 50%)"
                e = add_fee(
                    tk,
                    d,
                    text,
                    str(r.choice(["L330", "L450", "L160", "L110"])),
                    "A111",
                    self.hours(3.2),
                    {"travel": True, "halved": halved},
                )
                e["rate_factor"] = Decimal("0.5") if halved else Decimal("1")
            else:
                pool = [
                    a for a in atoms if (a.roles != "paralegal" or is_para) and (a.roles == "any" or not is_para or a.roles == "paralegal")
                ]
                if is_para:
                    pool = [a for a in N.PROFESSIONAL if a.roles in ("any", "paralegal")] or pool
                a = pool[int(r.integers(len(pool)))]
                if ("deposition" in a.tags or "hearing" in a.tags) and not is_para:
                    self._event(entries, m, a, d, tk, lawyers, add_fee)
                    continue
                add_fee(tk, d, self.fill(a.text, m), a.task, a.activity, self.hours(a.hours), {"research": "research" in a.tags})

        # internal conferences: both lawyers bill the same meeting
        for _ in range(int(r.poisson(fc["internal_meetings"]))):
            if len(lawyers) < 2:
                break
            i, j = r.choice(len(lawyers), size=2, replace=False)
            a_tk, b_tk = lawyers[int(i)], lawyers[int(j)]
            d = days[int(r.integers(len(days)))]
            tmpl = N.INTERNAL_CONFERENCE[int(r.integers(len(N.INTERNAL_CONFERENCE)))]
            units = Decimal(str(round(float(r.uniform(0.3, 1.2)), 1)))
            task = str(r.choice(["L120", "L310", "L240", "L330"]))
            meeting = f"M{self.counter}-{int(r.integers(1_000_000))}"
            for me, other in ((a_tk, b_tk), (b_tk, a_tk)):
                text = self.fill(tmpl, m, colleague=self.name_form(other, m.firm_id))
                add_fee(me, d, text, task, "A105", units, {"internal_conference": meeting, "with": other.id})

        # long days for trial and deposition work
        by_day: dict[tuple[str, date], Decimal] = defaultdict(lambda: Decimal("0"))
        for e in entries:
            if e["type"] == "F":
                by_day[(e["tk"].id, e["date"])] += e["units"]
        for e in list(entries):
            if e["type"] == "F" and e["facts"].get("event") and r.random() < self.ev["long_day"] * 6:
                extra = Decimal(str(round(float(r.uniform(2.5, 5.0)), 1)))
                a = N.PROFESSIONAL[int(r.integers(len(N.PROFESSIONAL)))]
                add_fee(e["tk"], e["date"], self.fill(a.text, m), a.task, a.activity, extra, {"long_day": True})

        # firm habits and rare events, applied per fee line
        for e in entries:
            if e["type"] != "F":
                continue
            f = e["facts"]
            if r.random() < fc["increment_slip"]:
                q = Decimal(str(max(0.25, round(float(e["units"]) * 4) / 4)))
                if q % Decimal("0.1") != 0:
                    e["units"] = q
                    f["increment_slip"] = True
            if r.random() < self.ev["inflated_hours"] and not f.get("event") and not f.get("block"):
                e["units"] = Decimal(str(round(float(e["units"]) * float(r.uniform(3.5, 5.0)) + 2, 1)))
                f["inflated"] = True
            if r.random() < self.ev["task_code_slip"] and e["task"] in TASK_CODES:
                phase = e["task"][:2]
                siblings = [c for c in TASK_CODES if c[:2] == phase and c != e["task"]] or [c for c in TASK_CODES if c != e["task"]]
                f["true_task"] = e["task"]
                e["task"] = str(r.choice(siblings))
            if r.random() < self.ev["injection"] and not f.get("vague"):
                tmpl = N.INJECTIONS[int(r.integers(len(N.INJECTIONS)))]
                e["text"] = tmpl.replace("{base}", e["text"].rstrip("."))
                f["injection"] = True
            if r.random() < self.ev["unauthorized_timekeeper"]:
                outsiders = [t for t in firm.timekeepers if t.id not in m.authorized_timekeepers]
                if outsiders:
                    e["tk"] = outsiders[int(r.integers(len(outsiders)))]
                    f["unauthorized"] = True

        # dates sorted like a timekeeping export
        entries.sort(key=lambda e: (e["date"], e["tk"].id if e.get("tk") else "~"))
        lines: list[LineItem] = []
        facts_by_line: dict[str, dict[str, Any]] = {}
        for n, e in enumerate(entries, start=1):
            ln = str(n)
            if e["type"] == "E":
                total = money(e["unit_cost"] * e["units"])
                lines.append(
                    LineItem(
                        line_no=ln,
                        type=LineType.EXPENSE,
                        date=e["date"],
                        units=e["units"],
                        unit_cost=e["unit_cost"],
                        total=total,
                        expense_code=e["code"],
                        description=e["text"],
                    )
                )
            else:
                tk = e["tk"]
                rate = self.billed_rate(m.tenant, m.firm_id, tk, e["date"]) * e.get("rate_factor", Decimal("1"))
                rate = money(rate)
                total = money(rate * e["units"])
                text = self.style(e["text"], m.firm_id, e["date"], e["task"], e["activity"])
                lines.append(
                    LineItem(
                        line_no=ln,
                        type=LineType.FEE,
                        date=e["date"],
                        units=e["units"],
                        unit_cost=rate,
                        total=total,
                        task_code=e["task"],
                        activity_code=e["activity"],
                        timekeeper_id=tk.id,
                        timekeeper_name=tk.name,
                        timekeeper_class=tk.classification,
                        description=text,
                    )
                )
            facts_by_line[ln] = e
        self.counter += 1
        total = money(sum((li.total for li in lines), Decimal("0")))
        delay = int(r.integers(70, 130)) if r.random() < self.ev["late_invoice"] else int(r.integers(5, 28))
        number = f"{m.firm_id[:3].upper()}-{start:%y%m}-{self.counter:04d}"
        inv = Invoice(
            number=number,
            date=end + timedelta(days=delay),
            client_id=tenant.client_id,
            law_firm_id=m.firm_id,
            law_firm_matter_id=m.law_firm_matter_id,
            client_matter_id=m.id,
            total=total,
            period_start=start,
            period_end=end,
            description=f"Professional services rendered {start:%B %Y}",
            lines=tuple(lines),
        )
        line_truth, inv_truth = T.score_invoice(
            inv,
            m,
            self.firms[m.firm_id],
            facts_by_line,
            self.params_at,
            self.approved_rate,
            spent,
            month_index=mi,
            history_months=self.cfg["history_months"],
        )
        return inv, line_truth, inv_truth

    def _expense(self, entries: list[dict[str, Any]], m: Matter, fc: dict[str, Any], d: date) -> None:
        r = self.rng
        roll = r.random()
        if roll < 0.35:
            pages = Decimal(int(r.integers(40, 900)))
            cost = Decimal(str(fc["copy_cost"]))
            entries.append(
                {
                    "type": "E",
                    "date": d,
                    "code": "E101",
                    "units": pages,
                    "unit_cost": cost,
                    "text": f"Photocopies ({pages} pages)",
                    "facts": {"expense": "E101"},
                }
            )
        elif roll < 0.35 + fc["meals"] * 8:
            entries.append(
                {
                    "type": "E",
                    "date": d,
                    "code": "E111",
                    "units": Decimal("1"),
                    "unit_cost": money(r.uniform(18, 140)),
                    "text": str(
                        r.choice(["Working lunch during document review", "Dinner while travelling for deposition", "Meals - trial team"])
                    ),
                    "facts": {"expense": "E111"},
                }
            )
        elif roll < 0.5 and r.random() < fc["word_processing"] * 15:
            entries.append(
                {
                    "type": "E",
                    "date": d,
                    "code": "E103",
                    "units": Decimal("1"),
                    "unit_cost": money(r.uniform(40, 200)),
                    "text": "Word processing charges",
                    "facts": {"expense": "E103"},
                }
            )
        else:
            code, text, lo, hi = [
                ("E110", "Airfare and hotel for deposition travel", 180, 950),
                ("E115", "Deposition transcript and video", 300, 1400),
                ("E112", "Court filing fee", 50, 450),
                ("E107", "Courier delivery to opposing counsel", 15, 90),
                ("E106", "Online legal research charges", 40, 380),
                ("E105", "Conference call service charges", 10, 80),
                ("E104", "Facsimile charges", 5, 30),
                ("E118", "E-discovery hosting, monthly", 400, 2500),
            ][int(r.integers(8))]
            entries.append(
                {
                    "type": "E",
                    "date": d,
                    "code": code,
                    "units": Decimal("1"),
                    "unit_cost": money(r.uniform(lo, hi)),
                    "text": text,
                    "facts": {"expense": code},
                }
            )

    def _event(
        self, entries: list[dict[str, Any]], m: Matter, a: N.Atom, d: date, tk: Timekeeper, lawyers: list[Timekeeper], add_fee: Any
    ) -> None:
        """A deposition or hearing: usually one or two lawyers attend, sometimes one more than usual."""
        r = self.rng
        n = 1 + int(r.random() < 0.45)
        if r.random() < self.ev["extra_attendee"]:
            n += 1
        others = [x for x in lawyers if x.id != tk.id]
        attendees = [tk] + [others[int(i)] for i in r.choice(len(others), size=min(len(others), n - 1), replace=False)] if others else [tk]
        text = self.fill(a.text, m)
        hours = self.hours(a.hours)
        event = f"E{self.counter}-{int(r.integers(1_000_000))}"
        kind = "deposition" if "deposition" in a.tags else "hearing"
        for at in attendees:
            add_fee(at, d, text, a.task, a.activity, hours, {"event": event, "event_kind": kind})

    # ---------------------------------------------------------------- past reviewer decisions (noisy labels)
    def reviewer_decisions(self, inv: Invoice, line_truth: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rv = self.cfg["reviewers"]
        r = self.rng
        out: list[dict[str, Any]] = []
        judge = ["block_billing", "vague_description", "clerical_work"]
        for lt in line_truth:
            kinds = {k["kind"] for k in lt["kinds"]}
            flags: list[str] = []
            for k in kinds & set(judge):
                if r.random() < rv["recall"]:
                    flags.append(str(r.choice([x for x in judge if x != k])) if r.random() < rv["reason_confusion"] else k)
            if not flags and r.random() < rv["false_flag"]:
                flags.append(str(r.choice(judge)))
            li = inv.line(lt["line_no"])
            out.append(
                {
                    "invoice": inv.number,
                    "tenant": lt["tenant"],
                    "line_no": lt["line_no"],
                    "date": li.date.isoformat(),
                    "description": li.description,
                    "task_code": li.task_code,
                    "activity_code": li.activity_code,
                    "timekeeper_class": li.timekeeper_class,
                    "type": li.type.value,
                    "units": str(li.units),
                    "flags": sorted(set(flags)),
                }
            )
        return out


def _stable(s: str) -> int:
    """A hash that is the same in every process (Python's hash() of a str is salted per run)."""
    return zlib.crc32(s.encode())


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_jsonable(v) for v in x]
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, date):
        return x.isoformat()
    return x


def main(config: str = "config/world.yaml", out: str = "data") -> dict[str, Any]:
    return World(Path(config), Path(out)).build()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
