![CI](https://github.com/YOUR_USERNAME/deplar/actions/workflows/ci.yml/badge.svg)
[![PyPI version](https://badge.fury.io/py/deplar.svg)](https://pypi.org/project/deplar/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
# deplar

**Dependency radar for multi-repo codebases.**

deplar scans your repos and builds a precise, confidence-scored dependency map — detecting not just imports but actual HTTP calls, the REST/SOAP routes each service *provides*, FeignClient annotations, gRPC stubs, and Kafka topics. It matches a consumer's call to the exact endpoint it hits on the provider, so it tells AI agents exactly what they'll break — down to the individual API contract — before they touch anything.

```
$ deplar scan ./order-service

  deplar scanning order-service

  → walking files...
  → parsing imports...
  → detecting network calls...
  → resolving dependencies...

  Files scanned        42
  Dependencies found    7
  Services detected     5

  ✓ High confidence    5
  ⚠ Needs review       2

  Saved to deps.json
```

```
$ deplar map deps.json

order-service
  → payments-service   (feign, http)  100%
  → orders.created     (kafka)        100%
  → orders.failed      (kafka)        100%
  → user-service       (import)        60%
  → paymentsclient     (import)        60%
```

---

## The problem

In multi-repo organizations, dependency management is often zero to null. What exists lives in architecture diagrams that are years out of date, or in someone's head.

When an AI agent works in one repo, it has no idea what other services call it, what Kafka topics it owns, or what will break downstream if it changes an API contract.

deplar fixes that.

---

## How it works

deplar detects both what a service **consumes** and what it **provides**, then joins them across the corpus.

**Consumes** (outbound calls):

| Signal | Example | Confidence |
|---|---|---|
| Literal HTTP call | `requests.post("https://payments-svc/v1/charge")` | 100% |
| FeignClient (Java) | `@FeignClient(name="payments")` + `@GetMapping("/v1/users/{id}")` | 95% |
| RestTemplate / WebClient (Java) | `restTemplate.postForEntity(url, ...)`, `webClient.get().uri("/x")` | 100% |
| Wrapped axios (JS/TS) | `const api = axios.create({baseURL}); api.get('/x')` | 100% |
| Env var HTTP call | `httpx.get(os.getenv("USER_SVC_URL") + "/v1/x")` | 70% |
| SOAP call | `webServiceTemplate.marshalSendAndReceive(uri, req)` | 100% |
| Kafka producer | `producer.send("orders.created", ...)` | 100% |
| Import statement | `from payments_client import Client` | 60% |

**Provides** (inbound HTTP routes it serves):

| Framework | Example |
|---|---|
| Spring MVC (Java) | `@RestController` + `@GetMapping("/v1/users/{id}")` |
| JAX-RS (Java) | `@Path("/v1/accounts")` + `@GET @Path("/{id}")` |
| Express / Fastify (JS/TS) | `router.get('/v1/users/:id', handler)` (+ `app.use('/api', router)` prefixes) |
| NestJS (JS/TS) | `@Controller('users')` + `@Get(':id')` |
| FastAPI / Flask (Py) | `@app.get("/v1/users/{id}")`, `@app.route("/x", methods=["POST"])` |

Each dependency gets a confidence score. High confidence means a concrete runtime signal was found. Lower confidence means the tool found a likely signal that needs a human eye.

---

## API surface matching — endpoint-level impact

deplar doesn't stop at "A depends on B." It folds every consumer call and every
provider route to a canonical key (`GET /v1/orders/{}` — path params collapsed),
then matches them. The result: it knows A calls B's *specific* `POST /v1/charge`,
not just that A calls B.

Two artifacts come out of a `scan-org`:

- **`org-deps.json`** — the cross-repo edges, each carrying the matched `surfaces[]`.
- **`org-interfaces.json`** — per repo, everything it `provides` and `consumes`.

This makes impact analysis surgical. Scope a change to one endpoint and deplar
flags **only** the consumers that call it — changing an endpoint nobody calls
ripples to nobody:

```bash
deplar impact user-service --endpoint "GET /v1/users/{id}"
#   Directly affected: payment-service — calls `GET /v1/users/{}`

deplar impact user-service --endpoint "DELETE /v1/users/{id}"
#   Directly affected: none detected
```

---

## Identity resolution — no manual name mapping

The hard part of a cross-repo map is turning a reference like
`https://order-mgmt-svc.internal/v1` into "that's the `orders` repo" — when the
folder name rarely matches the deployed service name.

deplar solves this without hardcoding or manual mapping in two moves:

1. **Provider identity catalog.** Every repo advertises what it *is* — deplar
   extracts these identities from `package.json` / `pyproject.toml` / `go.mod`
   names, `spring.application.name`, Kubernetes `Service` names, OpenAPI
   `servers`/`info.title`, and the git remote. `deplar identities <repo>` shows a
   repo's catalog.
2. **Corpus reconciliation.** Each consumer reference is matched against that
   catalog across *all* repos (exact → plural-stem → conservative token-subset,
   confidence-scored). It's iterative: after scanning a new repo, run
   `deplar reconcile` and references that were left dangling when earlier repos
   were scanned get bound to the newly-declared identity — self-references are
   dropped and duplicates merged.

```bash
deplar scan ./checkout            # references order-mgmt-svc.internal (dangling)
deplar scan ./orders              # declares identity "order-mgmt-svc" via package.json
deplar reconcile                  # binds checkout -> orders automatically
```

For the rare case the matcher can't infer (e.g. an internal codename), pin it
once — the override has full confidence and **survives re-scans**:

```bash
deplar alias orders neptune --reconcile   # "orders is also known as neptune"
deplar alias orders neptune --remove      # unpin
```

---

## Installation

```bash
pip install deplar
```

Requires Python 3.11+.

---

## Usage

### Scan a single repo

```bash
deplar scan ./my-service
deplar scan ./my-service --output deps.json --verbose
```

### Print the dependency map

```bash
deplar map deps.json
```

### Scan an entire org

```bash
deplar scan-org ./services --config deplar.yaml
```

`deplar.yaml` format:

```yaml
repos:
  - path: ./services/orders
    name: orders-service
  - path: ./services/payments
    name: payments-service
  - url: https://github.com/myorg/auth-service
```

### Generate a CLAUDE.md

Auto-generate an agent context file for a repo so AI agents know its dependencies before acting:

```bash
deplar claude-md ./my-service --graph deps.json
```

This writes a `CLAUDE.md` to the repo root:

```markdown
## Dependency context (auto-generated by deplar)

### This service calls
- payments-service (HTTP /v1/charge) [confidence: high]
- user-service (gRPC GetUser) [confidence: high]

### This service is called by
- checkout-service
- mobile-gateway

### Events emitted
- orders.created (Kafka)
- orders.failed (Kafka)

### High-risk change zones
- src/api/v1/orders.py (3 known callers)
- src/models/order.py (referenced by 2 downstream services)
```

Claude Code reads `CLAUDE.md` automatically before acting in a codebase.

### Query the knowledge graph

Every scan populates a SQLite store (`deplar.db`) holding the dependency graph
plus a symbol index (classes, methods, function signatures, and call sites with
line numbers). Query it directly:

```bash
deplar query --dependents payments-service   # who calls it
deplar query --calls order-service            # what it calls
deplar query --blast payments-service         # transitive blast radius
deplar query --symbols getUser                # find a symbol + signature + line
deplar query --callers charge                 # every call site of a symbol
```

### Build a coordinated multi-repo workspace

Given a repo, `deplar workspace` resolves every repo affected by changing it
(the target plus its dependents) and checks each one out as a **git worktree**
into a single directory — so an agent can make coordinated, parallel edits
across all of them at once:

```bash
deplar scan-org ./services --config deplar.yaml   # record repo paths + graph
deplar workspace payments-service --out ./ws --branch feature/new-charge-api

#   deplar workspace for payments-service — 3 repo(s): payments-service, order-service, checkout-service
#     ✓ payments-service → ./ws/payments-service   created: branch feature/new-charge-api
#     ✓ order-service     → ./ws/order-service      created: branch feature/new-charge-api
#     ✓ checkout-service  → ./ws/checkout-service   created: branch feature/new-charge-api
```

Flags: `--transitive` pulls the full blast radius (not just direct dependents),
`--with-dependencies` also includes repos the target itself calls, and
`--remove` tears the workspace back down.

### Impact report — know before you touch

Before editing, generate a structured report of everything a change would ripple
into — dependents, transitive blast radius, emitted events, and cross-repo call
sites of a specific symbol:

```bash
deplar impact payments-service --symbol charge          # markdown
deplar impact payments-service --json -o impact.json    # machine-readable
```

### Agentic loops (optional — needs an API key)

With `pip install deplar[agent]` and `ANTHROPIC_API_KEY` set, two LLM-driven
agents drive the graph tools themselves:

```bash
# Impact-analysis agent: describe a change in English, it queries the graph and
# writes an impact report before any code is touched.
deplar impact-agent "change the getUser signature in payments-service"

# Planner + validator (dual-agent): planner drafts a coordinated change plan
# across affected repos; validator re-queries the graph for coverage and runs
# every repo's tests in the workspace, then returns a verdict.
deplar validate-agent "new charge API" --workspace ./workspace
```

Both are thin loops over the same read-only graph tools the MCP server exposes;
the deterministic `deplar impact` / `deplar verify-workspace` commands cover the
same ground without an API key.

### Validate a workspace after edits

Re-run every repo's tests across a coordinated workspace (auto-detects pytest,
npm, mvn/gradle, go test, or use `--test-cmd`):

```bash
deplar verify-workspace ./workspace
```

### Agent memory

Persist patterns, conventions, and gotchas about a repo so they survive across
sessions and flow into its CLAUDE.md / SKILL.md:

```bash
deplar remember payments-service "charge() is not idempotent — pass an idempotency key" --kind gotcha
deplar recall payments-service
```

### Skillhub — reusable skills per repo

Generate a Claude skill (SKILL.md) that distills how to work in a repo, versioned
by a hash of the graph snapshot it came from:

```bash
deplar skill payments-service -o SKILL.md      # one repo
deplar skillhub --out ./skillhub               # all repos + a browsable portal
```

`deplar skillhub` writes `<repo>/SKILL.md` per repo, an `index.json` registry,
and a self-contained `index.html` portal (search by repo or language).

### Visual UI

An interactive, force-directed dependency map — click a node to see its
dependents, dependencies, blast radius, and API surface. Two ways to open it:

```bash
deplar ui --open                 # self-contained HTML file (offline, no server)
deplar serve                     # local server with live data + a Reconcile button
```

The static file (`deplar ui`) embeds the data and has no external dependencies —
double-click to open anywhere. `deplar serve` (stdlib only, no extra installs)
serves the same page against the live store and adds a one-click **Reconcile**
action. Real repos and unresolved/external targets are colored distinctly;
search, a confidence-threshold slider, and a "hide external" toggle keep large
graphs readable.

### MCP server

Expose the knowledge graph to agents over MCP:

```bash
deplar mcp --db deplar.db
```

Tools: `get_dependencies`, `get_dependents`, `blast_radius`, `search_symbols`,
`get_callers`, `list_repos`, `affected_repos`, `impact_report`, `remember`,
`recall`, `list_skills`, `get_skill`. Register it in Claude Code with:

```json
{ "mcpServers": { "deplar": { "command": "deplar", "args": ["mcp", "--db", "/abs/path/deplar.db", "--skills", "/abs/path/skillhub"] } } }
```

---

## Language support

| Language | Import parsing | HTTP detection | Annotations |
|---|---|---|---|
| Python | ✅ | ✅ (requests, httpx, urllib, aiohttp) | — |
| Java | ✅ | ✅ | ✅ @FeignClient |
| TypeScript | ✅ | ✅ (axios, fetch, got, ky) | — |
| JavaScript | ✅ | ✅ (axios, fetch, got, ky) | — |
| Go | 🔜 | 🔜 | — |

Python HTTP detection follows variable assignments across a scope, so
`URL = "https://payments-svc"; requests.post(f"{URL}/charge")` resolves to a
high-confidence literal.

---

## The vision: agents that know before they act

Right now when you ask Claude Code to fix a bug, it scans the entire codebase hunting for context.

With deplar's MCP server running, you can give it exactly the right methods and line numbers upfront. But the real unlock is multi-repo awareness.

Most real-world bugs don't live in one repo. You change an API contract in service A. The break shows up in service B and service C. The fix needs to happen in all three — coordinated, in the right order.

With deplar:

1. Agent queries the MCP server: *which services call this method? what line numbers? what Kafka topics are downstream?*
2. Agent checks out all dependent repos into a single workspace
3. Agent makes coordinated changes across all of them in one session — with full knowledge of what it's touching and why

**Instead of:** "here's the whole repo, figure it out"

**deplar enables:** "the issue is in `OrderService.java:47`, it's called by `checkout-service` at line 23 and `mobile-gateway` at line 89 — fix all three"

---

## Roadmap

- [x] File walker with .gitignore support
- [x] AST import parser (Python, Java, TypeScript)
- [x] @FeignClient annotation parser
- [x] Network call detector (HTTP, Kafka, gRPC)
- [x] Confidence-scored dependency resolver
- [x] Dependency graph with blast radius analysis
- [x] CLI: `scan`, `map`, `diff`
- [x] CLAUDE.md generator
- [x] Multi-repo org scan
- [x] Automatic identity resolution (provider catalog + corpus reconciliation)
- [x] TypeScript HTTP client detection (axios, fetch, got, ky)
- [x] Python variable-assignment tracking for URL resolution
- [x] Per-repo knowledge graph (classes, methods, signatures, call sites, line numbers)
- [x] SQLite symbol/graph store + `deplar query`
- [x] MCP server — expose knowledge graph to agents
- [x] Multi-repo git-worktree checkout (`deplar workspace`)
- [x] Impact reports (`deplar impact`) + LLM impact agent (`deplar impact-agent`)
- [x] Symbol-aware CLAUDE.md v2 (signatures, line numbers, cross-repo call sites, learned patterns)
- [x] Agent memory layer (`deplar remember` / `recall`, persisted in the store)
- [x] Cross-repo workspace validator (`deplar verify-workspace`) + planner/validator agent (`deplar validate-agent`)
- [x] SKILL.md generator + skillhub portal (`deplar skill` / `deplar skillhub`)
- [x] Interactive graph UI (`deplar ui` static file / `deplar serve` live)
- [ ] Go support
- [ ] Code embeddings + hybrid search
- [ ] Skillhub web portal with RAG-powered search (currently a static portal)

---

## Project structure

```
deplar/
  src/deplar/
    cli.py              # typer CLI — scan, scan-org, map, claude-md, query,
                        #   impact, workspace, verify-workspace, remember, recall,
                        #   skill, skillhub, mcp
    mcp_server.py       # FastMCP server exposing the knowledge graph
    agent.py            # LLM tool-use loops: impact agent + planner/validator
    ui.py               # interactive graph UI (static file + stdlib server)
    worktree.py         # multi-repo git-worktree checkout
    impact.py           # structured impact reports (blast radius, call sites)
    validator.py        # cross-repo workspace test runner
    skill.py            # SKILL.md generator + skillhub portal
    scanner/
      walker.py         # repo file walker with .gitignore support
      ast_parser.py     # tree-sitter import + annotation parser (py/java/ts/js)
      network_detector.py  # HTTP, Kafka, gRPC detection + variable tracking
      symbols.py        # symbol-level extraction (classes, methods, call sites)
      identity.py       # provider identity extraction (the catalog)
      reconciler.py     # match references against the catalog across the corpus
      resolver.py       # merge + deduplicate signals, score confidence
      org_scanner.py    # multi-repo scan + identity reconciliation
    graph/
      store.py          # networkx dependency graph, blast radius, export
      symbol_store.py   # SQLite store: symbols, calls, dependency edges
    output/
      claude_md.py      # CLAUDE.md generator
  tests/
    fixtures/           # sample microservices for integration tests
  schemas/
    deps.schema.json    # JSON Schema for the deps.json manifest
```

---

## Contributing

```bash
git clone https://github.com/YOUR_USERNAME/deplar.git
cd deplar
pip install -e ".[dev]"
pytest
```

PRs welcome. If you run deplar on your codebase and find something surprising, open an issue — real findings shape the roadmap.

---

## Why I'm building this

This started as a learning project to transition from full-stack to AI engineering. The problem is real — dependency blindness in multi-repo orgs is something I hit daily. Building deplar in public as I learn: AST parsing, knowledge graphs, RAG, MCP, embeddings, agentic workflows.

Following the build: [LinkedIn](https://linkedin.com/in/YOUR_PROFILE)

---

## License

MIT