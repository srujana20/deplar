import json

from deplar.scanner.identity import extract_identities, normalize_identity, stem


def test_normalize_strips_scheme_path_and_generic_tokens():
    assert normalize_identity("https://payments-svc.internal/v1/charge") == "payments"
    assert normalize_identity("payment-service") == "payment"
    assert normalize_identity("$ENV:USER_SERVICE_URL") == "user"
    assert normalize_identity("order-management-service") == "order-management"


def test_stem_folds_plural():
    assert stem("payments") == "payment"
    assert stem(normalize_identity("payments-svc")) == "payment"
    # both singular and plural service names collapse to the same stem
    assert stem(normalize_identity("payment-service")) == stem(
        normalize_identity("payments-service"))


def test_extract_includes_canonical_name():
    ids = {a.alias for a in extract_identities("/nonexistent", "my-service")}
    assert "my" in ids   # 'service' token dropped by normalization


def test_extract_package_json(tmp_path):
    # package name normalizes differently from the folder, so both survive dedup
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "@org/order-management-service"}))
    aliases = extract_identities(tmp_path, "checkout")
    by_source = {a.source: a.alias for a in aliases}
    assert by_source["config"] == "checkout"
    assert by_source["package"] == "order-management"


def test_extract_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("module github.com/acme/billing-service\n\ngo 1.22\n")
    aliases = {a.alias for a in extract_identities(tmp_path, "billing")}
    assert "billing" in aliases


def test_extract_spring_application_name(tmp_path):
    res = tmp_path / "src/main/resources"
    res.mkdir(parents=True)
    (res / "application.yml").write_text("spring:\n  application:\n    name: fraud-service\n")
    # folder 'billing' differs from the declared spring name 'fraud-service'
    aliases = [a for a in extract_identities(tmp_path, "billing") if a.source == "spring"]
    assert aliases and aliases[0].alias == "fraud"


def test_extract_dedupes_by_alias_keeping_highest_confidence(tmp_path):
    # package name normalizes to the same alias as the folder name
    (tmp_path / "package.json").write_text(json.dumps({"name": "payment"}))
    aliases = extract_identities(tmp_path, "payment")
    payment = [a for a in aliases if a.alias == "payment"]
    assert len(payment) == 1
    assert payment[0].confidence == 1.0   # config (1.0) wins over package (0.9)
