"""Server base-path detection — the prefix every provided route sits behind.

A Spring controller declares `@PostMapping("/create-pnr")`, but the service is
actually reachable at `/pss-api/create-pnr` because `application.properties` sets
`server.servlet.context-path=/pss-api`. That context path lives in config, not in
any annotation, so `route_detector` never sees it. A consumer, however, calls the
*full* external path — so the provider's declared surface (`/create-pnr`) and the
consumer's call (`/pss-api/create-pnr`) never match.

This module reads the base prefix per repo and hands it back so the caller can
prepend it to the routes that repo provides, making the provider's recorded
surface the real external one.

The base prefix has a framework-specific *source*:

    Spring MVC   server.servlet.context-path (Boot 2+), server.context-path
                 (Boot 1.x), + spring.mvc.servlet.path        -> config
    Spring WebFlux  spring.webflux.base-path                  -> config
    NestJS       app.setGlobalPrefix('api') in main.ts        -> code (TODO)
    Express      app.use('/api', router)                      -> code (handled
                 already, per-file, in route_detector)
    FastAPI      FastAPI(root_path=…) / include_router(prefix) -> code (TODO)
    Flask        Blueprint(url_prefix=…)                       -> code (TODO)

Only the config-driven Spring sources are resolved here today; the code-driven
bases are left as clearly-marked seams for later.
"""
import re
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

from deplar.scanner.config_scanner import _flatten_yaml, _parse_properties
from deplar.scanner.endpoints import normalize_path

# base config file names — profile variants (application-prod.yml) are skipped on
# purpose so a prod-only override never masquerades as the default surface.
_BASE_CONFIG_NAMES = (
    "application.properties", "application.yml", "application.yaml",
    "bootstrap.properties", "bootstrap.yml", "bootstrap.yaml",
)

_SKIP_DIRS = {"node_modules", ".git", "vendor", "target", "build", "dist",
              ".venv", "venv"}

# Spring keys, most-preferred first within each role.
_CONTEXT_KEYS = ("server.servlet.context-path", "server.context-path")
_SERVLET_KEYS = ("spring.mvc.servlet.path",)
_WEBFLUX_KEYS = ("spring.webflux.base-path",)

# ${VAR} or ${VAR:default} — we can only trust the literal default.
_PLACEHOLDER = re.compile(r"^\$\{([^:}]+)(?::([^}]*))?\}$")


def _resolve_placeholder(value: str) -> str:
    """A config value may be a Spring property placeholder. Keep only what is
    statically knowable: the `:default` of `${VAR:default}`, or the literal.
    A bare `${VAR}` (no default) or a value with an embedded placeholder is
    unknowable at scan time and folds to empty."""
    v = (value or "").strip().strip("\"'")
    m = _PLACEHOLDER.match(v)
    if m:
        return (m.group(2) or "").strip()
    if "${" in v:
        return ""
    return v


def _join_prefix(a: str, b: str) -> str:
    """Concatenate two path prefixes with exactly one separating slash."""
    a = (a or "").strip().rstrip("/")
    b = (b or "").strip()
    if b and not b.startswith("/"):
        b = "/" + b
    return (a + b) or ""


def _load_config_props(repo_root: Path) -> Dict[str, str]:
    """Merge base config files into a single dotted-key -> value map. First file
    to define a key wins (properties before yaml, default doc before profiles)."""
    props: Dict[str, str] = {}
    for name in _BASE_CONFIG_NAMES:
        for f in sorted(repo_root.rglob(name)):
            if any(p in _SKIP_DIRS for p in f.parts) or not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            pairs: List[Tuple[str, str]] = []
            if f.suffix in (".yml", ".yaml"):
                try:
                    for doc in yaml.safe_load_all(text):
                        pairs += [(k, v) for k, v, _ in _flatten_yaml(doc)]
                except yaml.YAMLError:
                    continue
            else:
                pairs += [(k, v) for k, v, _ in _parse_properties(text)]
            for k, v in pairs:
                props.setdefault(k, v)
    return props


def _first(props: Dict[str, str], keys) -> str:
    for k in keys:
        if k in props:
            return _resolve_placeholder(props[k])
    return ""


def detect_base_paths(repo_root: Path) -> Dict[str, str]:
    """Return {framework: base_prefix} for frameworks whose base lives in config.

    Currently only Spring (MVC + WebFlux) is config-driven; the returned prefix
    is keyed under both "spring" and "jaxrs" because a JAX-RS resource served by
    a Spring Boot app sits behind the same context path. Frameworks whose base
    lives in code are not returned here.

    Returns {} when nothing meaningful is declared (root "/" is a no-op).
    """
    props = _load_config_props(Path(repo_root))
    if not props:
        return {}

    context = _first(props, _CONTEXT_KEYS)
    # mvc.servlet.path and webflux.base-path are mutually exclusive in practice;
    # take whichever is present as an additional inner prefix.
    inner = _first(props, _SERVLET_KEYS) or _first(props, _WEBFLUX_KEYS)

    base = _join_prefix(context, inner)
    if not base:
        return {}
    base = normalize_path(base)
    if base == "/":
        return {}
    return {"spring": base, "jaxrs": base}


def apply_base_paths(routes: list, base_paths: Dict[str, str]) -> list:
    """Prepend each route's framework base prefix to its path, in place.

    A route whose framework has no configured base is left untouched. Mutates and
    also returns `routes` for convenience.
    """
    if not base_paths:
        return routes
    for r in routes:
        prefix = base_paths.get(getattr(r, "framework", ""))
        if prefix and prefix != "/":
            r.path = normalize_path(prefix + "/" + r.path.lstrip("/"))
    return routes
