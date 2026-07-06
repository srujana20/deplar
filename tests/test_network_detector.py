from pathlib import Path

from deplar.scanner.network_detector import detect_python_network_calls

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo" / "src"


def test_detects_literal_http_call():
    edges = detect_python_network_calls(FIXTURE / "payments_client.py")
    http_edges = [e for e in edges if e.call_type == "http"]
    assert len(http_edges) >= 1

def test_literal_url_confidence_is_high():
    edges = detect_python_network_calls(FIXTURE / "payments_client.py")
    literal = [e for e in edges if "payments-service.internal" in e.target]
    assert literal[0].confidence == 1.0

def test_env_var_confidence_is_medium():
    edges = detect_python_network_calls(FIXTURE / "payments_client.py")
    print(f"Detected edges: {edges}")
    env_edges = [e for e in edges if e.confidence == 0.4]
    print(f"Edges with env var confidence: {env_edges}")
    assert len(env_edges) >= 1

def test_detects_kafka_topic():
    edges = detect_python_network_calls(FIXTURE / "events.py")
    kafka = [e for e in edges if e.call_type == "kafka"]
    topics = [e.target for e in kafka]
    assert "orders.created" in topics
    assert "orders.failed" in topics

def test_kafka_confidence_is_high():
    edges = detect_python_network_calls(FIXTURE / "events.py")
    kafka = [e for e in edges if e.call_type == "kafka"]
    assert all(e.confidence == 1.0 for e in kafka)