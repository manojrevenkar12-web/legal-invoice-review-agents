"""Versioned prompts.

Each prompt is a YAML file in lira/prompts with an id and a version. Its hash covers the text and the output
schema, and prompts.lock pins (version, hash) for every prompt. Changing a prompt without raising its version,
or raising a version without a recorded regression run, fails `lira prompts check` in CI (see eval.prompt_regression).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from lira.llm.schemas import TOOLS_FOR_PROMPT

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    description: str
    system: str
    user: str

    @property
    def sha256(self) -> str:
        tools = [t.input_schema for t in TOOLS_FOR_PROMPT.get(self.id, [])]
        blob = json.dumps({"system": self.system, "user": self.user, "tools": tools}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def render_user(self, **values: Any) -> str:
        return render(self.user, **values)

    def variables(self) -> set[str]:
        return set(_VAR.findall(self.user))


def render(template: str, **values: Any) -> str:
    missing = set(_VAR.findall(template)) - set(values)
    if missing:
        raise KeyError(f"prompt variables not provided: {sorted(missing)}")
    return _VAR.sub(lambda m: str(values[m.group(1)]), template)


@cache
def load(prompt_id: str) -> Prompt:
    text = resources.files("lira.prompts").joinpath(f"{prompt_id}.yaml").read_text()
    d = yaml.safe_load(text)
    if d["id"] != prompt_id:
        raise ValueError(f"{prompt_id}.yaml declares id {d['id']}")
    return Prompt(d["id"], int(d["version"]), d["description"], d["system"].strip(), d["user"].strip())


def all_prompts() -> list[Prompt]:
    names = sorted(p.name.removesuffix(".yaml") for p in resources.files("lira.prompts").iterdir() if p.name.endswith(".yaml"))
    return [load(n) for n in names]


def lock_entries() -> dict[str, dict[str, Any]]:
    return {p.id: {"version": p.version, "sha256": p.sha256} for p in all_prompts()}


def check_lock(lock_path: Path, history_path: Path) -> list[str]:
    """Problems that block a release: prompt text changed without a version bump, or a new version never evaluated."""
    problems: list[str] = []
    locked = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    evaluated: set[tuple[str, int, str]] = set()
    if history_path.exists():
        for line in history_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                evaluated.add((r["prompt_id"], int(r["version"]), r["sha256"]))
    for p in all_prompts():
        lk = locked.get(p.id)
        if lk is None:
            problems.append(f"{p.id}: not in prompts.lock (run `lira prompts lock` after its regression run)")
            continue
        if lk["sha256"] != p.sha256 and lk["version"] == p.version:
            problems.append(f"{p.id}: text or schema changed but version is still {p.version}; raise the version")
        if (p.id, p.version, p.sha256) not in evaluated:
            problems.append(f"{p.id} v{p.version}: no regression run recorded for this exact prompt (run `lira eval prompts`)")
    return problems
