"""
Canopy Seed Settings
--------------------
Central configuration management.

API key resolution order (highest priority first):
  1. Forge vault bridge  — GET http://localhost:5151/vault/keys (when Forge proxy is running
                            and the vault is unlocked).  Keys are pushed by the Forge UI
                            immediately after vault unlock and wiped on vault lock.
  2. .env file / system env — loaded via python-dotenv load_dotenv(override=True).

Maintain keys in one place: the Forge vault.
The .env file is the fallback for headless / CI runs where the Forge is not running.
"""

import json
import os
import urllib.request
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv(override=True)  # .env values override system env for the fallback layer


# ── Forge vault bridge ─────────────────────────────────────────────────────────
# Try to read keys pushed by the Forge UI into the local proxy (localhost:5151).
# Non-blocking: 0.5 s timeout so startup is never held up if proxy isn't running.
def _load_forge_vault_keys(proxy_url: str = "http://localhost:5151") -> dict:
    """Return in-memory vault keys from the Forge proxy, or {} on any error."""
    try:
        req = urllib.request.Request(proxy_url + "/vault/keys")
        with urllib.request.urlopen(req, timeout=0.5) as resp:
            data = json.loads(resp.read().decode())
            return data.get("keys", {})
    except Exception:
        return {}


_forge_keys = _load_forge_vault_keys()


def _key(forge_field: str, env_var: str, fallback: str = "") -> str:
    """Resolve an API key: Forge vault first, .env / system env second."""
    vault_val = _forge_keys.get(forge_field, "")
    if isinstance(vault_val, str) and vault_val.strip():
        return vault_val.strip()
    return os.getenv(env_var, fallback)

class Settings:
    def __init__(self):
        # Bot identifiers (Telegram — optional)
        self.BOT_TOKEN = os.getenv("BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", ""))
        self.OWNER_ID = int(os.getenv("OWNER_ID", "0"))

        # Paths
        self.ROOT_DIR = Path(".")
        self.LOG_DIR = self.ROOT_DIR / "logs"
        self.DB_PATH = self.ROOT_DIR / "memory" / "canopy.db"
        self.context_output_path = Path(os.getenv("EXPORTS_PATH", "exports")) / "PROJECT_CONTEXT.json"

        # ── Anthropic / Claude ──────────────────────────────────────────────
        self.ANTHROPIC_API_KEY = _key("anthropic_api_key", "ANTHROPIC_API_KEY")
        self.CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        self.CLAUDE_ESCALATION_ENABLED = os.getenv("CLAUDE_ESCALATION", "true").lower() == "true"

        # ── API Server ──────────────────────────────────────────────────────
        self.DASHBOARD_API_PORT = int(os.getenv("DASHBOARD_API_PORT", "7822"))

        # ── Google Gemini ───────────────────────────────────────────────────
        self.GOOGLE_API_KEY = _key("google_api_key", "GOOGLE_API_KEY")
        self.GEMINI_API_KEY = self.GOOGLE_API_KEY  # alias
        # Session 56: default to customtools endpoint — all Pro calls get native tool support
        self.GOOGLE_GEMINI_MODEL = os.getenv("GOOGLE_GEMINI_MODEL", "gemini-3.1-pro-preview-customtools")
        self.GEMINI_CUSTOMTOOLS_MODEL = os.getenv(
            "GEMINI_CUSTOMTOOLS_MODEL", "gemini-3.1-pro-preview-customtools"
        )
        # Session 58: plain Pro (adaptive thinking, no schema) — reserved for interview role only
        self.GOOGLE_GEMINI_PLAIN_MODEL = os.getenv("GOOGLE_GEMINI_PLAIN_MODEL", "gemini-3.1-pro-preview")
        # ADR-035: Gemini 3.x Flash family (LM Forge benchmark winners)
        # gemini-3-flash-preview: 9.83 avg, $0.50/$3.00 — replaces openai-codex as STANDARD default
        self.GEMINI_FLASH_MODEL = os.getenv("GEMINI_FLASH_MODEL", "gemini-3-flash-preview")
        # gemini-3.1-flash-lite-preview: 9.46 avg, 233 t/s, $0.25/$1.50 — LITE default
        self.GEMINI_FLASH_LITE_MODEL = os.getenv("GEMINI_FLASH_LITE_MODEL", "gemini-3.1-flash-lite-preview")

        # ── OpenAI ───────────────────────────────────────────────────────────
        self.OPENAI_API_KEY = _key("openai_api_key", "OPENAI_API_KEY")
        self.OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
        self.OPENAI_CODEX_MODEL = os.getenv("OPENAI_CODEX_MODEL", "gpt-5.3-codex")
        self.CODEX_REASONING_EFFORT = os.getenv("CODEX_REASONING_EFFORT", "medium")
        # Session 57: T3-T8 raised to match _estimate_contract_token_budget defaults
        self.CONTRACT_TOKEN_BUDGET_T1 = int(os.getenv("CONTRACT_TOKEN_BUDGET_T1", "2048"))
        self.CONTRACT_TOKEN_BUDGET_T2 = int(os.getenv("CONTRACT_TOKEN_BUDGET_T2", "4096"))
        self.CONTRACT_TOKEN_BUDGET_T3 = int(os.getenv("CONTRACT_TOKEN_BUDGET_T3", "8192"))   # was 6144
        self.CONTRACT_TOKEN_BUDGET_T4 = int(os.getenv("CONTRACT_TOKEN_BUDGET_T4", "12288"))  # was 8192
        self.CONTRACT_TOKEN_BUDGET_T5 = int(os.getenv("CONTRACT_TOKEN_BUDGET_T5", "16384"))  # was 12288
        self.CONTRACT_TOKEN_BUDGET_T6 = int(os.getenv("CONTRACT_TOKEN_BUDGET_T6", "24576"))  # was 16384
        self.CONTRACT_TOKEN_BUDGET_T7 = int(os.getenv("CONTRACT_TOKEN_BUDGET_T7", "32768"))  # was 24576
        self.CONTRACT_TOKEN_BUDGET_T8 = int(os.getenv("CONTRACT_TOKEN_BUDGET_T8", "65536"))  # was 32768

        # ── LM Studio / local inference ────────────────────────────────────
        self.LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
        self.LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "local-model")

        # ── Worker proxy (optional) ────────────────────────────────────────
        self.WORKER_BASE_URL = os.getenv("WORKER_BASE_URL", "http://localhost:8080/v1")
        self.WORKER_MODEL = os.getenv("WORKER_MODEL", "worker-model")

        # ── Default routing ────────────────────────────────────────────────
        self.DEFAULT_AI_BACKEND = os.getenv("DEFAULT_AI_BACKEND", "claude")

        # ── Telegram (optional) ───────────────────────────────────────────
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("BOT_TOKEN", ""))
        self.AUTHORIZED_USER_IDS: list = [
            int(x.strip()) for x in os.getenv("AUTHORIZED_USER_IDS", "").split(",")
            if x.strip().isdigit()
        ]

        # ── Whisper / audio (optional) ────────────────────────────────────
        self.WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
        self.WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")

        # ── Feature flags ─────────────────────────────────────────────────
        self.AUTO_EXTRACT_ENABLED = os.getenv("AUTO_EXTRACT", "true").lower() == "true"
        self.ENABLE_PROACTIVE = os.getenv("ENABLE_PROACTIVE", "false").lower() == "true"
        self.ALLOWED_READ_PATHS: list = [
            p.strip() for p in os.getenv("ALLOWED_READ_PATHS", ".").split(",") if p.strip()
        ]

        # Behavior
        self.DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
        self.LOG_LEVEL = "DEBUG" if self.DEBUG_MODE else "INFO"

        # ── ADR-030 Big Fixer thresholds ──────────────────────────────────────
        self.BIG_FIXER_LOW_THRESHOLD = int(os.getenv("BIG_FIXER_LOW_THRESHOLD", "40"))
        self.BIG_FIXER_HIGH_THRESHOLD = int(os.getenv("BIG_FIXER_HIGH_THRESHOLD", "95"))
        self.BIG_FIXER_MAX_ATTEMPTS = int(os.getenv("BIG_FIXER_MAX_ATTEMPTS", "2"))

        # ── Qwen / Alibaba DashScope (ADR-034) ────────────────────────────
        self.QWEN_API_KEY = _key("qwen_api_key", "QWEN_API_KEY",
                                  os.getenv("DASHSCOPE_API_KEY", ""))
        # International DashScope endpoint (OpenAI-compatible)
        self.QWEN_BASE_URL = os.getenv(
            "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
        # ADR-035: corrected model IDs from LM Forge benchmark
        # qwen3.5-flash: 8.70 avg (min 6.33) — relay/mechanical only
        self.QWEN_FLASH_MODEL = os.getenv("QWEN_FLASH_MODEL", "qwen3.5-flash-2026-02-23")
        # qwen3.5-plus: 9.63 avg — solid mid-tier STANDARD alternative
        self.QWEN_PLUS_MODEL = os.getenv("QWEN_PLUS_MODEL", "qwen3.5-plus-2026-02-15")
        # qwen3-coder variants (two separate backends: flash=9.30, plus=9.60)
        self.QWEN_CODER_FLASH_MODEL = os.getenv("QWEN_CODER_FLASH_MODEL", "qwen3-coder-flash")
        self.QWEN_CODER_PLUS_MODEL = os.getenv("QWEN_CODER_PLUS_MODEL", "qwen3-coder-plus-2025-07-22")
        # legacy alias kept for backward compat — points to coder-plus
        self.QWEN_CODER_MODEL = os.getenv("QWEN_CODER_MODEL", self.QWEN_CODER_PLUS_MODEL)
        # qwen3.5-122b-a10b: 9.675 avg on standard, 156 t/s, $0.40/$3.20 (ADR-036)
        # MoE model — 122B total, 10B active — fast despite large param count
        self.QWEN_122B_MODEL = os.getenv("QWEN_122B_MODEL", "qwen3.5-122b-a10b")
        # qwen-local routes to LM Studio using the locally loaded Qwen model
        self.QWEN_LOCAL_MODEL = os.getenv("QWEN_LOCAL_MODEL", "Qwen/Qwen3.5-35B-A3B")

        # ── Orchestration Board (ADR-032) ─────────────────────────────────
        self.ORCHESTRATION_BOARD_ENABLED = (
            os.getenv("ORCHESTRATION_BOARD_ENABLED", "true").lower() == "true"
        )
        self.ORCHESTRATION_BOARD_MAX_NUDGES = int(
            os.getenv("ORCHESTRATION_BOARD_MAX_NUDGES", "5")
        )

        # ── Swarm Tools (ADR-033) ─────────────────────────────────────────
        self.SWARM_TOOLS_ENABLED = os.getenv("SWARM_TOOLS_ENABLED", "true").lower() == "true"
        self.SWARM_TOOLS_CODE_RUN_TIMEOUT = int(os.getenv("SWARM_TOOLS_CODE_RUN_TIMEOUT", "30"))
        self.SWARM_TOOLS_LINT_ENABLED = (
            os.getenv("SWARM_TOOLS_LINT_ENABLED", "true").lower() == "true"
        )

        # ── Swarm tier → backend routing (ADR-034 / ADR-035) ─────────────
        # ADR-035 updates defaults based on LM Forge benchmark results:
        #   LITE    → gemini-flash-lite  (9.46 avg, 233 t/s, $0.25/$1.50)
        #   STANDARD → gemini-flash      (9.83 avg, nearly Pro-tier, $0.50/$3.00)
        #   BRAIN/HEAVY → claude         (unchanged, best quality for complex tasks)
        # Override any tier via .env, e.g. SWARM_BACKEND_LITE=qwen-flash-lite
        # Legacy flat tier routing — kept for backward compat, superseded by role chains below
        self.SWARM_BACKEND_LITE = os.getenv("SWARM_BACKEND_LITE", "gemini-flash-lite")
        self.SWARM_BACKEND_STANDARD = os.getenv("SWARM_BACKEND_STANDARD", "gemini-flash")
        self.SWARM_BACKEND_BRAIN = os.getenv("SWARM_BACKEND_BRAIN", "claude")
        self.SWARM_BACKEND_HEAVY = os.getenv("SWARM_BACKEND_HEAVY", "claude")

        # ── Role-based backend chains (ADR-036) ───────────────────────────
        # Each role has a comma-separated ordered list: primary, fallback-1, fallback-2.
        # The gear-shift escalation chain respects this order on failure.
        # Benchmark basis: standard-tier apples-to-apples (4 tasks each, no Sonnet):
        #   Gem Pro 3.1 = Gem Flash 3.0 = Qwen 3.5 Plus: 9.925 avg on standard
        #   Qwen 3.5 122B A10B: 9.675 avg, 156 t/s, $0.40/$3.20
        #   Qwen Coder Plus: 9.775, Haiku: 9.75, Flash Lite: 9.475
        #
        # Session 58: role chains updated to reflect Gemini 3 thinking-level architecture:
        #   flash-high  — HIGH thinking (contract gen, orchestrator, chunker, auditor, fixer)
        #   flash-minimal — MINIMAL thinking (dev/test swarm T1/T2 — fast, mechanical)
        #   gemini-pro-plain — plain Pro adaptive thinking, interview role ONLY (no schema)
        #   gemini-customtools — Pro customtools, schema-compliant, all other pipeline roles
        #
        # Interview  — project intake, requirements clarification
        # gemini-pro-plain: plain Pro (adaptive thinking) only valid here — no schema required
        self.ROLE_INTERVIEW_CHAIN = os.getenv("ROLE_INTERVIEW_CHAIN", "claude,gemini-pro-plain,gemini-flash-high")
        # Planner    — high-level architecture and feature planning
        self.ROLE_PLANNER_CHAIN = os.getenv("ROLE_PLANNER_CHAIN", "gemini-customtools,claude,gemini-flash-high")
        # Orchestrator — subtask decomposition: customtools primary (Flash 3.0 HIGH+schema → 400, S58 log)
        self.ROLE_ORCHESTRATOR_CHAIN = os.getenv("ROLE_ORCHESTRATOR_CHAIN", "gemini-customtools,gemini-flash-high,claude-haiku")
        # Chunker    — breaking large tasks into parallel subtask chunks (same tier as orchestrator)
        self.ROLE_CHUNKER_CHAIN = os.getenv("ROLE_CHUNKER_CHAIN", "gemini-customtools,gemini-flash-high,claude-haiku")
        # Auditor    — Giant Brain post-build quality audit: high thinking for deep analysis
        self.ROLE_AUDITOR_CHAIN = os.getenv("ROLE_AUDITOR_CHAIN", "gemini-customtools,gemini-flash-high,claude")
        # Contract Generator — Phase 0 test contract: customtools primary (Flash 3.0 HIGH+schema non-JSON, S58)
        self.ROLE_CONTRACT_CHAIN = os.getenv("ROLE_CONTRACT_CHAIN", "gemini-customtools,gemini-flash-high,claude")
        # Giant Brain — post-build source audit, FixManifest JSON: customtools primary (reliable JSON, S58)
        self.ROLE_GIANT_BRAIN_CHAIN = os.getenv("ROLE_GIANT_BRAIN_CHAIN", "gemini-customtools,gemini-flash-high,claude")
        # Dev Swarm T1 — lite coding tasks, simple helpers/utilities: MINIMAL thinking
        self.ROLE_DEV_SWARM_T1_CHAIN = os.getenv("ROLE_DEV_SWARM_T1_CHAIN", "gemini-flash-minimal,gemini-flash,claude-haiku")
        # Dev Swarm T2 — standard coding tasks, modules with dependencies: MINIMAL thinking
        self.ROLE_DEV_SWARM_T2_CHAIN = os.getenv("ROLE_DEV_SWARM_T2_CHAIN", "gemini-flash-minimal,gemini-customtools,claude-haiku")
        # Dev Swarm T3 — complex/architectural tasks, cross-module logic
        self.ROLE_DEV_SWARM_T3_CHAIN = os.getenv("ROLE_DEV_SWARM_T3_CHAIN", "claude,gemini-customtools,gemini-flash-high")
        # Test Swarm T1 — generate/run unit tests for simple modules: MINIMAL thinking
        self.ROLE_TEST_SWARM_T1_CHAIN = os.getenv("ROLE_TEST_SWARM_T1_CHAIN", "gemini-flash-minimal,gemini-customtools,claude-haiku")
        # Test Swarm T2 — integration tests, complex assertion logic
        self.ROLE_TEST_SWARM_T2_CHAIN = os.getenv("ROLE_TEST_SWARM_T2_CHAIN", "gemini-customtools,claude,gemini-flash-high")
        # Big Fixer  — post-audit structural repair: customtools (tool calling) + high thinking fallback
        self.ROLE_BIG_FIXER_CHAIN = os.getenv("ROLE_BIG_FIXER_CHAIN", "gemini-customtools,gemini-flash-high,claude")

        # ── Mechanical / relay role chains (ADR-037) ──────────────────────
        # qwen-flash: 8.70 avg (min 6.33) — cheap, fast, mechanical work only.
        # At $0.05/$0.15 and 480 t/s it's purpose-built for high-volume,
        # low-stakes tasks: MCP tool dispatch, context relay, memory read/write.
        #
        # MCP — MCP server calls, tool routing, structured relay/dispatch
        self.ROLE_MCP_CHAIN = os.getenv("ROLE_MCP_CHAIN", "qwen-flash,gemini-flash-lite,claude-haiku")
        # Memory — context summarization, memory read/write, embedding prep
        self.ROLE_MEMORY_CHAIN = os.getenv("ROLE_MEMORY_CHAIN", "qwen-flash,gemini-flash-lite,claude-haiku")

        # ── Workflow profile (ADR-040) ─────────────────────────────────────
        # Selects the end-to-end model chain preset for all roles in one shot.
        # "gemini" (default, battle-tested Sessions 48-53)
        # "claude" (untested — validate before production use)
        # "qwen"   (untested — validate before production use)
        #
        # Priority in get_role_chain():
        #   1. Per-role ROLE_*_CHAIN env var override  (highest — fine-grained control)
        #   2. Active workflow profile chain            (this setting)
        #   3. Hardcoded _ROLE_DEFAULTS in ai_backend  (legacy fallback)
        self.WORKFLOW_PROFILE = os.getenv("WORKFLOW_PROFILE", "gemini")

