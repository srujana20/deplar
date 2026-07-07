import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal

import tree_sitter_java as tsjava
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from deplar.scanner.endpoints import split_host_path
from deplar.scanner.walker import FileMap

PY_LANGUAGE = Language(tspython.language())
JAVA_LANGUAGE = Language(tsjava.language())
TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())


@dataclass
class NetworkCallEdge:
    source_file: Path
    call_type: Literal["http", "grpc", "kafka", "rabbitmq", "soap"]
    target: str
    confidence: float
    line_number: int
    raw: str = ""
    method: str = ""   # HTTP verb (GET/POST/...) — surface matching, if known
    path: str = ""     # request path split from the resolved target URL
    # provenance tier — see _score_target / config_scanner:
    #   "call-site" a literal/resolved URL at a recognized network call (highest)
    #   "config"    a URL from a config/properties file (high)
    #   "inferred"  a call site whose host we couldn't resolve (low)
    tier: str = "call-site"


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8")


# --- HTTP client vocabularies ---

PY_HTTP_CLIENTS = {
    "requests": ["get", "post", "put", "patch", "delete", "head", "options"],
    "httpx":    ["get", "post", "put", "patch", "delete", "head", "options"],
    "urllib":   ["urlopen", "urlretrieve"],
    "aiohttp":  ["get", "post", "put", "patch", "delete"],
}

# TypeScript / JavaScript HTTP clients
TS_HTTP_OBJECTS = {"axios", "got", "http", "https", "superagent", "ky"}
TS_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "request", "head"}
TS_HTTP_FUNCS = {"fetch", "axios", "got", "ky"}

GRPC_PATTERNS = ["insecure_channel", "secure_channel"]

# Namespace / schema URIs that appear as string literals in XML, SOAP, JAXB and
# SAX code but are NEVER real network targets. Dropped outright, at any tier.
_NAMESPACE_HOSTS = {
    "www.w3.org", "w3.org", "schemas.xmlsoap.org", "xmlsoap.org",
    "xmlns.example.com", "www.xml.org", "xml.org",
}
_NAMESPACE_PATH_MARKERS = (
    "apache.org/xml/features", "xml.org/sax/features", "xml.org/sax/properties",
    "w3.org/", "xmlsoap.org/",
)


def is_namespace_noise(url: str) -> bool:
    """True if `url` is an XML/SOAP namespace URI, not a real endpoint."""
    if not url:
        return False
    u = re.sub(r"^\$ENV:", "", url).lower()
    m = re.match(r"^[a-z]+://([^/]+)", u)
    host = m.group(1).split(":")[0] if m else ""
    if host in _NAMESPACE_HOSTS:
        return True
    return any(marker in u for marker in _NAMESPACE_PATH_MARKERS)

# client method name -> canonical HTTP verb (unmapped names => "" => ANY)
_HTTP_VERB = {v: v.upper() for v in
              ("get", "post", "put", "patch", "delete", "head", "options")}


def _score_target(target: str) -> tuple[str, float, str]:
    """Classify a (possibly variable-resolved) call-site target.

    Returns (normalized_target, confidence, tier). Because this is only ever
    called on the *argument of a recognized network call*, every result is a
    genuine dependency signal — a string literal that merely looks like a URL
    elsewhere in the file never reaches here.
    """
    if target.startswith("http://") or target.startswith("https://"):
        return target, 1.0, "call-site"

    # already resolved to an env-var reference (via a tracked variable)
    if target.startswith("$ENV:"):
        return target, 0.7, "call-site"

    if "os.getenv" in target or "os.environ" in target or "process.env" in target:
        match = re.search(r'getenv\(["\']([^"\']+)["\']', target)
        if not match:
            match = re.search(r'process\.env\.(\w+)', target)
        name = match.group(1) if match else target
        return f"$ENV:{name}", 0.7, "call-site"

    # a call site whose host we couldn't resolve (variable/template/concat)
    return target, 0.4, "inferred"


# --- Python ---

def _py_string_literal(node: Node, source: bytes) -> str | None:
    """Return the unquoted value of a plain Python string literal, else None."""
    if node.type != "string":
        return None
    text = _node_text(node, source)
    # f-strings / byte strings are not plain literals
    prefix = re.match(r'^([a-zA-Z]*)', text).group(1).lower()
    if "f" in prefix or "b" in prefix:
        return None
    return text[len(prefix):].strip("\"'")


def _resolve_py_target(node: Node, source: bytes, var_map: Dict[str, str]) -> str:
    """Resolve a call argument to a concrete target, substituting known vars."""
    text = _node_text(node, source)

    if node.type == "identifier":
        return var_map.get(text, text)

    if node.type == "string":
        prefix = re.match(r'^([a-zA-Z]*)', text).group(1)
        inner = text[len(prefix):].strip("\"'")
        # substitute {VAR} references from f-strings
        inner = re.sub(r'\{(\w+)\}', lambda m: var_map.get(m.group(1), m.group(0)), inner)
        return inner

    return text


def _py_env_ref(node: Node, source: bytes) -> str | None:
    """Return `$ENV:NAME` if the node is os.getenv("NAME") / os.environ[...]."""
    text = _node_text(node, source)
    if "getenv" in text or "environ" in text:
        m = (re.search(r'getenv\(\s*["\']([^"\']+)["\']', text)
             or re.search(r'environ\.get\(\s*["\']([^"\']+)["\']', text)
             or re.search(r'environ\[\s*["\']([^"\']+)["\']', text))
        if m:
            return f"$ENV:{m.group(1)}"
    return None


def _collect_py_vars(root: Node, source: bytes) -> Dict[str, str]:
    """Map variable names to string literals / env refs assigned to them."""
    var_map: Dict[str, str] = {}

    def walk(node: Node):
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left and right and left.type == "identifier":
                literal = _py_string_literal(right, source)
                if literal is None:
                    literal = _py_env_ref(right, source)
                if literal is not None:
                    var_map[_node_text(left, source)] = literal
        for child in node.children:
            walk(child)

    walk(root)
    return var_map


def detect_python_network_calls(path: Path) -> List[NetworkCallEdge]:
    source = path.read_bytes()
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source)
    edges: List[NetworkCallEdge] = []
    var_map = _collect_py_vars(tree.root_node, source)
    # gRPC is only credible when the grpc runtime is actually imported —
    # otherwise `insecure_channel` is just a method name that happens to match.
    grpc_imported = bool(re.search(rb'^\s*(import grpc\b|from grpc\b)', source, re.M))

    def first_arg(args_node: Node) -> Node | None:
        return args_node.named_children[0] if args_node.named_children else None

    def walk(node: Node):
        if node.type == "call":
            func_node = node.child_by_field_name("function")
            args_node = node.child_by_field_name("arguments")

            if func_node and args_node:
                func_text = _node_text(func_node, source)
                node_text = _node_text(node, source)

                # HTTP: requests.get(...), httpx.post(...)
                http_method = ""
                for client, methods in PY_HTTP_CLIENTS.items():
                    for method in methods:
                        if func_text == f"{client}.{method}":
                            http_method = method
                if http_method:
                    arg = first_arg(args_node)
                    target = _resolve_py_target(arg, source, var_map) if arg else ""
                    scored, confidence, tier = _score_target(target)
                    if not is_namespace_noise(scored):
                        _, req_path = split_host_path(scored)
                        edges.append(NetworkCallEdge(
                            source_file=path, call_type="http", target=scored,
                            confidence=confidence, line_number=node.start_point[0] + 1,
                            raw=node_text[:120],
                            method=_HTTP_VERB.get(http_method, ""), path=req_path,
                            tier=tier,
                        ))

                # Kafka: producer.send("topic", ...)
                if func_text.endswith(".send") and "producer" in node_text.lower():
                    arg = first_arg(args_node)
                    literal = _py_string_literal(arg, source) if arg else None
                    if literal:
                        edges.append(NetworkCallEdge(
                            source_file=path, call_type="kafka", target=literal,
                            confidence=1.0, line_number=node.start_point[0] + 1,
                            raw=node_text[:120],
                        ))

                # gRPC: grpc.insecure_channel(...) — only if grpc is imported
                if grpc_imported and any(
                    func_text.endswith("." + p) or func_text == "grpc." + p
                    for p in GRPC_PATTERNS
                ):
                    arg = first_arg(args_node)
                    literal = (_py_string_literal(arg, source) if arg else None) or "unknown"
                    edges.append(NetworkCallEdge(
                        source_file=path, call_type="grpc", target=literal,
                        confidence=0.9, line_number=node.start_point[0] + 1,
                        raw=node_text[:120],
                    ))

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges


# --- TypeScript / JavaScript ---

def _ts_string_value(node: Node, source: bytes) -> str:
    for child in node.children:
        if child.type == "string_fragment":
            return _node_text(child, source)
    return _node_text(node, source).strip("\"'`")


def _resolve_ts_target(node: Node, source: bytes, var_map: Dict[str, str]) -> str:
    if node.type == "string":
        return _ts_string_value(node, source)

    if node.type == "template_string":
        parts: List[str] = []
        for child in node.children:
            if child.type == "string_fragment":
                parts.append(_node_text(child, source))
            elif child.type == "template_substitution":
                inner = _node_text(child, source).strip("${} \t")
                parts.append(var_map.get(inner, "{" + inner + "}"))
        return "".join(parts)

    if node.type == "identifier":
        return var_map.get(_node_text(node, source), _node_text(node, source))

    return _node_text(node, source)


def _ts_env_ref(node: Node, source: bytes) -> str | None:
    """Return `$ENV:NAME` for `process.env.NAME` (with optional `?? default`)."""
    text = _node_text(node, source)
    m = re.search(r'process\.env\.(\w+)', text) or re.search(
        r'process\.env\[\s*["\'](\w+)["\']', text)
    return f"$ENV:{m.group(1)}" if m else None


def _collect_ts_vars(root: Node, source: bytes) -> Dict[str, str]:
    var_map: Dict[str, str] = {}

    def walk(node: Node):
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name and value and name.type == "identifier":
                if value.type == "string":
                    var_map[_node_text(name, source)] = _ts_string_value(value, source)
                else:
                    env = _ts_env_ref(value, source)
                    if env:
                        var_map[_node_text(name, source)] = env
        for child in node.children:
            walk(child)

    walk(root)
    return var_map


def _resolve_baseurl_expr(expr: str, var_map: Dict[str, str]) -> str:
    expr = expr.strip()
    if expr[:1] in ("'", '"', "`"):
        return expr.strip("'\"`")
    m = re.search(r'process\.env\.(\w+)', expr)
    if m:
        return f"$ENV:{m.group(1)}"
    return var_map.get(expr, expr)


def _collect_ts_axios_instances(
    root: Node, source: bytes, var_map: Dict[str, str]
) -> Dict[str, str]:
    """Map an axios-instance variable to its baseURL: `const api = axios.create({baseURL})`."""
    inst: Dict[str, str] = {}

    def walk(node: Node):
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if (name and value and name.type == "identifier"
                    and value.type == "call_expression"):
                f = value.child_by_field_name("function")
                if f and _node_text(f, source) in ("axios.create", "axios.default.create"):
                    a = value.child_by_field_name("arguments")
                    base = ""
                    if a:
                        m = re.search(r'baseURL\s*:\s*([^,}]+)', _node_text(a, source))
                        if m:
                            base = _resolve_baseurl_expr(m.group(1), var_map)
                    inst[_node_text(name, source)] = base
        for c in node.children:
            walk(c)

    walk(root)
    return inst


def detect_ts_network_calls(path: Path, tsx: bool = False) -> List[NetworkCallEdge]:
    source = path.read_bytes()
    parser = Parser(TSX_LANGUAGE if tsx else TS_LANGUAGE)
    tree = parser.parse(source)
    edges: List[NetworkCallEdge] = []
    var_map = _collect_ts_vars(tree.root_node, source)
    instances = _collect_ts_axios_instances(tree.root_node, source, var_map)

    def first_arg(args_node: Node) -> Node | None:
        return args_node.named_children[0] if args_node.named_children else None

    def walk(node: Node):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if func and args:
                is_http = False
                http_method = ""  # canonical verb, "" => ANY
                base = ""
                if func.type == "identifier":
                    is_http = _node_text(func, source) in TS_HTTP_FUNCS
                    # fetch(url, { method: 'POST' })
                    if is_http and len(args.named_children) > 1:
                        m = re.search(r'method\s*:\s*["\'](\w+)["\']',
                                      _node_text(args.named_children[1], source))
                        if m:
                            http_method = m.group(1).upper()
                elif func.type == "member_expression":
                    obj = func.child_by_field_name("object")
                    prop = func.child_by_field_name("property")
                    if obj and prop and obj.type == "identifier":
                        obj_text = _node_text(obj, source)
                        prop_text = _node_text(prop, source)
                        # direct axios.get(...) OR a wrapped axios.create() instance
                        if ((obj_text in TS_HTTP_OBJECTS or obj_text in instances)
                                and prop_text in TS_HTTP_METHODS):
                            is_http = True
                            base = instances.get(obj_text, "")
                        if prop_text in _HTTP_VERB:
                            http_method = _HTTP_VERB[prop_text]

                if is_http:
                    arg = first_arg(args)
                    target = _resolve_ts_target(arg, source, var_map) if arg else ""
                    # prepend the instance baseURL when the call used a bare path
                    if base and (target.startswith("/") or not target):
                        target = base.rstrip("/") + target
                    scored, confidence, tier = _score_target(target)
                    if not is_namespace_noise(scored):
                        _, req_path = split_host_path(scored)
                        edges.append(NetworkCallEdge(
                            source_file=path, call_type="http", target=scored,
                            confidence=confidence, line_number=node.start_point[0] + 1,
                            raw=_node_text(node, source)[:120],
                            method=http_method, path=req_path, tier=tier,
                        ))

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges


# --- Java (RestTemplate / WebClient / OkHttp) ---

# RestTemplate method -> verb. The *ForObject/*ForEntity names are specific
# enough to match on their own; bare put/delete need a rest-ish receiver.
JAVA_REST_VERBS = {
    "getForObject": "GET", "getForEntity": "GET",
    "postForObject": "POST", "postForEntity": "POST", "postForLocation": "POST",
    "put": "PUT", "delete": "DELETE", "patchForObject": "PATCH",
}
_JAVA_AMBIGUOUS = {"put", "delete"}          # only count with a client-ish receiver
_WEBCLIENT_VERBS = {"get", "post", "put", "patch", "delete"}
# Spring-WS / JAX-WS SOAP client entry points that take the endpoint URI first.
_SOAP_METHODS = {"marshalSendAndReceive", "sendSourceAndReceive"}


def _java_string_literal(node: Node, source: bytes) -> str | None:
    if node.type == "string_literal":
        for c in node.children:
            if c.type == "string_fragment":
                return _node_text(c, source)
        return _node_text(node, source).strip('"')
    return None


def _collect_java_vars(root: Node, source: bytes) -> Dict[str, str]:
    """Map Java variable/field names to their string-literal initializers."""
    var_map: Dict[str, str] = {}

    def walk(node: Node):
        if node.type in ("local_variable_declaration", "field_declaration"):
            for child in node.children:
                if child.type == "variable_declarator":
                    name = child.child_by_field_name("name")
                    value = child.child_by_field_name("value")
                    if name and value:
                        lit = _java_string_literal(value, source)
                        if lit is not None:
                            var_map[_node_text(name, source)] = lit
        for c in node.children:
            walk(c)

    walk(root)
    return var_map


def _resolve_java_target(node: Node, source: bytes, var_map: Dict[str, str]) -> str:
    lit = _java_string_literal(node, source)
    if lit is not None:
        return lit
    if node.type == "identifier":
        return var_map.get(_node_text(node, source), _node_text(node, source))
    if node.type == "binary_expression":
        # concatenation: resolve each operand; unknown identifiers that sit
        # after a path separator become a `{}` template hole.
        parts: List[str] = []

        def visit(n: Node):
            if (n.type == "binary_expression"
                    and _node_text(n.child_by_field_name("operator"), source) == "+"):
                visit(n.child_by_field_name("left"))
                visit(n.child_by_field_name("right"))
                return
            s = _java_string_literal(n, source)
            if s is not None:
                parts.append(s)
            elif n.type == "identifier":
                parts.append(var_map.get(_node_text(n, source), "{}"))
            else:
                parts.append("{}")

        visit(node)
        return "".join(parts)
    # String.format("%s/x", ...) etc — best-effort: keep the literal template
    lits = re.findall(r'"([^"]*)"', _node_text(node, source))
    return "".join(lits) if lits else _node_text(node, source)


def detect_java_network_calls(path: Path) -> List[NetworkCallEdge]:
    source = path.read_bytes()
    parser = Parser(JAVA_LANGUAGE)
    tree = parser.parse(source)
    edges: List[NetworkCallEdge] = []
    var_map = _collect_java_vars(tree.root_node, source)

    def args_of(inv: Node) -> List[Node]:
        a = inv.child_by_field_name("arguments")
        return a.named_children if a else []

    def add_http(method: str, target_node: Node | None, line: int, raw: str,
                 explicit_path: str | None = None):
        target = (_resolve_java_target(target_node, source, var_map)
                  if target_node is not None else "")
        scored, confidence, tier = _score_target(target)
        if is_namespace_noise(scored):
            return
        _, req_path = split_host_path(scored)
        if explicit_path is not None:
            req_path = explicit_path
        edges.append(NetworkCallEdge(
            source_file=path, call_type="http", target=scored,
            confidence=confidence, line_number=line, raw=raw[:120],
            method=method, path=req_path, tier=tier,
        ))

    def walk(node: Node):
        if node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            obj_node = node.child_by_field_name("object")
            name = _node_text(name_node, source) if name_node else ""
            obj_text = _node_text(obj_node, source) if obj_node else ""
            args = args_of(node)
            line = node.start_point[0] + 1
            raw = _node_text(node, source)

            # RestTemplate.exchange(url, HttpMethod.POST, ...)
            if name == "exchange" and args:
                verb = "ANY"
                if len(args) >= 2:
                    m = re.search(r'HttpMethod\.(\w+)', _node_text(args[1], source))
                    if m:
                        verb = m.group(1).upper()
                add_http(verb, args[0], line, raw)

            # RestTemplate getForObject/postForEntity/put/delete(url, ...)
            elif name in JAVA_REST_VERBS and args:
                receiver_ok = (name not in _JAVA_AMBIGUOUS
                               or re.search(r'rest|template|client', obj_text, re.I))
                if receiver_ok:
                    add_http(JAVA_REST_VERBS[name], args[0], line, raw)

            # OkHttp: new Request.Builder().url("...") — require a builder chain,
            # so a bare `.url("http://www.w3.org/...")` in XML code is ignored.
            elif name == "url" and args and re.search(r'builder|request', obj_text, re.I):
                add_http("ANY", args[0], line, raw)

            # HttpURLConnection: new URL("...").openConnection()
            elif name == "openConnection" and obj_node is not None:
                url_arg = None
                if obj_node.type == "object_creation_expression":
                    oargs = args_of(obj_node)
                    tnode = obj_node.child_by_field_name("type")
                    ttext = _node_text(tnode, source) if tnode else ""
                    if oargs and re.search(r'\bURL$', ttext):  # URL / java.net.URL
                        url_arg = oargs[0]
                elif obj_node.type == "identifier":
                    url_arg = obj_node  # a URL variable
                if url_arg is not None:
                    add_http("ANY", url_arg, line, raw)

            # SOAP: webServiceTemplate.marshalSendAndReceive(uri, request)
            elif name in _SOAP_METHODS and args:
                first = args[0]
                # only when the first arg is a URI string/var, not a payload
                target = _resolve_java_target(first, source, var_map)
                if _java_string_literal(first, source) or first.type == "identifier":
                    scored, confidence, tier = _score_target(target)
                    host, req_path = split_host_path(scored)
                    if (host or scored.startswith("$ENV:")) and not is_namespace_noise(scored):
                        edges.append(NetworkCallEdge(
                            source_file=path, call_type="soap", target=scored,
                            confidence=confidence, line_number=line, raw=raw[:120],
                            method="POST", path=req_path, tier=tier,
                        ))

            # WebClient: webClient.get().uri("/path")
            elif name == "uri" and args:
                verb = "ANY"
                if (obj_node and obj_node.type == "method_invocation"):
                    inner = obj_node.child_by_field_name("name")
                    v = _node_text(inner, source) if inner else ""
                    if v in _WEBCLIENT_VERBS:
                        verb = v.upper()
                lit = _java_string_literal(args[0], source)
                if lit is not None:
                    add_http(verb, None, line, raw, explicit_path=lit)

        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return edges


# --- Main detector class ---

class NetworkDetector:
    def detect(self, file_map: FileMap) -> List[NetworkCallEdge]:
        edges: List[NetworkCallEdge] = []
        for path in file_map.files.get("python", []):
            try:
                edges.extend(detect_python_network_calls(path))
            except Exception as e:
                print(f"[warn] network detection failed for {path}: {e}")
        for path in file_map.files.get("java", []):
            try:
                edges.extend(detect_java_network_calls(path))
            except Exception as e:
                print(f"[warn] network detection failed for {path}: {e}")
        for path in file_map.files.get("typescript", []):
            try:
                edges.extend(detect_ts_network_calls(path, tsx=path.suffix == ".tsx"))
            except Exception as e:
                print(f"[warn] network detection failed for {path}: {e}")
        for path in file_map.files.get("javascript", []):
            try:
                edges.extend(detect_ts_network_calls(path, tsx=path.suffix == ".jsx"))
            except Exception as e:
                print(f"[warn] network detection failed for {path}: {e}")
        return edges
