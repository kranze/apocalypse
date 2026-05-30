"""Statischer Test: Iron Principle.

Prüft, dass app/llm/*.py keine DB-Schreibfunktionen importiert.
Verbotene Muster: Import von app.db, app.sim.ledger, sqlite3
zusammen mit Schreib-Operationen (execute, commit, INSERT, UPDATE, DELETE).

Methode: Quelltexte der LLM-Module lesen und auf verbotene Muster testen.
Keine Netzverbindung, kein Import der Module notwendig.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LLM_DIR = ROOT / "app" / "llm"


def _python_files() -> list[Path]:
    """Alle .py-Dateien in app/llm/ (außer __pycache__)."""
    return [
        p for p in LLM_DIR.glob("*.py")
        if p.name != "__pycache__"
    ]


# ---------------------------------------------------------------------------
# Verbotene Importe
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORTS = [
    "app.db",
    "app.sim.ledger",
    "app.sim.audit",
    "app.sim.generation",
    "app.sim.looting",
    "app.sim.resources",
    "app.sim.tick",
]

# Direkte Schreib-Calls auf sqlite3.Connection sind verboten
FORBIDDEN_SQLITE_WRITES = [
    "conn.execute",
    "conn.executemany",
    "conn.executescript",
    "conn.commit",
    ".execute(",
    ".executemany(",
    ".executescript(",
]

# Direkte sqlite3-Import-Nutzung mit Schreibmethoden
FORBIDDEN_SQLITE_IMPORT_WRITES = [
    "sqlite3.connect",
]


class TestIronPrinciple:
    """Statische Checks: LLM-Pakete dürfen nicht auf DB schreiben."""

    @pytest.fixture(autouse=True)
    def load_sources(self):
        self.sources: dict[str, str] = {}
        for p in _python_files():
            self.sources[p.name] = p.read_text(encoding="utf-8")

    def test_llm_files_found(self):
        """Sanity-Check: Die LLM-Module existieren."""
        assert len(self.sources) >= 2, "Zu wenige LLM-Dateien gefunden"
        assert "base.py" in self.sources
        assert "stub.py" in self.sources

    def test_no_forbidden_imports(self):
        """Keine der verbotenen DB/Sim-Importe in LLM-Modulen."""
        violations = []
        for fname, src in self.sources.items():
            for pattern in FORBIDDEN_IMPORTS:
                # import app.sim.ledger  ODER  from app.sim import ledger
                if f"import {pattern}" in src or f"from {pattern}" in src:
                    violations.append(f"{fname}: verbotener Import '{pattern}'")
        assert not violations, "\n".join(violations)

    def test_no_sqlite3_direct_import(self):
        """Kein direktes 'import sqlite3' in LLM-Modulen."""
        violations = []
        for fname, src in self.sources.items():
            lines = src.splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("import sqlite3") or stripped.startswith("from sqlite3"):
                    violations.append(f"{fname}:{i}: direkter sqlite3-Import")
        assert not violations, "\n".join(violations)

    def test_no_app_db_import(self):
        """Kein Import von app.db (DB-Verbindungs-Management)."""
        violations = []
        for fname, src in self.sources.items():
            if "app.db" in src and "import" in src:
                # Gezielter Check: Zeilen mit import + app.db
                for i, line in enumerate(src.splitlines(), 1):
                    if "import" in line and "app.db" in line:
                        violations.append(f"{fname}:{i}: app.db-Import")
        assert not violations, "\n".join(violations)

    def test_no_sql_write_keywords_in_llm_code(self):
        """Keine rohen SQL-Schreibbefehle (INSERT/UPDATE/DELETE) direkt in LLM-Modulen.

        claude.py ruft anthropic SDK, nicht DB -> kein INSERT/UPDATE/DELETE erwartet.
        base.py und stub.py sollen DB-agnostisch sein.
        """
        violations = []
        sql_writes = ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE", "CREATE TABLE"]
        for fname, src in self.sources.items():
            upper = src.upper()
            for kw in sql_writes:
                if kw in upper:
                    violations.append(f"{fname}: enthält SQL-Schreibbefehl '{kw}'")
        assert not violations, "\n".join(violations)

    def test_base_py_no_db_dependency(self):
        """base.py ist rein abstrakt — keine DB-Abhängigkeit."""
        src = self.sources.get("base.py", "")
        assert src, "base.py nicht gefunden"
        assert "sqlite3" not in src
        assert "conn" not in src or "connection" not in src.lower()

    def test_stub_py_no_db_write(self):
        """stub.py (RuleBackend) darf keine DB schreiben."""
        src = self.sources.get("stub.py", "")
        assert src, "stub.py nicht gefunden"
        assert "sqlite3" not in src
        # Kein conn.execute, kein INSERT
        assert ".execute(" not in src
        assert "INSERT" not in src.upper()
        assert "UPDATE" not in src.upper()
        assert "DELETE" not in src.upper()

    def test_llm_init_no_db_write(self):
        """__init__.py wählt nur Backend — kein DB-Zugriff."""
        src = self.sources.get("__init__.py", "")
        assert src, "__init__.py nicht gefunden"
        assert "sqlite3" not in src
        assert ".execute(" not in src

    def test_parse_via_ast_no_db_calls(self):
        """AST-Check: In allen LLM-Dateien gibt es keine Attribute-Calls 'execute'
        oder 'commit' auf irgendeinem Objekt (würde DB-Schreiben bedeuten)."""
        violations = []
        for fname, src in self.sources.items():
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute):
                        if func.attr in ("execute", "executemany", "executescript", "commit"):
                            violations.append(
                                f"{fname}:{getattr(node, 'lineno', '?')}: "
                                f"DB-Methode '{func.attr}' aufgerufen"
                            )
        assert not violations, "\n".join(violations)
