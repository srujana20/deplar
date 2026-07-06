from dataclasses import dataclass
from pathlib import Path
from typing import List

import tree_sitter_java as tsjava
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from deplar.scanner.walker import FileMap

PY_LANGUAGE = Language(tspython.language())
JAVA_LANGUAGE = Language(tsjava.language())
TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())


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
    edges: List[ImportEdge] = []

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
            module_node = node.child_by_field_name("module_name")
            if module_node:
                module = _node_text(module_node, source)
            else:
                # relative imports (from . import x) expose no module_name field
                for child in node.children:
                    if child.type in ("dotted_name", "relative_import"):
                        module = _node_text(child, source)
                        break
            # collect imported names (children after the module_name node)
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import"):
                    text = _node_text(child, source)
                    if text != module:
                        imported.append(text)
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
    edges: List[ImportEdge] = []

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


def _ts_string_value(node: Node, source: bytes) -> str:
    """Extract the text of a TS `string` node without its surrounding quotes."""
    for child in node.children:
        if child.type == "string_fragment":
            return _node_text(child, source)
    return _node_text(node, source).strip("\"'`")


def parse_typescript_imports(path: Path, tsx: bool = False) -> List[ImportEdge]:
    source = path.read_bytes()
    parser = Parser(TSX_LANGUAGE if tsx else TS_LANGUAGE)
    tree = parser.parse(source)
    edges: List[ImportEdge] = []

    def _imported_names(clause: Node) -> List[str]:
        names: List[str] = []

        def collect(n: Node):
            if n.type in ("identifier", "property_identifier"):
                names.append(_node_text(n, source))
            for c in n.children:
                collect(c)

        collect(clause)
        return names

    def walk(node: Node):
        # import ... from 'module'   /   import 'module'
        if node.type == "import_statement":
            module = ""
            names: List[str] = []
            for child in node.children:
                if child.type == "string":
                    module = _ts_string_value(child, source)
                elif child.type == "import_clause":
                    names = _imported_names(child)
            if module:
                edges.append(ImportEdge(
                    source_file=path,
                    imported_module=module,
                    imported_names=names or [module.split("/")[-1]],
                    line_number=node.start_point[0] + 1,
                    raw=_node_text(node, source),
                ))

        # const x = require('module')
        elif node.type == "call_expression":
            func = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if func and args and _node_text(func, source) == "require":
                for child in args.children:
                    if child.type == "string":
                        module = _ts_string_value(child, source)
                        edges.append(ImportEdge(
                            source_file=path,
                            imported_module=module,
                            imported_names=[module.split("/")[-1]],
                            line_number=node.start_point[0] + 1,
                            raw=_node_text(node, source),
                        ))
                        break

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges


def parse_feign_clients(path: Path) -> List[FeignClientEdge]:
    import re

    source = path.read_bytes()
    parser = Parser(JAVA_LANGUAGE)
    tree = parser.parse(source)
    edges: List[FeignClientEdge] = []

    def walk(node: Node, current_class: str = ""):
        if node.type == "class_declaration":
            for child in node.children:
                if child.type == "identifier":
                    current_class = _node_text(child, source)

        if node.type in ("marker_annotation", "annotation"):
            name_node = node.child_by_field_name("name")
            if name_node and _node_text(name_node, source) == "FeignClient":
                client_name = ""
                url_pattern = ""
                args = node.child_by_field_name("arguments")
                if args:
                    text = _node_text(args, source)
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
    return edges


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

        for path in file_map.files.get("typescript", []):
            try:
                import_edges.extend(
                    parse_typescript_imports(path, tsx=path.suffix == ".tsx")
                )
            except Exception as e:
                print(f"[warn] failed to parse {path}: {e}")

        for path in file_map.files.get("javascript", []):
            try:
                import_edges.extend(
                    parse_typescript_imports(path, tsx=path.suffix == ".jsx")
                )
            except Exception as e:
                print(f"[warn] failed to parse {path}: {e}")

        return import_edges, feign_edges
