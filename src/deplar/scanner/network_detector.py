import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal

import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from deplar.scanner.walker import FileMap

PY_LANGUAGE = Language(tspython.language())
TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())


@dataclass
class NetworkCallEdge:
    source_file: Path
    call_type: Literal["http", "grpc", "kafka", "rabbitmq"]
    target: str
    confidence: float
    line_number: int
    raw: str = ""


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


def _score_target(target: str) -> tuple[str, float]:
    """
    Given a (possibly variable-resolved) target string from source code,
    return (normalized_target, confidence_score).
    """
    if target.startswith("http://") or target.startswith("https://"):
        return target, 1.0

    if "os.getenv" in target or "os.environ" in target or "process.env" in target:
        match = re.search(r'getenv\(["\']([^"\']+)["\']', target)
        if not match:
            match = re.search(r'process\.env\.(\w+)', target)
        name = match.group(1) if match else target
        return f"$ENV:{name}", 0.7

    # unresolved f-string / template / concatenation with a variable
    if "{" in target or "+" in target or "$" in target:
        return target, 0.4

    return target, 0.4


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


def _collect_py_vars(root: Node, source: bytes) -> Dict[str, str]:
    """Map variable names to string literals assigned to them (scope-flat)."""
    var_map: Dict[str, str] = {}

    def walk(node: Node):
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left and right and left.type == "identifier":
                literal = _py_string_literal(right, source)
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
                is_http = any(
                    func_text == f"{client}.{method}"
                    for client, methods in PY_HTTP_CLIENTS.items()
                    for method in methods
                )
                if is_http:
                    arg = first_arg(args_node)
                    target = _resolve_py_target(arg, source, var_map) if arg else ""
                    scored, confidence = _score_target(target)
                    edges.append(NetworkCallEdge(
                        source_file=path, call_type="http", target=scored,
                        confidence=confidence, line_number=node.start_point[0] + 1,
                        raw=node_text[:120],
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

                # gRPC: grpc.insecure_channel(...)
                if any(p in func_text for p in GRPC_PATTERNS):
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


def _collect_ts_vars(root: Node, source: bytes) -> Dict[str, str]:
    var_map: Dict[str, str] = {}

    def walk(node: Node):
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name and value and name.type == "identifier" and value.type == "string":
                var_map[_node_text(name, source)] = _ts_string_value(value, source)
        for child in node.children:
            walk(child)

    walk(root)
    return var_map


def detect_ts_network_calls(path: Path, tsx: bool = False) -> List[NetworkCallEdge]:
    source = path.read_bytes()
    parser = Parser(TSX_LANGUAGE if tsx else TS_LANGUAGE)
    tree = parser.parse(source)
    edges: List[NetworkCallEdge] = []
    var_map = _collect_ts_vars(tree.root_node, source)

    def first_arg(args_node: Node) -> Node | None:
        return args_node.named_children[0] if args_node.named_children else None

    def walk(node: Node):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if func and args:
                is_http = False
                if func.type == "identifier":
                    is_http = _node_text(func, source) in TS_HTTP_FUNCS
                elif func.type == "member_expression":
                    obj = func.child_by_field_name("object")
                    prop = func.child_by_field_name("property")
                    if obj and prop and obj.type == "identifier":
                        is_http = (
                            _node_text(obj, source) in TS_HTTP_OBJECTS
                            and _node_text(prop, source) in TS_HTTP_METHODS
                        )

                if is_http:
                    arg = first_arg(args)
                    target = _resolve_ts_target(arg, source, var_map) if arg else ""
                    scored, confidence = _score_target(target)
                    edges.append(NetworkCallEdge(
                        source_file=path, call_type="http", target=scored,
                        confidence=confidence, line_number=node.start_point[0] + 1,
                        raw=_node_text(node, source)[:120],
                    ))

        for child in node.children:
            walk(child)

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
