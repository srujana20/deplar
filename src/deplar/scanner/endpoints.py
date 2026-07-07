"""Endpoint normalization — the join key for HTTP surface matching.

A consumer writes `GET ${base}/v1/orders/${id}` and a provider declares
`@GetMapping("/v1/orders/{id}")`. To match them we must fold both to the same
canonical key. Path parameters take many syntactic forms across frameworks:

    /orders/{id}     Spring / OpenAPI / FastAPI
    /orders/:id      Express / NestJS
    /orders/${id}    JS template literal (consumer-side, pre-resolution)
    /orders/123      a concrete id baked into a consumer call

All of these collapse to `/orders/{}`. The result is a stable string like
`GET /v1/orders/{}` used as a dict key on both sides.
"""
import re

# a path segment that is a bound parameter in *some* framework's syntax
_PARAM_SEGMENT = re.compile(
    r"""^(?:
        \{[^}]*\}            # {id}  or  {id:.*}  (Spring/FastAPI/OpenAPI)
      | :[A-Za-z_][\w-]*     # :id                (Express/Nest)
      | \$\{[^}]*\}          # ${id}              (JS template literal)
      | <[^>]*>              # <int:id>           (Flask converter syntax)
      | \d+                  # 123                (concrete id in a call)
      | [0-9a-fA-F-]{16,}    # a uuid / long hex token
    )$""",
    re.VERBOSE,
)

# a leftover template hole embedded in a segment, e.g. "user-{id}" or "v${n}"
_EMBEDDED_HOLE = re.compile(r"\$?\{[^}]*\}")


def normalize_path(path: str) -> str:
    """Fold a route/URL path to its canonical template form.

    >>> normalize_path("/v1/orders/{id}")
    '/v1/orders/{}'
    >>> normalize_path("/v1/orders/:orderId/items")
    '/v1/orders/{}/items'
    >>> normalize_path("orders/123")
    '/orders/{}'
    """
    if not path:
        return "/"

    # drop scheme+host if a full URL slipped through, and any query/fragment
    path = re.sub(r"^[a-z]+://[^/]+", "", path, flags=re.IGNORECASE)
    path = path.split("?", 1)[0].split("#", 1)[0]

    segments = [s for s in path.split("/") if s != ""]
    out = []
    for seg in segments:
        if _PARAM_SEGMENT.match(seg):
            out.append("{}")
        elif _EMBEDDED_HOLE.search(seg):
            # a segment that is partly templated ("user-${id}") is unreliable
            # to match precisely; treat the whole segment as a parameter.
            out.append("{}")
        else:
            out.append(seg.lower())

    return "/" + "/".join(out) if out else "/"


def endpoint_key(method: str, path: str) -> str:
    """Canonical (method, path) key, e.g. `POST /v1/orders/{}`.

    An empty/unknown method folds to `ANY`, which the matcher treats as a
    wildcard so a consumer whose verb we couldn't determine still matches.
    """
    m = (method or "ANY").strip().upper()
    return f"{m} {normalize_path(path)}"


def split_host_path(url: str) -> tuple[str, str]:
    """Split a (possibly resolved) target URL into (host, path).

    >>> split_host_path("https://payments.internal/v1/charge")
    ('payments.internal', '/v1/charge')
    >>> split_host_path("/v1/charge")
    ('', '/v1/charge')
    """
    u = url or ""
    # env-var host: $ENV:NAME[/path] — the env name stands in for the host
    m = re.match(r"^\$ENV:([^/]+)(/.*)?$", u)
    if m:
        return m.group(1), (m.group(2) or "/")
    m = re.match(r"^[a-z]+://([^/]+)(/.*)?$", u, re.IGNORECASE)
    if m:
        return m.group(1), (m.group(2) or "/")
    # template-hole host: {var}/path or ${var}/path — host unknown, keep the path
    m = re.match(r"^\$?\{[^}]*\}(/.*)?$", u)
    if m:
        return "", (m.group(1) or "/")
    # no scheme: if it starts with "/" it's a bare path; otherwise treat the
    # first segment as a host only when it looks host-ish (contains a dot/colon)
    if u.startswith("/"):
        return "", u
    head = u.split("/", 1)
    if "." in head[0] or ":" in head[0]:
        return head[0], ("/" + head[1] if len(head) > 1 else "/")
    return "", u if u.startswith("/") else "/" + u
