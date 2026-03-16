import asyncio
try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib
from unittest.mock import AsyncMock

from core.tester_swarm import SwarmSummary, TesterSwarm


def test_run_returns_empty_summary_on_empty_dir(mock_ai_backend, tmp_path):
    swarm = TesterSwarm(mock_ai_backend)

    summary = asyncio.run(swarm.run(str(tmp_path)))

    assert isinstance(summary, SwarmSummary)
    assert summary.total == 0
    assert summary.passed == 0
    assert summary.failed == 0


def test_run_marks_passing_test_correctly(mock_ai_backend, tmp_path):
    test_file = tmp_path / "test_pass.py"
    test_file.write_text("def test_ok():\n    pass\n", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)
    summary = asyncio.run(swarm.run(str(tmp_path)))

    assert summary.total == 1
    assert len(summary.results) == 1
    assert summary.results[0].passed is True


def test_run_marks_failing_test_correctly(mock_ai_backend, tmp_path):
    test_file = tmp_path / "test_fail.py"
    test_file.write_text("def test_bad():\n    assert False\n", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)
    summary = asyncio.run(swarm.run(str(tmp_path)))

    assert summary.total == 1
    assert len(summary.results) == 1
    assert summary.results[0].passed is False
    assert summary.results[0].failure_output != ""


def test_ai_summary_called_on_failure(mock_ai_backend, tmp_path):
    test_file = tmp_path / "test_fail.py"
    test_file.write_text("def test_bad():\n    assert False\n", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)
    _ = asyncio.run(swarm.run(str(tmp_path)))

    assert mock_ai_backend.calls


def test_detect_project_language_returns_typescript_when_package_json_exists(mock_ai_backend, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)

    assert swarm._detect_project_language(str(tmp_path)) == "typescript"


def test_detect_project_language_returns_python_when_no_package_json(mock_ai_backend, tmp_path):
    (tmp_path / "README.md").write_text("placeholder", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)

    assert swarm._detect_project_language(str(tmp_path)) == "python"


def test_detect_language_html(mock_ai_backend, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)

    assert swarm._detect_project_language(str(tmp_path)) == "python"


def test_detect_language_unknown(mock_ai_backend, tmp_path):
    swarm = TesterSwarm(mock_ai_backend)

    assert swarm._detect_project_language(str(tmp_path)) == "python"


def test_run_short_circuits_for_js_project(mock_ai_backend, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    expected = SwarmSummary(total=2, passed=2, failed=0, duration=0.1, results=[])

    swarm = TesterSwarm(mock_ai_backend)
    swarm._run_js_tests = AsyncMock(return_value=expected)

    summary = asyncio.run(swarm.run(str(tmp_path)))

    assert summary == expected


def test_run_short_circuits_for_html_project(mock_ai_backend, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)
    summary = asyncio.run(swarm.run(str(tmp_path)))

    assert isinstance(summary, SwarmSummary)
    assert summary.total == 0


def test_js_runner_returns_empty_summary_when_npx_not_found(mock_ai_backend, tmp_path, monkeypatch):
    sse = AsyncMock()
    swarm = TesterSwarm(mock_ai_backend, sse_broadcast=sse)

    async def _raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("npx not found")

    monkeypatch.setattr("core.tester_swarm.asyncio.create_subprocess_exec", _raise_file_not_found)

    summary = asyncio.run(swarm._run_js_tests(str(tmp_path)))

    assert isinstance(summary, SwarmSummary)
    assert summary.total == 0
    assert summary.passed == 0
    assert summary.failed == 0
    sse.assert_awaited_once_with(
        "runner_unavailable",
        {
            "reason": "JS test runner (npm/npx) not found on PATH",
            "export_dir": str(tmp_path),
        },
    )


def test_run_js_tests_parses_vitest_json_output(mock_ai_backend, tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest run"}}',
        encoding="utf-8",
    )
    (tmp_path / "node_modules").mkdir(parents=True, exist_ok=True)

    swarm = TesterSwarm(mock_ai_backend)

    vitest_json = (
        '{"testResults":[{"name":"src/registry.spec.ts","status":"failed",'
        '"assertionResults":[{"fullName":"Registry should add cell","status":"passed"},'
        '{"fullName":"Registry should reject duplicate","status":"failed",'
        '"failureMessages":["expected duplicate error"]}]}]}'
    )

    async def fake_exec_cmd(cmd, cwd, timeout=None):
        return 1, vitest_json, ""

    monkeypatch.setattr(swarm, "_exec_cmd", fake_exec_cmd)

    summary = asyncio.run(swarm._run_js_tests(str(tmp_path)))

    assert summary.total >= 2
    assert any(result.passed for result in summary.results)
    assert any((not result.passed) and "duplicate" in result.failure_output for result in summary.results)


def test_run_js_tests_synthetic_failure_on_npm_install_error(mock_ai_backend, tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)

    def fake_which(name):
        if name == "npm":
            return "C:/Program Files/nodejs/npm.cmd"
        if name == "npx":
            return "C:/Program Files/nodejs/npx.cmd"
        return None

    monkeypatch.setattr("core.tester_swarm.shutil.which", fake_which, raising=False)

    async def fake_exec_cmd(cmd, cwd, timeout=None):
        if len(cmd) >= 2 and cmd[0].endswith("npm.cmd") and cmd[1] == "install":
            return 1, "", "npm install failed"
        return 0, "", ""

    monkeypatch.setattr(swarm, "_exec_cmd", fake_exec_cmd)

    summary = asyncio.run(swarm._run_js_tests(str(tmp_path)))

    assert summary.total == 1
    assert summary.failed == 1
    assert summary.results[0].test_file == "npm_install"
    assert "npm install failed" in summary.results[0].failure_output


def test_run_js_tests_unavailable_when_npm_not_on_path(mock_ai_backend, tmp_path, monkeypatch, caplog):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)
    sse = AsyncMock()
    swarm._sse_broadcast = sse

    monkeypatch.setattr("core.tester_swarm.shutil.which", lambda _name: None, raising=False)

    caplog.set_level("WARNING")
    summary = asyncio.run(swarm._run_js_tests(str(tmp_path)))

    assert summary.total == 0
    assert summary.passed == 0
    assert summary.failed == 0
    sse.assert_awaited_once_with(
        "runner_unavailable",
        {
            "reason": "npm not found on PATH — install Node.js",
            "export_dir": str(tmp_path),
        },
    )
    assert "npm not on PATH" in caplog.text


def test_run_js_tests_prefers_npm_test_script(mock_ai_backend, tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")
    (tmp_path / "node_modules").mkdir(parents=True, exist_ok=True)

    swarm = TesterSwarm(mock_ai_backend)
    commands = []

    monkeypatch.setattr("core.tester_swarm.shutil.which", lambda name: "C:/Program Files/nodejs/npm.CMD" if name == "npm" else None)

    async def fake_exec_cmd(cmd, cwd, timeout=None):
        commands.append(cmd)
        return 0, '{"testResults": []}', ""

    monkeypatch.setattr(swarm, "_exec_cmd", fake_exec_cmd)

    _ = asyncio.run(swarm._run_js_tests(str(tmp_path)))

    assert commands
    assert commands[0][0].lower().endswith("npm.cmd")
    assert commands[0][1:3] == ["run", "test"]


def test_run_js_tests_empty_parse_returns_synthetic_failure(mock_ai_backend, tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")
    (tmp_path / "node_modules").mkdir(parents=True, exist_ok=True)

    swarm = TesterSwarm(mock_ai_backend)

    async def fake_exec_cmd(cmd, cwd, timeout=None):
        return 0, "", ""

    monkeypatch.setattr(swarm, "_exec_cmd", fake_exec_cmd)

    summary = asyncio.run(swarm._run_js_tests(str(tmp_path)))

    assert summary.total == 1
    assert summary.failed == 1
    assert summary.results[0].test_file == "js_tests"


def test_run_single_test_continues_when_editable_install_fails(mock_ai_backend, tmp_path, monkeypatch, caplog):
    test_file = tmp_path / "test_example.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[build-system]\nrequires = []\n", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)
    swarm._persist_result = AsyncMock()
    swarm._broadcast = AsyncMock()
    swarm._summarize_failure = AsyncMock(return_value="")
    swarm._attempt_install_repair = AsyncMock(return_value=False)

    calls = []

    async def fake_exec_cmd(cmd, cwd):
        calls.append(cmd)
        if cmd[:4] == ["python", "-m", "pytest", str(test_file)]:
            return 0, "", ""
        if len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "install"]:
            return 1, "", "install failed"
        return 0, "", ""

    monkeypatch.setattr(swarm, "_exec_cmd", fake_exec_cmd)

    caplog.set_level("WARNING")
    result = asyncio.run(
        swarm._run_single_test(
            export_path=tmp_path,
            test_file=test_file,
            source_file="",
            semaphore=asyncio.Semaphore(1),
            session_id="session-1",
        )
    )

    assert result.passed is True
    assert any(len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "install"] for cmd in calls)
    install_cmds = [cmd for cmd in calls if len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "install"]]
    assert install_cmds
    assert "--no-deps" in install_cmds[0]
    assert any(cmd[:4] == ["python", "-m", "pytest", str(test_file)] for cmd in calls)
    assert "Editable install failed" in caplog.text


def test_maybe_install_editable_package_caches_per_export_dir(mock_ai_backend, tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[build-system]\nrequires = []\n", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)
    started = asyncio.Event()
    unblock = asyncio.Event()

    async def fake_exec_cmd(cmd, cwd):
        started.set()
        await unblock.wait()
        return 0, "", ""

    monkeypatch.setattr(swarm, "_exec_cmd", fake_exec_cmd)

    async def _run_concurrent_calls():
        task1 = asyncio.create_task(swarm._maybe_install_editable_package(tmp_path))
        await started.wait()

        export_key = str(tmp_path.resolve())
        assert swarm._editable_install_done.get(export_key) is True

        task2 = asyncio.create_task(swarm._maybe_install_editable_package(tmp_path))
        await task2

        unblock.set()
        await task1

    asyncio.run(_run_concurrent_calls())

    export_key = str(tmp_path.resolve())
    assert swarm._editable_install_done.get(export_key) is True


def test_sanitize_pyproject_toml_normalizes_project_fields(mock_ai_backend, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "My-App"
version = "0.1.0"
readme = "README.md"
license = {file = "LICENSE"}

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
""".strip() + "\n",
        encoding="utf-8",
    )

    swarm = TesterSwarm(mock_ai_backend)
    swarm._sanitize_pyproject_toml(tmp_path)

    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert "readme" not in parsed["project"]
    assert parsed["project"]["license"] == {"text": "MIT"}
    assert parsed["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["my_app"]


def test_sanitize_pyproject_toml_strips_markdown_fences(mock_ai_backend, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
```toml
[project]
name = "fenced-project"
version = "0.1.0"

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
```
""".strip() + "\n",
        encoding="utf-8",
    )

    swarm = TesterSwarm(mock_ai_backend)
    swarm._sanitize_pyproject_toml(tmp_path)

    sanitized_text = pyproject.read_text(encoding="utf-8")
    assert "```" not in sanitized_text

    parsed = tomllib.loads(sanitized_text)
    assert parsed["project"]["name"] == "fenced-project"

    pyproject.write_text(
        "```toml\r\n"
        "[project]\r\n"
        "name = \"fenced-project-crlf\"\r\n"
        "version = \"0.1.0\"\r\n"
        "\r\n"
        "[build-system]\r\n"
        "requires = [\"setuptools\"]\r\n"
        "build-backend = \"setuptools.build_meta\"\r\n"
        "```\r\n",
        encoding="utf-8",
    )

    swarm._sanitize_pyproject_toml(tmp_path)

    sanitized_text_crlf = pyproject.read_text(encoding="utf-8")
    assert "```" not in sanitized_text_crlf

    parsed_crlf = tomllib.loads(sanitized_text_crlf)
    assert parsed_crlf["project"]["name"] == "fenced-project-crlf"


def test_sanitize_strips_dynamic_version(mock_ai_backend, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "unit-converter"
dynamic = ["version"]

[tool.hatch.version]
path = "unit_converter/__init__.py"
source = "regex"
""".strip() + "\n",
        encoding="utf-8",
    )

    swarm = TesterSwarm(mock_ai_backend)
    swarm._sanitize_pyproject_toml(tmp_path)

    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert "dynamic" not in parsed["project"]
    assert parsed["project"]["version"] == "0.1.0"
    assert "version" not in parsed["tool"]["hatch"]


def test_sanitize_preserves_existing_static_version(mock_ai_backend, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "unit-converter"
version = "2.3.1"
dynamic = ["version"]

[tool.hatch.version]
path = "unit_converter/__init__.py"
""".strip() + "\n",
        encoding="utf-8",
    )

    swarm = TesterSwarm(mock_ai_backend)
    swarm._sanitize_pyproject_toml(tmp_path)

    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert "dynamic" not in parsed["project"]
    assert parsed["project"]["version"] == "2.3.1"
    assert "version" not in parsed["tool"]["hatch"]


def test_sanitize_strips_dynamic_version_leaves_other_dynamics(mock_ai_backend, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "unit-converter"
dynamic = ["version", "description"]

[tool.hatch.version]
source = "regex"
""".strip() + "\n",
        encoding="utf-8",
    )

    swarm = TesterSwarm(mock_ai_backend)
    swarm._sanitize_pyproject_toml(tmp_path)

    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert parsed["project"]["dynamic"] == ["description"]
    assert parsed["project"]["version"] == "0.1.0"
    assert "version" not in parsed["tool"]["hatch"]


def test_sanitize_switches_build_backend_to_setuptools(mock_ai_backend, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "unit-converter"
version = "0.1.0"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
""".strip() + "\n",
        encoding="utf-8",
    )

    swarm = TesterSwarm(mock_ai_backend)
    swarm._sanitize_pyproject_toml(tmp_path)

    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert parsed["build-system"]["requires"] == ["setuptools"]
    assert parsed["build-system"]["build-backend"] == "setuptools.build_meta"


def test_sanitize_adds_build_system_if_missing(mock_ai_backend, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "unit-converter"
version = "0.1.0"
""".strip() + "\n",
        encoding="utf-8",
    )

    swarm = TesterSwarm(mock_ai_backend)
    swarm._sanitize_pyproject_toml(tmp_path)

    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert "build-system" in parsed
    assert parsed["build-system"]["requires"] == ["setuptools"]
    assert parsed["build-system"]["build-backend"] == "setuptools.build_meta"


def test_install_hard_gate_skips_pytest_on_persistent_failure(mock_ai_backend, tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[build-system]\nrequires = []\n", encoding="utf-8")
    (tmp_path / "test_example.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)
    executed_commands = []

    async def fake_exec_cmd(cmd, cwd):
        executed_commands.append(cmd)
        if len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "install"]:
            return 1, "", "install failed"
        if cmd[:4] == ["python", "-m", "pytest", str(tmp_path / "test_example.py")]:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(swarm, "_exec_cmd", fake_exec_cmd)
    swarm._attempt_install_repair = AsyncMock(return_value=False)

    summary = asyncio.run(swarm.run(str(tmp_path)))

    assert summary.install_hard_fail is True
    assert summary.total == 0
    assert summary.passed == 0
    assert summary.failed == 0
    assert not any(cmd[:3] == ["python", "-m", "pytest"] for cmd in executed_commands)


def test_install_hard_gate_proceeds_after_repair_success(mock_ai_backend, tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[build-system]\nrequires = []\n", encoding="utf-8")
    (tmp_path / "test_example.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    swarm = TesterSwarm(mock_ai_backend)
    install_attempts = {"count": 0}

    async def fake_exec_cmd(cmd, cwd):
        if len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "install"]:
            install_attempts["count"] += 1
            if install_attempts["count"] == 1:
                return 1, "", "initial install failed"
            return 0, "", ""

        if cmd[:3] == ["python", "-m", "pytest"]:
            return 0, "", ""

        return 0, "", ""

    monkeypatch.setattr(swarm, "_exec_cmd", fake_exec_cmd)
    swarm._attempt_install_repair = AsyncMock(return_value=True)

    summary = asyncio.run(swarm.run(str(tmp_path)))

    assert summary.install_hard_fail is False
    assert summary.total == 1
    assert summary.passed == 1
    assert summary.failed == 0
    assert install_attempts["count"] == 2
