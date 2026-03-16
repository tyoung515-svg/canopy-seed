import pytest

from memory.canopy import MemoryStore


@pytest.fixture
def mock_settings():
    class MockSettings:
        AUTO_EXTRACT_ENABLED = True
        LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
        LMSTUDIO_MODEL = "test-model"
        WORKER_BASE_URL = "http://localhost:1234/v1"
        WORKER_MODEL = "test-worker"
        ENABLE_PROACTIVE = False

    return MockSettings()


@pytest.fixture
def memory_store(tmp_path):
    db_path = tmp_path / "test_canopy.db"
    return MemoryStore(db_path)


@pytest.fixture
def mock_ai_backend():
    class MockAIBackend:
        def __init__(self):
            self.response = "[]"
            self.calls = []
            self.settings = None

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return self.response

        async def get_backend_status(self):
            return "Mock Backend: OK"

        def get_role_chain(self, role: str):
            """Return single-element chains using legacy hardcoded backends so
            pre-ADR-038/039 tests continue to pass unchanged.

            ADR-039 adds contract_generator and giant_brain roles:
              contract_generator → openai-codex (legacy contract backend)
              giant_brain        → gemini-flash  (first backend in new chain)
            """
            _legacy = {
                "orchestrator":       ["openai-codex"],
                # Session 58: auditor primary → gemini-customtools (thinking-constrained, no schema)
                "auditor":            ["gemini-customtools"],
                # ADR-039 — Phase 0 contract generation
                "contract_generator": ["openai-codex"],
                # ADR-039 — Giant Brain audit
                "giant_brain":        ["gemini-flash"],
            }
            return _legacy.get(role, ["claude"])

        def get_primary_backend_for_role(self, role: str):
            return self.get_role_chain(role)[0]

    return MockAIBackend()
