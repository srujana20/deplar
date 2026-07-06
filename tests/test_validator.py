from deplar.validator import WorkspaceValidator, detect_test_command


def test_detect_pytest(tmp_path):
    (tmp_path / "tests").mkdir()
    assert detect_test_command(tmp_path) == "pytest -q"


def test_detect_npm(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert detect_test_command(tmp_path) == "npm test --silent"


def test_detect_none(tmp_path):
    assert detect_test_command(tmp_path) is None


def test_validate_pass_and_fail(tmp_path):
    # `sh -c` builtins keep this independent of any installed test runner.
    ws = tmp_path / "ws"
    (ws / "good").mkdir(parents=True)
    result = WorkspaceValidator().validate(ws, test_cmd="true")
    assert result.repos[0].passed
    assert result.ok is True

    result = WorkspaceValidator().validate(ws, test_cmd="false")
    assert not result.repos[0].passed
    assert result.ok is False


def test_validate_skips_when_no_command(tmp_path):
    ws = tmp_path / "ws"
    (ws / "empty").mkdir(parents=True)   # no tests/, package.json, etc.
    result = WorkspaceValidator().validate(ws)
    assert result.repos[0].skipped
    assert result.ok is True   # skipped repos don't fail the run


def test_test_cmd_override(tmp_path):
    ws = tmp_path / "ws"
    (ws / "anyrepo").mkdir(parents=True)
    result = WorkspaceValidator().validate(ws, test_cmd="true")
    assert result.repos[0].passed
    assert result.repos[0].command == "true"
