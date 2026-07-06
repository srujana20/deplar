"""SQLite-backed store for the symbol graph and dependency edges.

This is the Phase 2 upgrade from the flat deps.json: an embeddable, queryable
store that holds per-repo symbols, call sites, and cross-repo dependency edges.
No external service required. deps.json remains the portable export format;
this DB is the queryable index the MCP server reads.
"""
import json
import sqlite3
from pathlib import Path
from typing import List, Optional

from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.symbols import SymbolIndex

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    name       TEXT PRIMARY KEY,
    path       TEXT,
    scanned_at TEXT
);
CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    repo           TEXT NOT NULL,
    file           TEXT NOT NULL,
    language       TEXT,
    kind           TEXT,
    name           TEXT,
    qualified_name TEXT,
    signature      TEXT,
    parent         TEXT,
    start_line     INTEGER,
    end_line       INTEGER
);
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    file        TEXT NOT NULL,
    caller      TEXT,
    callee      TEXT,
    callee_name TEXT,
    line        INTEGER
);
CREATE TABLE IF NOT EXISTS dependencies (
    from_repo  TEXT NOT NULL,
    to_repo    TEXT NOT NULL,
    dep_types  TEXT,
    confidence REAL,
    evidence   TEXT,
    PRIMARY KEY (from_repo, to_repo)
);
CREATE TABLE IF NOT EXISTS memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo       TEXT NOT NULL,
    kind       TEXT,
    note       TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
    repo       TEXT NOT NULL,
    alias      TEXT NOT NULL,   -- normalized form used for matching
    raw        TEXT,            -- original declared value
    source     TEXT,            -- config | package | spring | go | k8s | openapi | git | manual
    confidence REAL,
    PRIMARY KEY (repo, alias)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_repo ON symbols(repo);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name);
CREATE INDEX IF NOT EXISTS idx_deps_to ON dependencies(to_repo);
CREATE INDEX IF NOT EXISTS idx_deps_from ON dependencies(from_repo);
CREATE INDEX IF NOT EXISTS idx_memory_repo ON memory(repo);
CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
"""

VALID_MEMORY_KINDS = ("pattern", "convention", "gotcha", "note")


class SymbolStore:
    def __init__(self, path: Path = Path("deplar.db")):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- ingestion ---

    def upsert_repo(self, name: str, path: str, scanned_at: str):
        self.conn.execute(
            "INSERT INTO repos(name, path, scanned_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET path=excluded.path, "
            "scanned_at=excluded.scanned_at",
            (name, path, scanned_at),
        )
        self.conn.commit()

    def replace_symbols(self, repo: str, index: SymbolIndex):
        """Replace all symbols/calls for a repo (idempotent re-scan)."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM symbols WHERE repo = ?", (repo,))
        cur.execute("DELETE FROM calls WHERE repo = ?", (repo,))
        cur.executemany(
            "INSERT INTO symbols(repo, file, language, kind, name, "
            "qualified_name, signature, parent, start_line, end_line) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(s.repo, s.file, s.language, s.kind, s.name, s.qualified_name,
              s.signature, s.parent, s.start_line, s.end_line)
             for s in index.symbols],
        )
        cur.executemany(
            "INSERT INTO calls(repo, file, caller, callee, callee_name, line) "
            "VALUES (?,?,?,?,?,?)",
            [(c.repo, c.file, c.caller, c.callee, c.callee_name, c.line)
             for c in index.calls],
        )
        self.conn.commit()

    def replace_dependencies(self, edges: List[DependencyEdge]):
        cur = self.conn.cursor()
        for e in edges:
            cur.execute(
                "INSERT INTO dependencies(from_repo, to_repo, dep_types, "
                "confidence, evidence) VALUES (?,?,?,?,?) "
                "ON CONFLICT(from_repo, to_repo) DO UPDATE SET "
                "dep_types=excluded.dep_types, confidence=excluded.confidence, "
                "evidence=excluded.evidence",
                (e.from_repo, e.to_repo, json.dumps(e.dep_types),
                 e.confidence, json.dumps(e.evidence)),
            )
        self.conn.commit()

    def all_dependencies(self) -> List[DependencyEdge]:
        rows = self.conn.execute(
            "SELECT from_repo, to_repo, dep_types, confidence, evidence "
            "FROM dependencies"
        ).fetchall()
        return [
            DependencyEdge(
                from_repo=r["from_repo"], to_repo=r["to_repo"],
                dep_types=json.loads(r["dep_types"]),
                confidence=r["confidence"], evidence=json.loads(r["evidence"]),
            ) for r in rows
        ]

    def clear_dependencies(self):
        self.conn.execute("DELETE FROM dependencies")
        self.conn.commit()

    # --- identity catalog (what each repo advertises itself as) ---

    def replace_aliases(self, repo: str, aliases: List[dict]):
        """Replace a repo's *auto-extracted* aliases (from a re-scan).

        Manual overrides are preserved — a human pin set once survives rescans.
        Each item: {alias, raw, source, confidence}.
        """
        cur = self.conn.cursor()
        cur.execute("DELETE FROM aliases WHERE repo = ? AND source != 'manual'",
                    (repo,))
        cur.executemany(
            "INSERT OR REPLACE INTO aliases(repo, alias, raw, source, confidence) "
            "VALUES (?,?,?,?,?)",
            [(repo, a["alias"], a.get("raw", a["alias"]), a.get("source", "manual"),
              a.get("confidence", 0.8)) for a in aliases if a.get("alias")],
        )
        self.conn.commit()

    def add_alias(self, repo: str, raw: str, source: str = "manual",
                  confidence: float = 1.0) -> str:
        """Pin an identity to a repo (survives rescans). Returns the normalized alias."""
        from deplar.scanner.identity import normalize_identity
        alias = normalize_identity(raw)
        if not alias:
            return ""
        self.conn.execute(
            "INSERT OR REPLACE INTO aliases(repo, alias, raw, source, confidence) "
            "VALUES (?,?,?,?,?)",
            (repo, alias, raw, source, confidence),
        )
        self.conn.commit()
        return alias

    def remove_alias(self, repo: str, raw_or_alias: str) -> bool:
        """Unpin an alias. Accepts either the raw value or its normalized form."""
        from deplar.scanner.identity import normalize_identity
        alias = normalize_identity(raw_or_alias)
        cur = self.conn.execute(
            "DELETE FROM aliases WHERE repo = ? AND (alias = ? OR raw = ?)",
            (repo, alias, raw_or_alias),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def all_aliases(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT repo, alias, raw, source, confidence FROM aliases"
        ).fetchall()
        return [dict(r) for r in rows]

    def aliases_for_repo(self, repo: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT repo, alias, raw, source, confidence FROM aliases "
            "WHERE repo = ? ORDER BY confidence DESC", (repo,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- queries ---

    def get_dependencies(self, repo: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT to_repo, dep_types, confidence FROM dependencies "
            "WHERE from_repo = ? ORDER BY confidence DESC", (repo,)
        ).fetchall()
        return [{"repo": r["to_repo"], "types": json.loads(r["dep_types"]),
                 "confidence": r["confidence"]} for r in rows]

    def get_dependents(self, repo: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT from_repo, dep_types, confidence FROM dependencies "
            "WHERE to_repo = ? ORDER BY confidence DESC", (repo,)
        ).fetchall()
        return [{"repo": r["from_repo"], "types": json.loads(r["dep_types"]),
                 "confidence": r["confidence"]} for r in rows]

    def blast_radius(self, repo: str, depth: int = 3) -> List[str]:
        """Transitive set of repos that (in)directly depend on `repo`."""
        visited: set[str] = set()
        frontier = {repo}
        for _ in range(depth):
            nxt: set[str] = set()
            for node in frontier:
                for dep in self.get_dependents(node):
                    if dep["repo"] not in visited and dep["repo"] != repo:
                        nxt.add(dep["repo"])
            visited |= frontier
            frontier = nxt
            if not frontier:
                break
        visited.discard(repo)
        return sorted(visited)

    def search_symbols(self, query: str, repo: Optional[str] = None,
                       limit: int = 50) -> List[dict]:
        sql = ("SELECT repo, file, kind, name, qualified_name, signature, "
               "start_line, end_line FROM symbols WHERE name LIKE ?")
        params: list = [f"%{query}%"]
        if repo:
            sql += " AND repo = ?"
            params.append(repo)
        sql += " ORDER BY (name = ?) DESC, length(name) ASC LIMIT ?"
        params.extend([query, limit])
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def symbols_for_repo(self, repo: str, kinds: Optional[List[str]] = None,
                         limit: int = 200) -> List[dict]:
        sql = ("SELECT file, language, kind, name, qualified_name, signature, "
               "parent, start_line, end_line FROM symbols WHERE repo = ?")
        params: list = [repo]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY file, start_line LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_callers(self, symbol_name: str, repo: Optional[str] = None,
                    limit: int = 100) -> List[dict]:
        sql = ("SELECT repo, file, caller, callee, line FROM calls "
               "WHERE callee_name = ?")
        params: list = [symbol_name]
        if repo:
            sql += " AND repo = ?"
            params.append(repo)
        sql += " ORDER BY repo, file, line LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # --- agent memory (long-term, persisted across sessions) ---

    def remember(self, repo: str, note: str, kind: str = "note",
                 created_at: str = "") -> int:
        if kind not in VALID_MEMORY_KINDS:
            kind = "note"
        cur = self.conn.execute(
            "INSERT INTO memory(repo, kind, note, created_at) VALUES (?,?,?,?)",
            (repo, kind, note, created_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def recall(self, repo: str, kind: Optional[str] = None) -> List[dict]:
        sql = "SELECT id, kind, note, created_at FROM memory WHERE repo = ?"
        params: list = [repo]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def forget(self, memory_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # --- versioning ---

    def snapshot_hash(self, repo: str) -> str:
        """Short stable hash of a repo's symbols + dependency edges.

        Ties a generated skill/report to the exact graph state it came from.
        """
        import hashlib

        parts: List[str] = []
        for r in self.conn.execute(
            "SELECT qualified_name, signature FROM symbols WHERE repo = ? "
            "ORDER BY qualified_name, signature", (repo,)
        ):
            parts.append(f"{r['qualified_name']}|{r['signature']}")
        for r in self.conn.execute(
            "SELECT to_repo, dep_types FROM dependencies WHERE from_repo = ? "
            "ORDER BY to_repo", (repo,)
        ):
            parts.append(f"->{r['to_repo']}:{r['dep_types']}")
        digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
        return digest[:12]

    def list_repos(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT name, path, scanned_at FROM repos ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def repo_path(self, repo: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT path FROM repos WHERE name = ?", (repo,)
        ).fetchone()
        return row["path"] if row else None

    def close(self):
        self.conn.close()
