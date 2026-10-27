"""Invariants that keep the evaluation honest and the service safe, checked on the source code itself."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "lira"
ALLOWED = {"sim", "eval"}  # only these may know the answer key


def _modules() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if p.relative_to(SRC).parts[0] not in ALLOWED]


def test_shipping_code_never_imports_the_simulation_or_the_evaluation():
    offenders = []
    for p in _modules():
        for node in ast.walk(ast.parse(p.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.startswith(("lira.sim", "lira.eval")) and not (
                    p.name == "cli.py" or (p.parts[-2] == "service" and p.name == "app.py")
                ):
                    offenders.append(f"{p.relative_to(SRC)} imports {n}")
    assert offenders == []


def test_shipping_code_never_reads_the_answer_key_or_the_world_config():
    offenders = []
    for p in _modules():
        text = p.read_text()
        for needle in ('"truth"', "'truth'", "/truth", "world.yaml"):
            if needle in text and p.name != "cli.py":
                offenders.append(f"{p.relative_to(SRC)} mentions {needle}")
    assert offenders == []


def test_the_cli_only_reaches_simulation_and_evaluation_through_their_commands():
    tree = ast.parse((SRC / "cli.py").read_text())
    top_level = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    assert all(not (isinstance(n, ast.ImportFrom) and (n.module or "").startswith(("lira.sim", "lira.eval"))) for n in top_level)


def test_decisions_and_audit_are_append_only_and_tenant_tables_force_row_level_security():
    sql = (SRC / "service" / "migrations" / "001_schema.sql").read_text()
    assert "CREATE TRIGGER decisions_append_only BEFORE UPDATE OR DELETE ON decisions" in sql
    assert "CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit" in sql
    for table in ("rulesets", "invoices", "findings", "decisions", "appeals", "audit"):
        assert f"'{table}'" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql


def test_the_container_image_does_not_ship_the_answer_key():
    root = SRC.parents[1]
    assert "data/truth" in (root / ".dockerignore").read_text()
    assert "COPY data/truth" not in (root / "deploy" / "Dockerfile").read_text()
