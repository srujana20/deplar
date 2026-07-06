"""Agent-loop tests using a stub Anthropic client (no API key required)."""
from dataclasses import dataclass, field
from typing import List

from deplar.agent import ImpactAgent, PlannerValidator, run_loop
from deplar.graph.symbol_store import SymbolStore
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.symbols import Symbol, SymbolIndex

# --- stub response objects mirroring the Anthropic SDK shape ---

@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict
    id: str = "tu_1"
    type: str = "tool_use"


@dataclass
class Resp:
    content: list
    stop_reason: str = "end_turn"


@dataclass
class StubMessages:
    scripted: List[Resp]
    seen: list = field(default_factory=list)

    def create(self, **kwargs):
        self.seen.append(kwargs)
        return self.scripted.pop(0)


@dataclass
class StubClient:
    scripted: List[Resp]

    def __post_init__(self):
        self.messages = StubMessages(self.scripted)


def _store(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    store.upsert_repo("payment", "/repos/payment", "2026-01-01T00:00:00Z")
    store.replace_dependencies([
        DependencyEdge("order", "payment", ["http"], 1.0, []),
    ])
    idx = SymbolIndex()
    idx.symbols.append(Symbol("payment", "p.py", "python", "function",
                              "charge", "charge", "charge(x)", "", 1, 5))
    store.replace_symbols("payment", idx)
    return store


# --- run_loop ---

def test_loop_returns_text_on_immediate_end_turn():
    client = StubClient([Resp([TextBlock("done")], "end_turn")])
    run = run_loop(client, "sys", "hi", tools=[], impls={})
    assert run.text == "done"
    assert run.iterations == 1
    assert run.tool_calls == []


def test_loop_executes_tool_then_finishes():
    calls = {}

    def my_tool(x):
        calls["x"] = x
        return {"ok": x}

    client = StubClient([
        Resp([ToolUseBlock("my_tool", {"x": 5})], "tool_use"),
        Resp([TextBlock("final answer")], "end_turn"),
    ])
    tools = [{"name": "my_tool", "input_schema": {"type": "object"}}]
    run = run_loop(client, "sys", "go", tools=tools, impls={"my_tool": my_tool})
    assert calls["x"] == 5
    assert run.text == "final answer"
    assert run.tool_calls == ["my_tool"]
    assert run.iterations == 2


def test_loop_handles_refusal():
    client = StubClient([Resp([], "refusal")])
    run = run_loop(client, "sys", "x", tools=[], impls={})
    assert run.refused is True


def test_loop_reports_tool_errors_without_crashing():
    def boom(**kw):
        raise ValueError("nope")

    client = StubClient([
        Resp([ToolUseBlock("boom", {})], "tool_use"),
        Resp([TextBlock("recovered")], "end_turn"),
    ])
    run = run_loop(client, "sys", "go",
                   tools=[{"name": "boom", "input_schema": {"type": "object"}}],
                   impls={"boom": boom})
    # the error is fed back as a tool_result(is_error=True); loop continues
    assert run.text == "recovered"
    tool_result_msg = client.messages.seen[1]["messages"][-1]["content"][0]
    assert tool_result_msg["is_error"] is True


# --- ImpactAgent ---

def test_impact_agent_queries_graph_and_returns_report(tmp_path):
    store = _store(tmp_path)
    client = StubClient([
        Resp([ToolUseBlock("get_dependents", {"repo": "payment"})], "tool_use"),
        Resp([TextBlock("## Directly affected\n- order")], "end_turn"),
    ])
    run = ImpactAgent(store, client=client).run("change charge() in payment")
    assert "Directly affected" in run.text
    assert run.tool_calls == ["get_dependents"]
    # the tool actually hit the store — result fed back mentions 'order'
    fed_back = client.messages.seen[1]["messages"][-1]["content"][0]["content"]
    assert "order" in fed_back


# --- PlannerValidator (dual-agent) ---

def test_planner_produces_plan(tmp_path):
    store = _store(tmp_path)
    client = StubClient([Resp([TextBlock("PLAN: edit payment then order")], "end_turn")])
    run = PlannerValidator(store, client=client).plan("change charge()")
    assert "PLAN" in run.text


def test_validator_runs_tests_over_workspace(tmp_path):
    store = _store(tmp_path)
    ws = tmp_path / "ws"
    (ws / "payment").mkdir(parents=True)
    client = StubClient([
        Resp([ToolUseBlock("run_tests", {})], "tool_use"),
        Resp([TextBlock("## Verdict\nAPPROVED")], "end_turn"),
    ])
    run = PlannerValidator(store, client=client).validate(
        "changed charge()", ws, test_cmd="true")
    assert "APPROVED" in run.text
    assert run.tool_calls == ["run_tests"]
    # run_tests actually executed the validator over the workspace
    fed_back = client.messages.seen[1]["messages"][-1]["content"][0]["content"]
    assert "payment" in fed_back
