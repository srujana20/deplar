"""Config-file dependency scanning.

Service endpoints are frequently *declared*, not called inline — a Spring app
points at `payments.service.url` in `application.yml`, a Node app reads a base
URL from `.env`. Code-only detection misses these. This scanner reads config /
properties files and emits the URL-valued service properties as `config`-tier
dependencies (high confidence: a declared endpoint is a real intent to call it).

Non-HTTP schemes (jdbc:, redis:, amqp:, kafka bootstrap lists, …) are skipped —
those belong to the future db/queue signals, not HTTP. Namespace URIs and
unresolved placeholders never enter.
"""
import re
from pathlib import Path
from typing import Dict, List

import yaml

from deplar.scanner.endpoints import split_host_path
from deplar.scanner.network_detector import NetworkCallEdge, is_namespace_noise

# a property key whose *name* implies it holds a service endpoint
_URLISH_KEY = re.compile(r'(url|uri|endpoint|base-?url|address|host)$', re.IGNORECASE)

# schemes that are not HTTP services (handled by other/future signals)
_NON_HTTP_SCHEME = re.compile(
    r'^(jdbc|r2dbc|mongodb(\+srv)?|redis|rediss|amqp|amqps|kafka|zookeeper|'
    r'ldap|ldaps|file|classpath):', re.IGNORECASE)

_CONFIG_GLOBS = ("application*.properties", "application*.yml", "application*.yaml",
                 "bootstrap*.yml", "bootstrap*.yaml", ".env", ".env.*")

_SKIP_DIRS = {"node_modules", ".git", "vendor", "target", "build", "dist",
              ".venv", "venv"}


def _looks_like_http_target(key: str, value: str) -> bool:
    v = value.strip()
    if not v or "${" in v or "{{" in v:      # unresolved placeholder
        return False
    if _NON_HTTP_SCHEME.match(v):
        return False
    if v.startswith("http://") or v.startswith("https://"):
        return True
    # scheme-less value under a url-ish key, and it looks host-like (has a dot)
    return bool(_URLISH_KEY.search(key)) and ("." in v.split("/")[0] or ":" in v)


def _emit(edges: List[NetworkCallEdge], src: Path, line: int, value: str):
    v = value if "://" in value else "http://" + value
    if is_namespace_noise(v):
        return
    _, path = split_host_path(v)
    edges.append(NetworkCallEdge(
        source_file=src, call_type="http", target=v, confidence=0.85,
        line_number=line, raw=f"{value}"[:120], method="", path=path,
        tier="config",
    ))


def _parse_properties(text: str) -> List[tuple]:
    out = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        m = re.match(r'([\w.\-/${}]+)\s*[=:]\s*(.+)$', line)
        if m:
            out.append((m.group(1), m.group(2).strip(), i))
    return out


def _flatten_yaml(node, prefix="") -> List[tuple]:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += _flatten_yaml(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(node, list):
        for v in node:
            out += _flatten_yaml(v, prefix)
    elif isinstance(node, str):
        out.append((prefix, node, 0))
    return out


def scan_config_file(path: Path) -> List[NetworkCallEdge]:
    edges: List[NetworkCallEdge] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return edges

    if path.suffix in (".yml", ".yaml"):
        try:
            for doc in yaml.safe_load_all(text):
                for key, value, _ in _flatten_yaml(doc):
                    if _looks_like_http_target(key, value):
                        _emit(edges, path, 0, value)
        except yaml.YAMLError:
            return edges
    else:  # .properties / .env
        for key, value, line in _parse_properties(text):
            # strip surrounding quotes common in .env
            value = value.strip().strip('"\'')
            if _looks_like_http_target(key, value):
                _emit(edges, path, line, value)
    return edges


class ConfigScanner:
    def scan(self, repo_root: Path) -> List[NetworkCallEdge]:
        root = Path(repo_root)
        seen: Dict[str, NetworkCallEdge] = {}
        for pattern in _CONFIG_GLOBS:
            for f in root.rglob(pattern):
                if any(p in _SKIP_DIRS for p in f.parts) or not f.is_file():
                    continue
                for e in scan_config_file(f):
                    # dedupe identical targets across profile files
                    seen.setdefault(e.target, e)
        return list(seen.values())
