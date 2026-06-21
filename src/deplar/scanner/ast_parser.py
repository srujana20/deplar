from dataclasses import dataclass, field
from pathlib import Path
from platform import node
from typing import List
from xml.dom import Node
from xml.etree.ElementTree import indent
import tree_sitter_python as tspython
import tree_sitter_java as tsjava
from tree_sitter import Language, Parser
from deplar.scanner.walker import FileMap

PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

JAVA_LANGUAGE = Language(tsjava.language())
parser_java = Parser(JAVA_LANGUAGE)

@dataclass
class ImportEdge:
    source_file: Path
    imported_module: str
    imported_names: List[str]
    line_number: int
    raw: str

@dataclass
class FeignClientEdge:
    source_file: Path
    client_name: str
    url_pattern: str
    declared_in: str
    line_number: int

def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8")


def parse_python_imports(path: Path) -> List[ImportEdge]:
    source = path.read_bytes()
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source)
    edges = []
    return edges

    def walk(node: Node):
        if node.type == "import_statement":
            # e.g. import os, import os as operating_system
            names = []
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import"):
                    names.append(_node_text(child, source))
            if names:
                edges.append(ImportEdge(
                    source_file=path,
                    imported_module=names[0],
                    imported_names=names,
                    line_number=node.start_point[0] + 1,
                    raw=_node_text(node, source),
                ))

        elif node.type == "import_from_statement":
            # e.g. from pathlib import Path, from . import utils
            module = ""
            imported = []
            for child in node.children:
                if child.type in ("dotted_name", "relative_import"):
                    module = _node_text(child, source)
                elif child.type == "import_prefix":
                    module = _node_text(child, source)
            # collect imported names
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import") and _node_text(child, source) != module:
                    imported.append(_node_text(child, source))
            edges.append(ImportEdge(
                source_file=path,
                imported_module=module,
                imported_names=imported,
                line_number=node.start_point[0] + 1,
                raw=_node_text(node, source),
            ))

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges

def parse_java_imports(path: Path) -> List[ImportEdge]:
    source = path.read_bytes()
    parser = Parser(JAVA_LANGUAGE)
    tree = parser.parse(source)
    edges = []

    def walk(node: Node):
        if node.type == "import_declaration":
            for child in node.children:
                if child.type == "scoped_identifier":
                    mod = _node_text(child, source)
                    edges.append(ImportEdge(
                        source_file=path,
                        imported_module=mod,
                        imported_names=[mod.split(".")[-1]],
                        line_number=node.start_point[0] + 1,
                        raw=_node_text(node, source),
                    ))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges


def parse_feign_clients(path: Path) -> List[FeignClientEdge]:
    source = path.read_bytes()
    parser = Parser(JAVA_LANGUAGE)
    tree = parser.parse(source)
    edges = []

    def walk(node: Node, current_class: str = ""):
        if node.type == "class_declaration":
            for child in node.children:
                if child.type == "identifier":
                    current_class = _node_text(child, source)

        if node.type == "marker_annotation" or node.type == "annotation":
            name_node = node.child_by_field_name("name")
            if name_node and _node_text(name_node, source) == "FeignClient":
                client_name = ""
                url_pattern = ""
                args = node.child_by_field_name("arguments")
                if args:
                    text = _node_text(args, source)
                    # crude but effective extraction from annotation args
                    import re
                    name_match = re.search(r'name\s*=\s*"([^"]+)"', text)
                    url_match = re.search(r'url\s*=\s*"([^"]+)"', text)
                    if name_match:
                        client_name = name_match.group(1)
                    if url_match:
                        url_pattern = url_match.group(1)
                edges.append(FeignClientEdge(
                    source_file=path,
                    client_name=client_name,
                    url_pattern=url_pattern,
                    declared_in=current_class,
                    line_number=node.start_point[0] + 1,
                ))

        for child in node.children:
            walk(child, current_class)
    
    walk(tree.root_node)
    print_tree(tree.root_node, source)
    return edges

def print_tree(node, source, indent=0):
    print(" " * indent + f"{node.type}: {source[node.start_byte:node.end_byte][:40]}")
    for child in node.children:
        print_tree(child, source, indent + 2)

class ASTParser:
    def parse(self, file_map: FileMap) -> tuple[List[ImportEdge], List[FeignClientEdge]]:
        import_edges: List[ImportEdge] = []
        feign_edges: List[FeignClientEdge] = []

        for path in file_map.files.get("python", []):
            try:
                import_edges.extend(parse_python_imports(path))
            except Exception as e:
                print(f"[warn] failed to parse {path}: {e}")

        for path in file_map.files.get("java", []):
            try:
                import_edges.extend(parse_java_imports(path))
                feign_edges.extend(parse_feign_clients(path))
            except Exception as e:
                print(f"[warn] failed to parse {path}: {e}")

        return import_edges, feign_edges

