"""Classical NLP models trained on the client's own review history.

* block billing, vague description, clerical work: calibrated logistic regression over word and character
  n-grams plus a few structural features (task segments, length, generic phrases, role, activity code);
* UTBMS task code: multinomial logistic regression over the normalised narrative;
* narrative topics: NMF over TF-IDF, used for drift monitoring and novelty;
* hours: robust (median/MAD) log-hours per task code and timekeeper class, for unusual time;
* appeal intent: logistic regression over the firm's appeal message.

Labels are past reviewers' decisions, which are noisy (reviewers miss lines and disagree on reasons), so
probabilities are calibrated on a later time slice and the models only decide which lines a language model
reads; they never adjust an invoice on their own.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from scipy import sparse
from sklearn.calibration import CalibratedClassifierCV
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, f1_score, roc_auc_score

from lira.nlp.text import normalize, task_segments

GENERIC = (
    "attention to file",
    "work on matter",
    "review documents",
    "legal research",
    "trial preparation",
    "correspondence",
    "review file",
    "follow up",
    "draft document",
    "discovery work",
    "prepare for meeting",
    "work on brief",
    "various emails",
    "review and revise",
    "case management",
    "analysis",
    "research",
    "conference",
)
CLERICAL_WORDS = (
    "organize",
    "organise",
    "copy",
    "distribute",
    "calendar",
    "electronic filing",
    "scan",
    "schedule",
    "bates",
    "assemble",
    "binder",
    "contact list",
    "courier",
    "book meeting",
    "rename",
    "index",
    "tab exhibits",
    "catering",
    "label",
    "file notice",
)
CLASSES = ("PT", "AS", "OC", "LA", "OT")
ACTIVITIES = tuple(f"A{n}" for n in range(101, 112))
JUDGEMENT = ("block_billing", "vague_description", "clerical_work")


def structural(description: str, timekeeper_class: str, activity: str, units: float) -> list[float]:
    norm = normalize(description)
    words = norm.split()
    bare = re.sub(r"[^a-z ]", "", norm).strip()
    segs = task_segments(description)
    return [
        float(len(segs)),
        float(len(segs) >= 2),
        math.log1p(len(words)),
        float(len(words) <= 3),
        float(";" in description or " / " in description),
        float(any(bare == g or (bare.startswith(g + " ") and len(words) <= 4) for g in GENERIC)),
        float(sum(w in norm for w in CLERICAL_WORDS)),
        float(bool(re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+", description))),
        math.log1p(max(units, 0.0)),
        *[float(timekeeper_class == c) for c in CLASSES],
        *[float(activity == a) for a in ACTIVITIES],
    ]


@dataclass
class TextModels:
    word: TfidfVectorizer
    char: TfidfVectorizer

    @classmethod
    def fit(cls, texts: list[str]) -> TextModels:
        w = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, max_features=40000)
        c = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=3, sublinear_tf=True, max_features=60000)
        w.fit(texts)
        c.fit(texts)
        return cls(w, c)

    def transform(self, texts: list[str], struct: np.ndarray) -> sparse.csr_matrix:
        return sparse.hstack([self.word.transform(texts), self.char.transform(texts), sparse.csr_matrix(struct)]).tocsr()


@dataclass
class NlpModels:
    version: str
    text: TextModels
    judgement: dict[str, Any]  # question -> calibrated classifier
    task_vec: TfidfVectorizer
    task_clf: LogisticRegression
    topic_vec: TfidfVectorizer
    topics: NMF
    hours: dict[str, tuple[float, float]]  # "task|class" -> (median log-hours, MAD)
    intent_vec: TfidfVectorizer | None = None
    intent_clf: LogisticRegression | None = None
    card: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- inference
    def judgement_probs(self, rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
        if not rows:
            return {q: np.zeros(0) for q in self.judgement}
        texts = [normalize(r["description"]) for r in rows]
        struct = np.array(
            [
                structural(r["description"], r.get("timekeeper_class", ""), r.get("activity_code", ""), float(r.get("units", 0)))
                for r in rows
            ]
        )
        X = self.text.transform(texts, struct)
        return {q: m.predict_proba(X)[:, 1] for q, m in self.judgement.items()}

    def task_codes(self, descriptions: list[str]) -> tuple[list[str], np.ndarray]:
        if not descriptions:
            return [], np.zeros(0)
        P = self.task_clf.predict_proba(self.task_vec.transform([normalize(d) for d in descriptions]))
        best = P.argmax(axis=1)
        return [str(self.task_clf.classes_[i]) for i in best], P.max(axis=1)

    def topic_mix(self, descriptions: list[str]) -> np.ndarray:
        W = self.topics.transform(self.topic_vec.transform([normalize(d) for d in descriptions]))
        s = W.sum(axis=1, keepdims=True)
        return np.divide(W, s, out=np.full_like(W, 1.0 / W.shape[1]), where=s > 0)

    def hours_z(self, task: str, activity: str, cls: str, units: float) -> float:
        med, mad = (
            self.hours.get(f"{task}|{activity}|{cls}")
            or self.hours.get(f"{task}|{activity}|*")
            or self.hours.get(f"{task}|*|*")
            or self.hours["*|*|*"]
        )
        return (math.log(max(units, 0.1)) - med) / max(mad * 1.4826, 0.15)

    def intents(self, messages: list[str]) -> list[tuple[str, float]]:
        if self.intent_clf is None or self.intent_vec is None:
            return [("unknown", 0.0) for _ in messages]
        P = self.intent_clf.predict_proba(self.intent_vec.transform([m.lower() for m in messages]))
        return [(str(self.intent_clf.classes_[i]), float(P[k, i])) for k, i in enumerate(P.argmax(axis=1))]

    # ---------------------------------------------------------------- persistence
    def save(self, d: Path) -> None:
        d.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, d / "nlp.joblib")
        (d / "model_card.json").write_text(json.dumps(self.card, indent=2, default=str))

    @staticmethod
    def load(d: Path) -> NlpModels:
        m = joblib.load(d / "nlp.joblib")
        if not isinstance(m, NlpModels):
            raise TypeError("not an NlpModels artifact")
        return m


def train(
    history: list[dict[str, Any]], appeals: list[dict[str, Any]] | None = None, validation_from: str | None = None, seed: int = 7
) -> NlpModels:
    """history rows: description, task_code, activity_code, timekeeper_class, units, type, date, flags (past reviewer labels)."""
    fees = [r for r in history if r["type"] == "F"]
    if validation_from:
        tr = [r for r in fees if r["date"] < validation_from]
        va = [r for r in fees if r["date"] >= validation_from]
    else:
        tr, va = fees, []
    texts = [normalize(r["description"]) for r in tr]
    struct = np.array([structural(r["description"], r["timekeeper_class"], r["activity_code"], float(r["units"])) for r in tr])
    tm = TextModels.fit(texts)
    X = tm.transform(texts, struct)
    judgement: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    for q in JUDGEMENT:
        y = np.array([q in r["flags"] for r in tr], dtype=int)
        base = LogisticRegression(C=2.0, class_weight="balanced", max_iter=2000, random_state=seed)
        clf = CalibratedClassifierCV(base, method="sigmoid", cv=3)
        clf.fit(X, y)
        judgement[q] = clf
        if va:
            Xv = tm.transform(
                [normalize(r["description"]) for r in va],
                np.array([structural(r["description"], r["timekeeper_class"], r["activity_code"], float(r["units"])) for r in va]),
            )
            yv = np.array([q in r["flags"] for r in va], dtype=int)
            p = clf.predict_proba(Xv)[:, 1]
            metrics[q] = {
                "auc_vs_reviewer_labels": round(float(roc_auc_score(yv, p)), 4) if 0 < yv.sum() < len(yv) else None,
                "ap_vs_reviewer_labels": round(float(average_precision_score(yv, p)), 4) if yv.sum() else None,
                "positives": int(yv.sum()),
                "n": len(yv),
            }
    # task codes (firm-provided codes are mostly right; a few percent are slips)
    task_vec = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True)
    Xt = task_vec.fit_transform(texts)
    task_clf = LogisticRegression(C=4.0, max_iter=3000, random_state=seed)
    task_clf.fit(Xt, [r["task_code"] for r in tr])
    if va:
        pv = task_clf.predict(task_vec.transform([normalize(r["description"]) for r in va]))
        metrics["task_code"] = {
            "accuracy_vs_firm_codes": round(float(np.mean(pv == np.array([r["task_code"] for r in va]))), 4),
            "macro_f1": round(float(f1_score([r["task_code"] for r in va], pv, average="macro")), 4),
        }
    # topics
    topic_vec = TfidfVectorizer(min_df=3, max_df=0.5, stop_words="english")
    Tt = topic_vec.fit_transform(texts)
    topics = NMF(n_components=12, init="nndsvda", random_state=seed, max_iter=400)
    topics.fit(Tt)
    vocab = np.array(topic_vec.get_feature_names_out())
    metrics["topics"] = [" ".join(vocab[np.argsort(-c)[:5]]) for c in topics.components_]
    # hours per task code and class
    groups: dict[str, list[float]] = {}
    for r in tr:
        h = math.log(max(float(r["units"]), 0.1))
        for key in (
            f"{r['task_code']}|{r['activity_code']}|{r['timekeeper_class']}",
            f"{r['task_code']}|{r['activity_code']}|*",
            f"{r['task_code']}|*|*",
            "*|*|*",
        ):
            groups.setdefault(key, []).append(h)
    hours = {k: (float(np.median(v)), float(np.median(np.abs(np.array(v) - np.median(v))))) for k, v in groups.items() if len(v) >= 8}
    model = NlpModels(
        version="", text=tm, judgement=judgement, task_vec=task_vec, task_clf=task_clf, topic_vec=topic_vec, topics=topics, hours=hours
    )
    if appeals:
        iv = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)
        Xi = iv.fit_transform([a["message"].lower() for a in appeals])
        ic = LogisticRegression(C=4.0, max_iter=2000, random_state=seed)
        ic.fit(Xi, [a["intent"] for a in appeals])
        model.intent_vec, model.intent_clf = iv, ic
    digest = hashlib.sha256(json.dumps([[r["description"], r["flags"], r["task_code"]] for r in tr]).encode()).hexdigest()[:12]
    model.version = f"nlp-{digest}"
    model.card = {
        "version": model.version,
        "trained_on_rows": len(tr),
        "validated_on_rows": len(va),
        "validation_from": validation_from,
        "labels": "past reviewer decisions (noisy)",
        "metrics": metrics,
    }
    return model
