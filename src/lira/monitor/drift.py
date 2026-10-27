"""Data drift in what law firms write, per firm and month.

A firm that changes its timekeeping software, staff or habits changes the text the classifiers read, and their
scores quietly lose meaning. Each month, every firm's narratives are compared with that firm's reference window:
PSI on numeric features (length, capitals, task segments, classifier scores and confidence), PSI on categorical
mixes (dominant topic, activity codes) and the share of words the models never saw. Alerts go to CloudWatch (EMF)
and the console; the runbook says what to do (docs/RUNBOOK.md: relabel a sample, retrain, or tune normalisation).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from lira import config
from lira.nlp.models import NlpModels
from lira.nlp.text import normalize, task_segments
from lira.obs import metrics


@dataclass
class Window:
    firm: str
    month: str
    rows: list[dict[str, Any]] = field(default_factory=list)


def features(rows: list[dict[str, Any]], models: NlpModels) -> dict[str, np.ndarray | list[Any]]:
    descs = [r["description"] for r in rows]
    if not rows:
        return {}
    probs = models.judgement_probs(rows)
    _codes, conf = models.task_codes(descs)
    topics = models.topic_mix(descs)
    vocab = models.task_vec.vocabulary_
    out: dict[str, Any] = {
        "words": np.array([len(normalize(d).split()) for d in descs], dtype=float),
        "upper_share": np.array([sum(c.isupper() for c in d) / max(1, sum(c.isalpha() for c in d)) for d in descs]),
        "task_segments": np.array([len(task_segments(d)) for d in descs], dtype=float),
        "task_code_confidence": np.array(conf),
        "topic": [int(np.argmax(t)) if t.sum() > 0 else -1 for t in topics],
        "activity_code": [r.get("activity_code", "") for r in rows],
        "oov": [_oov(d, vocab) for d in descs],
    }
    for q, p in probs.items():
        out[f"p_{q}"] = p
    return out


def _oov(text: str, vocab: dict[str, int]) -> float:
    toks = re.findall(r"[a-z]{3,}", normalize(text))
    return sum(t not in vocab for t in toks) / max(1, len(toks))


def psi_numeric(ref: np.ndarray, cur: np.ndarray, bins: int) -> float:
    if len(ref) == 0 or len(cur) == 0:
        return 0.0
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:
        edges = np.array([ref.min() - 1e-9, ref.max() + 1e-9])
    edges[0], edges[-1] = -np.inf, np.inf
    r = np.histogram(ref, edges)[0] / len(ref)
    c = np.histogram(cur, edges)[0] / len(cur)
    return _psi(r, c)


def psi_categorical(ref: list[Any], cur: list[Any]) -> float:
    cats = sorted(set(ref) | set(cur), key=str)
    rc, cc = Counter(ref), Counter(cur)
    r = np.array([rc[k] / max(1, len(ref)) for k in cats])
    c = np.array([cc[k] / max(1, len(cur)) for k in cats])
    return _psi(r, c)


def _psi(r: np.ndarray, c: np.ndarray, eps: float = 1e-4) -> float:
    r = np.clip(r, eps, None)
    c = np.clip(c, eps, None)
    return float(np.sum((c - r) * np.log(c / r)))


def psi_fixed(ref: np.ndarray, cur: np.ndarray, edges: list[float]) -> float:
    e = np.array(edges, dtype=float)
    e[0], e[-1] = -np.inf, np.inf
    r = np.histogram(ref, e)[0] / max(1, len(ref))
    c = np.histogram(cur, e)[0] / max(1, len(cur))
    return _psi(r, c)


CATEGORICAL = {"topic", "activity_code"}


def _p_value(ref: Any, cur: Any, categorical: bool) -> float:
    """Is the shift more than sampling noise? Two-sample KS for numbers, chi-square for categories."""
    from scipy import stats

    if categorical:
        cats = sorted(set(ref) | set(cur), key=str)
        table = np.array([[list(ref).count(k) for k in cats], [list(cur).count(k) for k in cats]]) + 0.5
        return float(stats.chi2_contingency(table)[1])
    if len(ref) < 2 or len(cur) < 2:
        return 1.0
    return float(stats.ks_2samp(np.asarray(ref, dtype=float), np.asarray(cur, dtype=float)).pvalue)


def monitor(rows: list[dict[str, Any]], models: NlpModels, reference: tuple[str, str], emit: bool = True) -> dict[str, Any]:
    """rows: fee lines with firm, month ("YYYY-MM"), description, activity_code, timekeeper_class, units.
    reference: (first, last) month of the firm's reference window; later months are monitored."""
    cfg = config.load("drift")
    first, last = reference
    by_firm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_firm[r["firm"]].append(r)
    names = list(cfg["alert_features"]) + list(cfg["report_features"])
    report: list[dict[str, Any]] = []
    for firm, fr in sorted(by_firm.items()):
        ref_rows = [r for r in fr if first <= r["month"] <= last]
        ref = features(ref_rows, models)
        if not ref:
            continue
        ref_oov = float(np.mean(ref["oov"]))
        for month in sorted({r["month"] for r in fr if r["month"] > last}):
            cur_rows = [r for r in fr if r["month"] == month]
            cur = features(cur_rows, models)
            scores: dict[str, float] = {}
            for f in names:
                if f in CATEGORICAL:
                    v = psi_categorical(list(ref[f]), list(cur[f]))
                elif f in cfg.get("fixed_bins", {}):
                    v = psi_fixed(np.asarray(ref[f]), np.asarray(cur[f]), cfg["fixed_bins"][f])
                else:
                    v = psi_numeric(np.asarray(ref[f]), np.asarray(cur[f]), int(cfg["psi_bins"]))
                scores[f] = round(v, 4)
            oov = float(np.mean(cur["oov"]))
            enough = len(cur_rows) >= int(cfg["min_lines"])
            pvals = {f: _p_value(ref[f], cur[f], f in CATEGORICAL) for f in cfg["alert_features"]}
            drivers = sorted(
                (f for f in cfg["alert_features"] if scores[f] >= float(cfg["alert"]["psi"]) and pvals[f] < float(cfg["alert"]["p_value"])),
                key=lambda f: -scores[f],
            )
            oov_jump = oov - ref_oov >= float(cfg["alert"]["oov_rate_increase"]) and pvals.get("oov", 1.0) < float(cfg["alert"]["p_value"])
            alert = enough and (bool(drivers) or oov_jump)
            item = {
                "firm": firm,
                "month": month,
                "lines": len(cur_rows),
                "psi": scores,
                "max_alerting_psi": max(scores[f] for f in cfg["alert_features"]),
                "oov_rate": round(oov, 4),
                "reference_oov_rate": round(ref_oov, 4),
                "alert": alert,
                "drivers": drivers + (["oov_rate"] if oov_jump and "oov_rate" not in drivers else []),
            }
            report.append(item)
            if emit:
                metrics.emit(
                    {"firm": firm, "check": "narrative_drift"},
                    max_psi=item["max_alerting_psi"],
                    oov_rate=item["oov_rate"],
                    drift_alert=float(alert),
                )
    return {
        "reference": list(reference),
        "windows": report,
        "alerts": [w for w in report if w["alert"]],
        "thresholds": cfg["alert"],
        "alert_features": cfg["alert_features"],
    }
