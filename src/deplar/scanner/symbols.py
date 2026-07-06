"""Symbol-level AST extraction.

Extracts classes, functions/methods (with signatures + line ranges) and call
sites (with the enclosing caller + line number) for Python, Java and
TypeScript/JavaScript. This is the structured layer that powers `search_symbols`
and `get_callers` in the MCP server.
"""
from dataclasses import dataclass, field
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
class Symbol:
    repo: str
    file: str            # path relative to repo root
    language: str
    kind: str            # class | interface | function | method
    name: str
    qualified_name: str  # e.g. Order.create
    signature: str       # e.g. create(self, x)
    parent: str          # enclosing class/interface name, or ""
    start_line: int
    end_line: int


@dataclass
class CallSite:
    repo: str
    file: str
    caller: str          # qualified name of the enclosing symbol, or "<module>"
    callee: str          # dotted call target, e.g. pay.charge
    callee_name: str      # final segment, e.g. charge
    line: int


@dataclass
class SymbolIndex:
    symbols: List[Symbol] = field(default_factory=list)
    calls: List[CallSite] = field(default_factory=list)

    def extend(self, other: "SymbolIndex"):
        self.symbols.extend(other.symbols)
        self.calls.extend(other.calls)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _field(node: Node, name: str, source: bytes) -> str:
    child = node.child_by_field_name(name)
    return _text(child, source) if child else ""


# --- Python ---

def _extract_python(path: Path, rel: str, repo: str, source: bytes) -> SymbolIndex:
    tree = Parser(PY_LANGUAGE).parse(source)
    idx = SymbolIndex()

    def walk(node: Node, parent_cls: str, caller: str):
        node_caller = caller
        if node.type == "class_definition":
            name = _field(node, "name", source)
            idx.symbols.append(Symbol(
                repo, rel, "python", "class", name, name, name, "",
                node.start_point[0] + 1, node.end_point[0] + 1,
            ))
            for child in node.children:
                walk(child, name, caller)
            return

        if node.type == "function_definition":
            name = _field(node, "name", source)
            params = _field(node, "parameters", source)
            qualified = f"{parent_cls}.{name}" if parent_cls else name
            kind = "method" if parent_cls else "function"
            idx.symbols.append(Symbol(
                repo, rel, "python", kind, name, qualified,
                f"{name}{params}", parent_cls,
                node.start_point[0] + 1, node.end_point[0] + 1,
            ))
            # methods nested inside a method still belong to the class scope
            for child in node.children:
                walk(child, parent_cls, qualified)
            return

        if node.type == "call":
            func = node.child_by_field_name("function")
            if func is not None:
                dotted = _text(func, source)
                name = dotted.split(".")[-1].split("(")[-1]
                idx.calls.append(CallSite(
                    repo, rel, node_caller or "<module>", dotted, name,
                    node.start_point[0] + 1,
                ))

        for child in node.children:
            walk(child, parent_cls, node_caller)

    walk(tree.root_node, "", "<module>")
    return idx


# --- Java ---

def _extract_java(path: Path, rel: str, repo: str, source: bytes) -> SymbolIndex:
    tree = Parser(JAVA_LANGUAGE).parse(source)
    idx = SymbolIndex()

    def walk(node: Node, parent_cls: str, caller: str):
        if node.type in ("class_declaration", "interface_declaration"):
            name = _field(node, "name", source)
            kind = "class" if node.type == "class_declaration" else "interface"
            idx.symbols.append(Symbol(
                repo, rel, "java", kind, name, name, name, "",
                node.start_point[0] + 1, node.end_point[0] + 1,
            ))
            for child in node.children:
                walk(child, name, caller)
            return

        if node.type == "method_declaration":
            name = _field(node, "name", source)
            params = _field(node, "parameters", source)
            ret = _field(node, "type", source)
            qualified = f"{parent_cls}.{name}" if parent_cls else name
            sig = f"{name}{params}".strip()
            if ret:
                sig = f"{ret} {sig}"
            idx.symbols.append(Symbol(
                repo, rel, "java", "method", name, qualified, sig, parent_cls,
                node.start_point[0] + 1, node.end_point[0] + 1,
            ))
            for child in node.children:
                walk(child, parent_cls, qualified)
            return

        if node.type == "method_invocation":
            name = _field(node, "name", source)
            obj = node.child_by_field_name("object")
            dotted = f"{_text(obj, source)}.{name}" if obj else name
            idx.calls.append(CallSite(
                repo, rel, caller or "<class>", dotted, name,
                node.start_point[0] + 1,
            ))

        for child in node.children:
            walk(child, parent_cls, caller)

    walk(tree.root_node, "", "<class>")
    return idx


# --- TypeScript / JavaScript ---

def _extract_typescript(path: Path, rel: str, repo: str, source: bytes,
                        tsx: bool) -> SymbolIndex:
    tree = Parser(TSX_LANGUAGE if tsx else TS_LANGUAGE).parse(source)
    idx = SymbolIndex()
    lang = "typescript"

    def add_fn(node: Node, name: str, parent_cls: str, kind: str) -> str:
        params = _field(node, "parameters", source)
        qualified = f"{parent_cls}.{name}" if parent_cls else name
        idx.symbols.append(Symbol(
            repo, rel, lang, kind, name, qualified, f"{name}{params}", parent_cls,
            node.start_point[0] + 1, node.end_point[0] + 1,
        ))
        return qualified

    def walk(node: Node, parent_cls: str, caller: str):
        if node.type == "class_declaration":
            name = _field(node, "name", source)
            idx.symbols.append(Symbol(
                repo, rel, lang, "class", name, name, name, "",
                node.start_point[0] + 1, node.end_point[0] + 1,
            ))
            for child in node.children:
                walk(child, name, caller)
            return

        if node.type in ("function_declaration", "generator_function_declaration"):
            qualified = add_fn(node, _field(node, "name", source), parent_cls, "function")
            for child in node.children:
                walk(child, parent_cls, qualified)
            return

        if node.type == "method_definition":
            qualified = add_fn(node, _field(node, "name", source), parent_cls, "method")
            for child in node.children:
                walk(child, parent_cls, qualified)
            return

        # const foo = (..) => {..}  /  const foo = function(){..}
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value and value.type in ("arrow_function", "function_expression",
                                        "function"):
                qualified = add_fn(value, _field(node, "name", source), parent_cls,
                                   "function")
                for child in value.children:
                    walk(child, parent_cls, qualified)
                return

        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None:
                dotted = _text(func, source)
                name = dotted.split(".")[-1]
                idx.calls.append(CallSite(
                    repo, rel, caller or "<module>", dotted, name,
                    node.start_point[0] + 1,
                ))

        for child in node.children:
            walk(child, parent_cls, caller)

    walk(tree.root_node, "", "<module>")
    return idx


class SymbolExtractor:
    def extract(self, file_map: FileMap, repo: str) -> SymbolIndex:
        index = SymbolIndex()
        root = file_map.root

        def rel(path: Path) -> str:
            try:
                return str(path.relative_to(root))
            except ValueError:
                return path.name

        handlers = {
            "python": lambda p, s: _extract_python(p, rel(p), repo, s),
            "java": lambda p, s: _extract_java(p, rel(p), repo, s),
            "typescript": lambda p, s: _extract_typescript(
                p, rel(p), repo, s, tsx=p.suffix == ".tsx"),
            "javascript": lambda p, s: _extract_typescript(
                p, rel(p), repo, s, tsx=p.suffix == ".jsx"),
        }

        for lang, handler in handlers.items():
            for path in file_map.files.get(lang, []):
                try:
                    index.extend(handler(path, path.read_bytes()))
                except Exception as e:
                    print(f"[warn] symbol extraction failed for {path}: {e}")

        return index
