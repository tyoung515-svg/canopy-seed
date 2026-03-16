from core.orchestrator import ProjectOrchestrator, REPAIR_AUDIT_SYSTEM


def test_parse_subtasks_recovers_array_with_trailing_comma(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)
    raw = """
    [
      {
        "title": "Task A",
        "description": "Implement backend endpoints",
        "target_files": ["core/api_server.py"],
        "domain_layer": "backend",
        "ambiguity_score": 1,
        "dependency_score": 2,
      }
    ]
    """

    subtasks = orchestrator._parse_subtasks(raw)

    assert len(subtasks) == 1
    assert subtasks[0].title == "Task A"
    assert subtasks[0].ambiguity_score == 1
    assert subtasks[0].dependency_score == 2


def test_parse_subtasks_recovers_object_with_tasks_key(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)
    raw = """
    {
      "tasks": [
        {
          "title": "Task B",
          "description": "Create frontend shell",
          "target_files": ["canopy-ui/app.js"],
          "domain_layer": "frontend",
          "ambiguity": 0,
          "external_deps": 1,
          "notes": "minimal"
        }
      ]
    }
    """

    subtasks = orchestrator._parse_subtasks(raw)

    assert len(subtasks) == 1
    assert subtasks[0].title == "Task B"
    assert subtasks[0].domain_layer == "frontend"
    assert subtasks[0].ambiguity_score == 0
    assert subtasks[0].dependency_score == 1


def test_parse_subtasks_recovers_mixed_text_plus_json(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)
    raw = """
    Here is the plan you asked for:

    ```json
    {
      "subtasks": [
        {
          "title": "Task C",
          "description": "Add regression tests",
          "target_files": ["tests/test_orchestrator_parse.py"],
          "domain_layer": "tests",
          "ambiguity_score": 2,
          "dependency_score": 0
        }
      ]
    }
    ```

    Let me know if you want me to expand each task.
    """

    subtasks = orchestrator._parse_subtasks(raw)

    assert len(subtasks) == 1
    assert subtasks[0].title == "Task C"
    assert subtasks[0].domain_layer == "tests"
    assert subtasks[0].ambiguity_score == 2
    assert subtasks[0].dependency_score == 0


def test_decompose_returns_single_fallback_task_on_non_json_response(mock_ai_backend):
    import asyncio

    orchestrator = ProjectOrchestrator(mock_ai_backend)
    mock_ai_backend.response = (
        "I'm sorry, I can't help with that. "
        "Here is a plain English paragraph with no JSON content at all."
    )

    project_context = {
        "project_name": "Canopy Seed",
        "description": "Build orchestrated project tasks",
    }

    subtasks = asyncio.run(orchestrator.decompose(project_context))

    assert isinstance(subtasks, list)
    assert len(subtasks) == 1
    assert subtasks[0].title.strip() != ""
    assert subtasks[0].description.strip() != ""

def test_build_repair_prompt_contains_source_tree(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)
    source_tree = {
        "app.py": "print('hello')",
        "utils.py": "def helper(): pass",
    }
    test_output = "FAILED tests/test_app.py::test_main - AssertionError: expected 42 got 41"
    project_context = {
        "project_name": "TestProj",
        "description": "A test project",
    }

    prompt = orchestrator._build_repair_prompt(source_tree, test_output, project_context)

    assert isinstance(prompt, str)
    assert len(prompt) > 0
    assert "app.py" in prompt
    assert "utils.py" in prompt
    assert "AssertionError: expected 42 got 41" in prompt
    assert "target_files" in prompt
    assert "TestProj" in prompt


def test_build_repair_prompt_truncates_long_files(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)
    long_file_content = "\n".join([f"line {i}" for i in range(250)])
    source_tree = {
        "long_file.py": long_file_content,
    }
    test_output = "FAILED test.py"
    project_context = {
        "project_name": "LongFileProj",
        "description": "Test truncation",
    }

    prompt = orchestrator._build_repair_prompt(source_tree, test_output, project_context)

    assert isinstance(prompt, str)
    assert "[50 lines truncated]" in prompt
    assert len(prompt) < 50000


def test_build_repair_prompt_caps_test_output_to_4000_chars(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)
    source_tree = {"app.py": "print('ok')"}
    test_output = "E" * 5000

    prompt = orchestrator._build_repair_prompt(
        source_tree=source_tree,
        test_output=test_output,
        project_context={"project_name": "CapProj", "description": "cap test"},
    )

    assert "## Test Output (failures)" in prompt
    section = prompt.split("## Test Output (failures)", 1)[1]
    body = section.split("## Task", 1)[0]
    fenced = body.strip().split("```", 2)
    extracted = (fenced[1] if len(fenced) > 1 else "").strip("\n")
    assert len(extracted) <= 4000
    assert "... [truncated]" in extracted


def test_repair_audit_uses_gemini_and_parses_subtasks(mock_ai_backend):  # Session 58: auditor → gemini-customtools
    import asyncio

    orchestrator = ProjectOrchestrator(mock_ai_backend)
    mock_ai_backend.response = """
    {
      "subtasks": [
        {
          "title": "Fix app import",
          "description": "Correct broken import path in app module",
          "target_files": ["app.py"],
          "domain_layer": "backend",
          "ambiguity_score": 0,
          "dependency_score": 1
        }
      ]
    }
    """

    result = asyncio.run(
        orchestrator.repair_audit(
            source_tree={"app.py": "from wrong import x"},
            test_output="FAILED tests/test_app.py::test_import - ModuleNotFoundError",
            project_context={"project_name": "RepairProj", "description": "repair"},
        )
    )

    assert len(result) == 1
    assert result[0].title == "Fix app import"
    assert len(mock_ai_backend.calls) == 1
    assert mock_ai_backend.calls[0]["backend"] == "gemini-customtools"
    assert mock_ai_backend.calls[0]["system"] == REPAIR_AUDIT_SYSTEM
    assert mock_ai_backend.calls[0]["json_mode"] is True


def test_build_repair_prompt_includes_truncation_rule(mock_ai_backend):
    """Verify that _build_repair_prompt includes the CRITICAL truncation rule."""
    orchestrator = ProjectOrchestrator(mock_ai_backend)
    source_tree = {
        "app.py": "print('test')",
    }
    test_output = "FAILED tests/test_app.py - SyntaxError"
    project_context = {
        "project_name": "TruncationTest",
        "description": "Test truncation rule",
    }

    prompt = orchestrator._build_repair_prompt(source_tree, test_output, project_context)

    assert isinstance(prompt, str)
    assert "CONCISE: Previous version was truncated" in prompt
    assert "150 lines" in prompt


def test_build_decompose_prompt_enforces_pyproject_slug_and_hatch_packages(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)
    context = {
        "project_name": "My Cool App",
        "description": "Python scaffold",
        "goals": [],
        "constraints": [],
        "tech_preferences": {},
    }

    prompt = orchestrator._build_decompose_prompt(context)

    assert "OUTPUT_DIRECTORY_SLUG: my_cool_app" in prompt
    assert "[project]" in prompt
    assert "name" in prompt
    assert "[tool.hatch.build.targets.wheel]" in prompt
    assert "packages = [\"OUTPUT_DIRECTORY_SLUG\"]" in prompt
    assert "Do not use auto-discovery for wheel packages." in prompt


def test_build_repair_prompt_enforces_pyproject_slug_and_hatch_packages(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)
    source_tree = {"pyproject.toml": "[project]\nname='wrong'\n"}
    test_output = "FAILED tests/test_pkg.py - ModuleNotFoundError"
    context = {
        "project_name": "Repair Slug",
        "description": "repair",
    }

    prompt = orchestrator._build_repair_prompt(source_tree, test_output, context)

    assert "OUTPUT_DIRECTORY_SLUG: repair_slug" in prompt
    assert "[tool.hatch.build.targets.wheel]" in prompt
    assert "packages = [\"OUTPUT_DIRECTORY_SLUG\"]" in prompt
    assert "Do not use auto-discovery for wheel packages." in prompt


def test_pyproject_license_guardrail_uses_mit_text_not_license_file(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)

    decompose_prompt = orchestrator._build_decompose_prompt({
        "project_name": "License Check",
        "description": "Python package",
    })
    repair_prompt = orchestrator._build_repair_prompt(
        source_tree={"pyproject.toml": "[project]\nname='x'\n"},
        test_output="FAILED tests/test_pkg.py - metadata generation failed",
        project_context={"project_name": "License Check", "description": "repair"},
    )

    assert 'license = {text = "MIT"}' in decompose_prompt
    assert 'license = {file = "LICENSE"}' in decompose_prompt
    assert "never" in decompose_prompt.lower()

    assert 'license = {text = "MIT"}' in repair_prompt
    assert 'license = {file = "LICENSE"}' in repair_prompt
    assert "never" in repair_prompt.lower()


def test_pyproject_metadata_guardrail_forbids_ungenerated_file_refs(mock_ai_backend):
    orchestrator = ProjectOrchestrator(mock_ai_backend)

    decompose_prompt = orchestrator._build_decompose_prompt({
        "project_name": "Metadata Guardrail",
        "description": "Python package",
    })
    repair_prompt = orchestrator._build_repair_prompt(
        source_tree={"pyproject.toml": "[project]\nname='x'\n"},
        test_output="FAILED tests/test_pkg.py - metadata generation failed",
        project_context={"project_name": "Metadata Guardrail", "description": "repair"},
    )

    for prompt in (decompose_prompt, repair_prompt):
        assert "Omit the `readme` field entirely." in prompt
        assert "Do not include `readme` in any form" in prompt
        assert "not as a string, not as a table, not as an inline table" in prompt
        assert 'license = {file = ...}' in prompt
        assert "changelog" in prompt.lower()
        assert "authors" in prompt.lower()
        assert "Use inline forms or omit." in prompt


def test_parse_subtask_with_explicit_tier(mock_ai_backend):
        orchestrator = ProjectOrchestrator(mock_ai_backend)
        raw = """
        {
            "subtasks": [
                {
                    "title": "Tiered Task",
                    "description": "depends on tier 1",
                    "target_files": ["app/service.py"],
                    "domain_layer": "backend",
                    "ambiguity_score": 1,
                    "dependency_score": 1,
                    "tier": 2
                }
            ]
        }
        """

        subtasks = orchestrator._parse_subtasks(raw)

        assert len(subtasks) == 1
        assert subtasks[0].tier == 2


def test_parse_subtask_missing_tier_defaults_to_one(mock_ai_backend):
        orchestrator = ProjectOrchestrator(mock_ai_backend)
        raw = """
        {
            "subtasks": [
                {
                    "title": "No Tier",
                    "description": "no tier field",
                    "target_files": ["app/core.py"],
                    "domain_layer": "backend",
                    "ambiguity_score": 1,
                    "dependency_score": 1
                }
            ]
        }
        """

        subtasks = orchestrator._parse_subtasks(raw)

        assert len(subtasks) == 1
        assert subtasks[0].tier == 1


def test_parse_subtask_bad_tier_defaults_to_one(mock_ai_backend):
        orchestrator = ProjectOrchestrator(mock_ai_backend)
        raw = """
        {
            "subtasks": [
                {
                    "title": "Bad Tier",
                    "description": "bad tier value",
                    "target_files": ["app/core.py"],
                    "domain_layer": "backend",
                    "ambiguity_score": 1,
                    "dependency_score": 1,
                    "tier": "bad_value"
                }
            ]
        }
        """

        subtasks = orchestrator._parse_subtasks(raw)

        assert len(subtasks) == 1
        assert subtasks[0].tier == 1


def test_mixed_tier_subtasks_sort_ascending(mock_ai_backend):
        orchestrator = ProjectOrchestrator(mock_ai_backend)
        raw = """
        [
            {
                "title": "Tier 3 tests",
                "description": "tests",
                "target_files": ["tests/test_app.py"],
                "domain_layer": "tests",
                "ambiguity_score": 0,
                "dependency_score": 1,
                "tier": 3
            },
            {
                "title": "Tier 1 module",
                "description": "leaf module",
                "target_files": ["app/base.py"],
                "domain_layer": "backend",
                "ambiguity_score": 0,
                "dependency_score": 0,
                "tier": 1
            },
            {
                "title": "Tier 2 module",
                "description": "imports tier1",
                "target_files": ["app/service.py"],
                "domain_layer": "backend",
                "ambiguity_score": 0,
                "dependency_score": 1,
                "tier": 2
            }
        ]
        """

        subtasks = orchestrator._parse_subtasks(raw)
        tiers_sorted = [subtask.tier for subtask in sorted(subtasks, key=lambda subtask: subtask.tier)]

        assert tiers_sorted == [1, 2, 3]


def test_round_trip_decomposition_json_preserves_all_tiers(mock_ai_backend):
        orchestrator = ProjectOrchestrator(mock_ai_backend)
        raw = """
        {
            "subtasks": [
                {
                    "title": "Core",
                    "description": "core module",
                    "target_files": ["app/core.py"],
                    "domain_layer": "backend",
                    "ambiguity_score": 0,
                    "dependency_score": 0,
                    "tier": 1
                },
                {
                    "title": "Service",
                    "description": "service module",
                    "target_files": ["app/service.py"],
                    "domain_layer": "backend",
                    "ambiguity_score": 1,
                    "dependency_score": 1,
                    "tier": 2
                },
                {
                    "title": "Tests",
                    "description": "test coverage",
                    "target_files": ["tests/test_service.py"],
                    "domain_layer": "tests",
                    "ambiguity_score": 1,
                    "dependency_score": 1,
                    "tier": 3
                }
            ]
        }
        """

        subtasks = orchestrator._parse_subtasks(raw)

        assert [subtask.tier for subtask in subtasks] == [1, 2, 3]
