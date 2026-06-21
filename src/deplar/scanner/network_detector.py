from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Literal
import re
from deplar.scanner.walker import FileMap

import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Node

PY_LANGUAGE = Language(tspython.language())


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


# --- Python HTTP detection ---

HTTP_CLIENTS = {
    "requests": ["get", "post", "put", "patch", "delete"],
    "httpx":    ["get", "post", "put", "patch", "delete"],
    "urllib":   ["urlopen", "urlretrieve"],
}

KAFKA_PRODUCERS = ["KafkaProducer", "producer"]
GRPC_PATTERNS  = ["insecure_channel", "secure_channel"]


def _score_target(target: str) -> tuple[str, float]:
    """
    Given a raw target string from source code, return
    (normalized_target, confidence_score).
    """
    # Literal URL
    if target.startswith("http://") or target.startswith("https://"):
        return target, 1.0

    # Environment variable
    print(f"Scoring target: {target}")
    if "os.getenv" in target or "os.environ" in target or "process.env" in target:
        # try to extract the var name
        match = re.search(r'getenv\(["\']([^"\']+)["\']', target)
        name = match.group(1) if match else target
        return f"$ENV:{name}", 0.7

    # f-string or concatenation with a variable
    if "{" in target or "+" in target:
        return target, 0.4

    # variable reference
    return target, 0.4


def detect_python_network_calls(path: Path) -> List[NetworkCallEdge]:
    source = path.read_bytes()
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source)
    edges = []

    def walk(node: Node):
        if node.type == "call":
            func_node = node.child_by_field_name("function")
            args_node = node.child_by_field_name("arguments")

            if func_node and args_node:
                func_text = _node_text(func_node, source)
                args_text = _node_text(args_node, source)

                # HTTP client calls: requests.get(...), httpx.post(...)
                for client, methods in HTTP_CLIENTS.items():
                    for method in methods:
                        if func_text == f"{client}.{method}":
                            # extract first argument as target
                            first_arg = ""
                            for child in args_node.children:
                                if child.type in ("string", "concatenated_string",
                                                  "f_string", "call", "identifier"):
                                    first_arg = _node_text(child, source).strip('"\'')
                                    break
                            target, confidence = _score_target(first_arg)
                            edges.append(NetworkCallEdge(
                                source_file=path,
                                call_type="http",
                                target=target,
                                confidence=confidence,
                                line_number=node.start_point[0] + 1,
                                raw=_node_text(node, source)[:120],
                            ))

                # Kafka: producer.send("topic", ...)
                if func_text.endswith(".send") and any(
                    k in _node_text(node, source) for k in ["Producer", "producer"]
                ):
                    first_arg = ""
                    for child in args_node.children:
                        if child.type == "string":
                            first_arg = _node_text(child, source).strip('"\'')
                            break
                    if first_arg:
                        edges.append(NetworkCallEdge(
                            source_file=path,
                            call_type="kafka",
                            target=first_arg,
                            confidence=1.0,
                            line_number=node.start_point[0] + 1,
                            raw=_node_text(node, source)[:120],
                        ))

                # gRPC: grpc.insecure_channel(...)
                if any(p in func_text for p in GRPC_PATTERNS):
                    first_arg = ""
                    for child in args_node.children:
                        if child.type == "string":
                            first_arg = _node_text(child, source).strip('"\'')
                            break
                    edges.append(NetworkCallEdge(
                        source_file=path,
                        call_type="grpc",
                        target=first_arg or "unknown",
                        confidence=0.9,
                        line_number=node.start_point[0] + 1,
                        raw=_node_text(node, source)[:120],
                    ))

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges


# --- Main detector class ---




class NetworkDetector:
    def detect(self, file_map: FileMap) -> List[NetworkCallEdge]:
        edges = []
        for path in file_map.files.get("python", []):
            try:
                edges.extend(detect_python_network_calls(path))
            except Exception as e:
                print(f"[warn] network detection failed for {path}: {e}")
        return edges