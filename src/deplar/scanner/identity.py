"""Provider-side identity extraction.

Deplar historically knew a repo only by its folder name, then guessed inbound
references against it. That breaks whenever the deployed/service name differs
from the folder. This module instead extracts what a repo *advertises itself
as* — from its package manifest, framework config, k8s Service, OpenAPI spec and
git remote — producing an identity catalog. The reconciler matches consumer
references against that catalog (see reconciler.py).
"""
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml

# Generic tokens that carry no identity on their own (service-name and env-var
# suffixes that would otherwise defeat matching).
_GENERIC_TOKENS = {"svc", "service", "services", "api", "apis", "srv",
                   "app", "server", "backend", "gateway",
                   "url", "uri", "endpoint", "host", "addr", "base", "baseurl"}

_DOMAIN_SUFFIXES = re.compile(
    r'\.(internal|local|svc\.cluster\.local|cluster\.local|com|io|net|org|dev)$'
)


@dataclass
class IdentityAlias:
    alias: str        # normalized form used for matching
    raw: str          # original declared value
    source: str       # config | package | spring | go | k8s | openapi | git
    confidence: float

    def as_dict(self) -> dict:
        return {"alias": self.alias, "raw": self.raw,
                "source": self.source, "confidence": self.confidence}


def normalize_identity(value: str) -> str:
    """Fold a name/URL/hostname to a canonical matching key.

    e.g. 'https://payments-svc.internal/v1' -> 'payments'
         'payment-service' -> 'payment'   (generic 'service' token dropped)
    """
    if not value:
        return ""
    s = value.strip().lower()
    s = re.sub(r'^\$env:', '', s)          # $ENV:FOO wrapper
    s = re.sub(r'^https?://', '', s)        # scheme
    s = s.split("/")[0].split(":")[0]        # host, drop path + port
    s = _DOMAIN_SUFFIXES.sub("", s)          # trailing infra/DNS suffixes
    s = re.sub(r'[._\s]+', '-', s)           # unify separators
    tokens = [t for t in s.split("-") if t]
    core = [t for t in tokens if t not in _GENERIC_TOKENS] or tokens
    return "-".join(core)


def stem(norm: str) -> str:
    """Plural-fold the last token so 'payments' matches 'payment'."""
    if not norm:
        return ""
    tokens = norm.split("-")
    if tokens and len(tokens[-1]) > 3 and tokens[-1].endswith("s"):
        tokens[-1] = tokens[-1][:-1]
    return "-".join(tokens)


def _alias(raw: str, source: str, confidence: float) -> IdentityAlias:
    return IdentityAlias(normalize_identity(raw), raw, source, confidence)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _package_json(root: Path, out: List[IdentityAlias]):
    pkg = root / "package.json"
    if not pkg.exists():
        return
    try:
        name = json.loads(_read(pkg)).get("name", "")
    except json.JSONDecodeError:
        return
    if name:
        out.append(_alias(name.split("/")[-1], "package", 0.9))


def _pyproject(root: Path, out: List[IdentityAlias]):
    py = root / "pyproject.toml"
    if not py.exists():
        return
    try:
        import tomllib
        data = tomllib.loads(_read(py))
    except Exception:
        return
    name = (data.get("project", {}) or {}).get("name", "")
    if name:
        out.append(_alias(name, "package", 0.9))


def _go_mod(root: Path, out: List[IdentityAlias]):
    mod = root / "go.mod"
    if not mod.exists():
        return
    m = re.search(r'^module\s+(\S+)', _read(mod), re.MULTILINE)
    if m:
        out.append(_alias(m.group(1).rstrip("/").split("/")[-1], "go", 0.9))


def _spring(root: Path, out: List[IdentityAlias]):
    for rel in ("src/main/resources/application.yml",
                "src/main/resources/application.yaml",
                "application.yml", "application.yaml"):
        f = root / rel
        if f.exists():
            try:
                data = yaml.safe_load(_read(f)) or {}
                name = (((data.get("spring") or {}).get("application") or {})
                        .get("name", ""))
                if name:
                    out.append(_alias(name, "spring", 0.95))
                    return
            except yaml.YAMLError:
                pass
    props = root / "src/main/resources/application.properties"
    if props.exists():
        m = re.search(r'spring\.application\.name\s*=\s*(\S+)', _read(props))
        if m:
            out.append(_alias(m.group(1), "spring", 0.95))


def _k8s_services(root: Path, out: List[IdentityAlias]):
    seen = set()
    for f in list(root.rglob("*.yaml"))[:200] + list(root.rglob("*.yml"))[:200]:
        if any(p in f.parts for p in ("node_modules", ".git", "vendor")):
            continue
        text = _read(f)
        if "kind: Service" not in text and "kind:Service" not in text:
            continue
        try:
            for doc in yaml.safe_load_all(text):
                if isinstance(doc, dict) and doc.get("kind") == "Service":
                    name = (doc.get("metadata") or {}).get("name")
                    if name and name not in seen:
                        seen.add(name)
                        out.append(_alias(name, "k8s", 0.9))
        except yaml.YAMLError:
            continue


def _openapi(root: Path, out: List[IdentityAlias]):
    for name in ("openapi.yaml", "openapi.yml", "openapi.json",
                 "swagger.yaml", "swagger.json"):
        f = root / name
        if not f.exists():
            continue
        try:
            data = (json.loads(_read(f)) if f.suffix == ".json"
                    else yaml.safe_load(_read(f))) or {}
        except (json.JSONDecodeError, yaml.YAMLError):
            continue
        title = (data.get("info") or {}).get("title", "")
        if title:
            out.append(_alias(title, "openapi", 0.7))
        for server in data.get("servers", []) or []:
            url = server.get("url", "") if isinstance(server, dict) else ""
            if url and "://" in url:
                out.append(_alias(url, "openapi", 0.85))
        return


def _git_remote(root: Path, out: List[IdentityAlias]):
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return
    url = res.stdout.strip()
    if res.returncode == 0 and url:
        name = re.sub(r'\.git$', '', url).rstrip("/").split("/")[-1]
        if name:
            out.append(_alias(name, "git", 0.85))


def extract_identities(repo_path: Path, repo_name: str) -> List[IdentityAlias]:
    """Build the identity catalog for a repo.

    Always includes the canonical repo name; adds any declared identities found.
    De-duplicated by normalized alias, keeping the highest-confidence source.
    """
    root = Path(repo_path)
    found: List[IdentityAlias] = [_alias(repo_name, "config", 1.0)]

    for extractor in (_package_json, _pyproject, _go_mod, _spring,
                      _k8s_services, _openapi, _git_remote):
        try:
            extractor(root, found)
        except Exception:
            continue

    best: dict[str, IdentityAlias] = {}
    for a in found:
        if not a.alias:
            continue
        if a.alias not in best or a.confidence > best[a.alias].confidence:
            best[a.alias] = a
    return list(best.values())
