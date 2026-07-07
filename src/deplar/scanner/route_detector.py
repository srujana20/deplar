"""Inbound HTTP route detection — what a repo *provides*.

This is the half of the model deplar never had: until now a repo was known only
by its name, never by the surface it serves. Here we walk the AST and extract
the routes each service exposes, so a consumer's outbound call can be matched to
the specific endpoint it hits (see surface_matcher.py).

Frameworks covered in this prototype:
  TS/JS  - Express / Fastify  (app.get('/x', handler))
         - NestJS             (@Controller('p') + @Get('x'))
  Java   - Spring MVC         (@RestController + @GetMapping / @RequestMapping)
  Python - FastAPI / Flask    (@app.get('/x') / @app.route('/x', methods=[...]))
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import tree_sitter_java as tsjava
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from deplar.scanner.endpoints import normalize_path
from deplar.scanner.walker import FileMap

PY_LANGUAGE = Language(tspython.language())
JAVA_LANGUAGE = Language(tsjava.language())
TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())

HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "all"}

# server-ish receiver names that make `x.get('/path', ...)` a route, not a client
_SERVER_OBJECTS = {"app", "router", "server", "fastify", "api", "route",
                   "routes", "express"}


@dataclass
class RouteEdge:
    source_file: Path
    method: str          # GET, POST, ... or ANY
    path: str            # normalized template, e.g. /v1/orders/{}
    framework: str       # express | nest | spring | fastapi | flask
    line_number: int
    raw: str = ""


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8")


def _string_literal(node: Node, source: bytes) -> Optional[str]:
    """Unquoted value of a plain string/template literal (no interpolation)."""
    t = node.type
    if t == "string":
        for c in node.children:
            if c.type == "string_fragment":
                return _node_text(c, source)
        return _node_text(node, source).strip("\"'`")
    if t == "template_string":
        # only accept templates with no ${...} holes as literal paths
        if any(c.type == "template_substitution" for c in node.children):
            return None
        for c in node.children:
            if c.type == "string_fragment":
                return _node_text(c, source)
        return _node_text(node, source).strip("`")
    return None


def _join(prefix: str, sub: str) -> str:
    a = (prefix or "").rstrip("/")
    b = (sub or "")
    if b and not b.startswith("/"):
        b = "/" + b
    return (a + b) or "/"


# --- TypeScript / JavaScript ---

def detect_ts_routes(path: Path, tsx: bool = False) -> List[RouteEdge]:
    source = path.read_bytes()
    parser = Parser(TSX_LANGUAGE if tsx else TS_LANGUAGE)
    tree = parser.parse(source)
    edges: List[RouteEdge] = []

    def is_function_arg(node: Node) -> bool:
        return node.type in ("arrow_function", "function_expression",
                             "function", "identifier", "member_expression")

    # Express mounts: `app.use('/api', ordersRouter)` serves that router's
    # routes under /api. Collect {routerVar: '/api'} (single-file scope).
    mounts: dict[str, str] = {}

    def collect_mounts(node: Node):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if (func and args and func.type == "member_expression"
                    and _node_text(func.child_by_field_name("property"), source) == "use"):
                named = args.named_children
                if len(named) >= 2:
                    prefix = _string_literal(named[0], source)
                    router = named[1]
                    if prefix and prefix.startswith("/") and router.type == "identifier":
                        mounts[_node_text(router, source)] = prefix
        for c in node.children:
            collect_mounts(c)

    def handle_express_call(node: Node):
        # <obj>.<verb>('/path', ...handler)
        func = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if not func or not args or func.type != "member_expression":
            return
        obj = func.child_by_field_name("object")
        prop = func.child_by_field_name("property")
        if not obj or not prop:
            return
        verb = _node_text(prop, source).lower()
        if verb not in HTTP_VERBS:
            return
        named = args.named_children
        if not named:
            return
        path_str = _string_literal(named[0], source)
        if path_str is None or not path_str.startswith("/"):
            return
        # disambiguate a route from an HTTP client call: a route has a handler
        # function, or the receiver is a known server object.
        obj_name = _node_text(obj, source).split(".")[-1]
        if obj_name.lower() not in _SERVER_OBJECTS and obj_name not in mounts:
            has_handler = any(is_function_arg(a) for a in named[1:])
            if not has_handler:
                return
        full = _join(mounts.get(obj_name, ""), path_str)
        edges.append(RouteEdge(
            source_file=path, method=verb.upper() if verb != "all" else "ANY",
            path=normalize_path(full), framework="express",
            line_number=node.start_point[0] + 1,
            raw=_node_text(node, source)[:120],
        ))

    # NestJS: @Controller('prefix') on class, @Get('sub') on methods
    def nest_decorator_value(dec: Node) -> tuple[Optional[str], Optional[str]]:
        """Return (decorator_name, first_string_arg) for a decorator node."""
        call = None
        for c in dec.children:
            if c.type == "call_expression":
                call = c
        if call is None:
            # bare @Get with no args
            ident = next((c for c in dec.children
                          if c.type in ("identifier", "member_expression")), None)
            return (_node_text(ident, source) if ident else None), None
        func = call.child_by_field_name("function")
        args = call.child_by_field_name("arguments")
        name = _node_text(func, source) if func else None
        arg = None
        if args and args.named_children:
            arg = _string_literal(args.named_children[0], source)
        return name, arg

    def handle_nest_class(cls: Node):
        prefix = ""
        # class-level @Controller('prefix')
        for c in cls.children:
            if c.type == "decorator":
                name, arg = nest_decorator_value(c)
                if name and name.split(".")[0] == "Controller":
                    prefix = arg or ""
        body = cls.child_by_field_name("body")
        if body is None:
            return
        # decorators are siblings that precede the method_definition they annotate
        pending: List[Node] = []
        for member in body.children:
            if member.type == "decorator":
                pending.append(member)
                continue
            if member.type != "method_definition":
                pending = []
                continue
            for dec in pending:
                name, arg = nest_decorator_value(dec)
                if not name:
                    continue
                verb = name.split(".")[0].lower()
                if verb not in HTTP_VERBS:
                    continue
                full = _join(prefix, arg or "")
                edges.append(RouteEdge(
                    source_file=path,
                    method=verb.upper() if verb != "all" else "ANY",
                    path=normalize_path(full), framework="nest",
                    line_number=member.start_point[0] + 1,
                    raw=_node_text(dec, source)[:120],
                ))
            pending = []

    def walk(node: Node):
        if node.type == "call_expression":
            handle_express_call(node)
        if node.type == "class_declaration":
            has_controller = any(
                c.type == "decorator" and "Controller" in _node_text(c, source)
                for c in node.children
            )
            if has_controller:
                handle_nest_class(node)
        for c in node.children:
            walk(c)

    collect_mounts(tree.root_node)
    walk(tree.root_node)
    return edges


# --- Java (Spring MVC) ---

_SPRING_METHOD_ANNOS = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}

# JAX-RS (javax/jakarta.ws.rs): the verb is its own marker annotation, the
# path comes from a separate @Path annotation.
_JAXRS_VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


def _anno_path_value(args_text: str) -> str:
    m = re.search(r'"([^"]+)"', args_text)
    return m.group(1) if m else ""


def _spring_anno_path(args_text: str) -> str:
    """Pull the path out of a mapping annotation's argument text."""
    # @GetMapping("/x")  or  @RequestMapping(path = "/x")  or  value = "/x"
    m = re.search(r'(?:value|path)\s*=\s*"([^"]+)"', args_text)
    if m:
        return m.group(1)
    m = re.search(r'"([^"]+)"', args_text)
    return m.group(1) if m else ""


def _spring_anno_method(args_text: str) -> str:
    m = re.search(r'RequestMethod\.(\w+)', args_text)
    return m.group(1).upper() if m else "ANY"


def _java_annotations(decl: Node):
    """Annotations of a Java declaration — direct children *and* those nested
    under a `modifiers` node (tree-sitter-java wraps them there)."""
    for c in decl.children:
        if c.type in ("marker_annotation", "annotation"):
            yield c
        elif c.type == "modifiers":
            for m in c.children:
                if m.type in ("marker_annotation", "annotation"):
                    yield m


def detect_java_routes(path: Path) -> List[RouteEdge]:
    source = path.read_bytes()
    parser = Parser(JAVA_LANGUAGE)
    tree = parser.parse(source)
    edges: List[RouteEdge] = []

    def class_is_controller_and_prefix(cls: Node) -> tuple[bool, str]:
        """Returns (is_http_endpoint_class, path_prefix). Covers Spring MVC
        (@RestController/@RequestMapping) and JAX-RS (@Path)."""
        is_ctrl = False
        prefix = ""
        for c in _java_annotations(cls):
            name_node = c.child_by_field_name("name")
            if not name_node:
                continue
            aname = _node_text(name_node, source)
            args = c.child_by_field_name("arguments")
            args_text = _node_text(args, source) if args else ""
            if aname in ("RestController", "Controller"):
                is_ctrl = True
            elif aname == "RequestMapping":
                if args_text:
                    prefix = _spring_anno_path(args_text)
            elif aname == "Path":            # JAX-RS resource class
                is_ctrl = True
                prefix = _anno_path_value(args_text) or prefix
        return is_ctrl, prefix

    def handle_method(method_node: Node, prefix: str):
        jaxrs_verb = ""
        jaxrs_sub = ""
        for c in _java_annotations(method_node):
            name_node = c.child_by_field_name("name")
            if not name_node:
                continue
            aname = _node_text(name_node, source)
            args = c.child_by_field_name("arguments")
            args_text = _node_text(args, source) if args else ""
            if aname in _SPRING_METHOD_ANNOS:
                edges.append(RouteEdge(
                    source_file=path, method=_SPRING_METHOD_ANNOS[aname],
                    path=normalize_path(_join(prefix, _spring_anno_path(args_text))),
                    framework="spring", line_number=c.start_point[0] + 1,
                    raw=_node_text(c, source)[:120],
                ))
            elif aname == "RequestMapping":
                edges.append(RouteEdge(
                    source_file=path, method=_spring_anno_method(args_text),
                    path=normalize_path(_join(prefix, _spring_anno_path(args_text))),
                    framework="spring", line_number=c.start_point[0] + 1,
                    raw=_node_text(c, source)[:120],
                ))
            elif aname in _JAXRS_VERBS:      # @GET / @POST marker
                jaxrs_verb = aname
            elif aname == "Path":            # method-level @Path("/sub")
                jaxrs_sub = _anno_path_value(args_text)
        if jaxrs_verb:
            edges.append(RouteEdge(
                source_file=path, method=jaxrs_verb,
                path=normalize_path(_join(prefix, jaxrs_sub)),
                framework="jaxrs", line_number=method_node.start_point[0] + 1,
                raw=_node_text(method_node, source)[:80],
            ))

    def walk(node: Node):
        if node.type == "class_declaration":
            is_ctrl, prefix = class_is_controller_and_prefix(node)
            if is_ctrl:
                body = node.child_by_field_name("body")
                if body:
                    for member in body.children:
                        if member.type == "method_declaration":
                            handle_method(member, prefix)
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return edges


# --- Python (FastAPI / Flask) ---

def detect_python_routes(path: Path) -> List[RouteEdge]:
    source = path.read_bytes()
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source)
    edges: List[RouteEdge] = []

    def py_string(node: Node) -> Optional[str]:
        if node.type != "string":
            return None
        text = _node_text(node, source)
        prefix = re.match(r'^([a-zA-Z]*)', text).group(1).lower()
        if "f" in prefix or "b" in prefix:
            return None
        return text[len(prefix):].strip("\"'")

    def handle_decorator(dec: Node):
        call = next((c for c in dec.children if c.type == "call"), None)
        if call is None:
            return
        func = call.child_by_field_name("function")
        args = call.child_by_field_name("arguments")
        if not func or func.type != "attribute":
            return
        attr = func.child_by_field_name("attribute")
        verb = _node_text(attr, source).lower() if attr else ""
        if not args or not args.named_children:
            return
        path_str = py_string(args.named_children[0])
        if path_str is None:
            return

        if verb in HTTP_VERBS:  # FastAPI: @app.get("/x")
            edges.append(RouteEdge(
                source_file=path, method=verb.upper(),
                path=normalize_path(path_str), framework="fastapi",
                line_number=dec.start_point[0] + 1,
                raw=_node_text(dec, source)[:120],
            ))
        elif verb == "route":  # Flask: @app.route("/x", methods=["POST"])
            args_text = _node_text(args, source)
            methods = re.findall(r'["\'](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)["\']',
                                 args_text, re.IGNORECASE)
            for m in (methods or ["GET"]):
                edges.append(RouteEdge(
                    source_file=path, method=m.upper(),
                    path=normalize_path(path_str), framework="flask",
                    line_number=dec.start_point[0] + 1,
                    raw=_node_text(dec, source)[:120],
                ))

    def walk(node: Node):
        if node.type == "decorator":
            handle_decorator(node)
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return edges


class RouteDetector:
    def detect(self, file_map: FileMap) -> List[RouteEdge]:
        edges: List[RouteEdge] = []

        def run(fn, path, *a):
            try:
                edges.extend(fn(path, *a))
            except Exception as e:
                print(f"[warn] route detection failed for {path}: {e}")

        for p in file_map.files.get("python", []):
            run(detect_python_routes, p)
        for p in file_map.files.get("java", []):
            run(detect_java_routes, p)
        for p in file_map.files.get("typescript", []):
            run(detect_ts_routes, p, p.suffix == ".tsx")
        for p in file_map.files.get("javascript", []):
            run(detect_ts_routes, p, p.suffix == ".jsx")
        return edges
