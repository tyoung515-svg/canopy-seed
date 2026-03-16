"""
AI Backend
Handles LM Studio (primary), Claude API (escalation), Google Gemini, and an optional worker backend.
Automatic fallback between backends.
"""

import logging
import asyncio
import json
import os
import re
import time
from typing import List, Dict, Optional, Callable

from core.agent_pool import GIANT_BRAIN_SCHEMA

logger = logging.getLogger(__name__)

# --- Claude Escalation Triggers ---
# Keywords/patterns that indicate a message should be routed to Claude API
# instead of the local LM Studio model. These represent tasks where Claude's
# stronger reasoning provides meaningfully better results.
ESCALATION_KEYWORDS = [
    # Complex reasoning and analysis
    "analyze this code", "code review", "security review", "audit this",
    "debug this", "what's wrong with", "find the bug",
    # Multi-step planning
    "create a plan", "design a system", "architect", "roadmap",
    "strategy for", "how should I structure",
    # Long-form writing
    "write a report", "draft a document", "write a proposal",
    "create documentation", "write a spec",
    # Complex technical questions
    "explain the tradeoffs", "compare and contrast",
    "what are the implications", "deep dive into",
]

ESCALATION_PREFIXES = ["!claude ", "!api "]


def should_escalate_to_claude(message: str, settings) -> bool:
    """
    Determine if a message should be routed to Claude API instead of local LM Studio.
    Returns True if escalation is warranted.
    """
    if not settings.CLAUDE_ESCALATION_ENABLED:
        return False
    if not settings.ANTHROPIC_API_KEY:
        return False

    msg_lower = message.lower().strip()

    # Explicit prefix override — user wants Claude
    for prefix in ESCALATION_PREFIXES:
        if msg_lower.startswith(prefix):
            return True

    # Keyword-based escalation
    for keyword in ESCALATION_KEYWORDS:
        if keyword in msg_lower:
            return True

    # Length heuristic — very long prompts with multiple questions benefit from Claude
    if len(message) > 1500 and message.count("?") >= 3:
        return True

    return False


# ── Gemini responseSchema sanitizer ──────────────────────────────────────────
# Gemini's responseSchema is a restricted subset of JSON Schema.
# These standard JSON Schema fields are not supported and cause HTTP 400:
_GEMINI_SCHEMA_UNSUPPORTED = frozenset({
    # JSON Schema meta / reference keywords — not supported
    "$schema", "$id", "$ref", "$defs", "$anchor",
    # Conditional / composition keywords — not supported
    "if", "then", "else",
    "oneOf", "anyOf", "allOf", "not",
    # Unevaluated / dependent — not supported
    "unevaluatedProperties", "unevaluatedItems",
    "dependentSchemas", "dependentRequired",
    # Pattern properties — not supported
    "patternProperties",
    # Content encoding — not supported
    "contentEncoding", "contentMediaType",
    # Annotation-only fields — not supported
    "readOnly", "writeOnly",
    "default",    # silently ignored in some models; causes errors in others
    "examples", "const",
    # Numeric constraints beyond min/max — not supported
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    # String constraints beyond enum/format — not supported
    "minLength", "maxLength", "pattern",
    # Array constraints beyond items/prefixItems/min/maxItems — not supported
    "uniqueItems",
    # Object constraints beyond properties/required/additionalProperties — not supported
    "minProperties", "maxProperties",
    # NOTE: "additionalProperties" IS supported by Gemini structured output and
    # must NOT be stripped — it constrains object schemas correctly.
})

def _sanitize_gemini_schema(schema: dict, *, _inside_properties: bool = False) -> dict:
    """Recursively strip JSON Schema fields that the Gemini API does not support.

    Operates on a copy — never mutates the original schema dict.

    IMPORTANT: When recursing into a ``"properties"`` dict, the child keys are
    property NAMES (e.g. ``"pattern"``, ``"default"``), NOT JSON Schema keywords.
    The ``_inside_properties`` flag prevents stripping those names — only their
    nested values are sanitized with the flag reset to ``False``.
    """
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        # Inside a "properties" dict, keys are user-defined property names —
        # never strip them even if they collide with _GEMINI_SCHEMA_UNSUPPORTED
        # (e.g. a property literally named "pattern" or "default").
        if not _inside_properties and k in _GEMINI_SCHEMA_UNSUPPORTED:
            continue
        if isinstance(v, dict):
            # When k == "properties", its children are property names → set flag
            out[k] = _sanitize_gemini_schema(v, _inside_properties=(k == "properties"))
        elif isinstance(v, list):
            out[k] = [
                _sanitize_gemini_schema(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            out[k] = v

    # Defensive: ensure every entry in "required" references an actual property.
    # Prevents Gemini 400 "property is not defined" if a property was stripped
    # or was never present in the schema to begin with.
    if "required" in out and "properties" in out:
        defined = set(out["properties"].keys())
        out["required"] = [r for r in out["required"] if r in defined]
        if not out["required"]:
            del out["required"]  # empty required array can also cause 400

    return out


from core.exceptions import ToolExhaustionError  # shared across agent_pool ↔ ai_backend


class AIBackend:
    def __init__(self, settings):
        self.settings = settings
        self._claude_client = None
        self._lmstudio_available = None
        self._gemini_pro_degraded = False
        self._gemini_pro_fail_count = 0  # consecutive failures before marking degraded

    def _get_claude_client(self):
        if not self._claude_client and self.settings.ANTHROPIC_API_KEY:
            import anthropic
            import httpx
            self._claude_client = anthropic.AsyncAnthropic(
                api_key=self.settings.ANTHROPIC_API_KEY,
                timeout=httpx.Timeout(90.0),
            )
        return self._claude_client

    async def _check_lmstudio(self) -> bool:
        """Check if LM Studio is running locally"""
        if self._lmstudio_available is not None:
            return self._lmstudio_available
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.settings.LMSTUDIO_BASE_URL}/models", timeout=2)
                self._lmstudio_available = r.status_code == 200
        except Exception:
            self._lmstudio_available = False
        return self._lmstudio_available

    async def complete(
        self,
        system: str,
        message: str,
        history: Optional[List[Dict]] = None,
        backend: Optional[str] = None,
        max_tokens: int = 2048,
        json_mode: bool = False,
        thinking_budget: Optional[int] = None,
        output_schema: Optional[dict] = None,
        response_schema: Optional[dict] = None,
        tools: Optional[List[Dict]] = None,
        tool_dispatcher: Optional[Callable] = None,
        thinking_level: Optional[str] = None,
        tool_mode: str = "AUTO",
        max_tool_rounds: Optional[int] = None,
    ) -> str:
        """
        Get completion from AI backend.
        Priority: explicit backend > escalation check > default > fallback
        """
        backend = backend or self.settings.DEFAULT_AI_BACKEND
        history = history or []

        # Strip extraneous fields like 'timestamp' that break APIs
        clean_history = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in history
            if "role" in msg and "content" in msg
        ]

        # --- Worker model override ---
        if backend == "worker":
            try:
                return await self._worker_complete(system, message, clean_history)
            except Exception as e:
                logger.error(f"Worker model failed: {e}")
                raise RuntimeError(f"Worker model failed.\n\n*Error:*\n`{str(e)}`")

        # --- Gemini override ---
        if backend == "gemini":
            if not self._gemini_pro_degraded:
                try:
                    primary_model = self.settings.GOOGLE_GEMINI_MODEL or "gemini-3.1-pro-preview"
                    result = await self._gemini_complete(
                        system,
                        message,
                        clean_history,
                        json_mode=json_mode,
                        response_schema=response_schema,
                        max_tokens=max_tokens,
                        model=primary_model,
                        tools=tools,
                        tool_dispatcher=tool_dispatcher,
                    )
                    self._gemini_pro_fail_count = 0  # successful — reset consecutive fail streak
                    return result
                except ToolExhaustionError:
                    raise  # propagate directly — Big Fixer handles this separately
                except Exception as e:
                    self._gemini_pro_fail_count += 1
                    logger.warning(
                        "Gemini Pro exhausted after 3 attempts — falling back to Claude Sonnet "
                        f"(consecutive failures: {self._gemini_pro_fail_count})"
                    )
                    # Only mark Pro degraded after 2 consecutive failures — a single
                    # transient error (streaming glitch, brief rate limit) should not
                    # poison the entire session.
                    if self._gemini_pro_fail_count >= 2:
                        self._gemini_pro_degraded = True
                        logger.warning("Gemini Pro marked degraded after %d consecutive failures",
                                       self._gemini_pro_fail_count)
                    gemini_error = e
            else:
                logger.warning(
                    "Gemini Pro marked degraded this session — routing directly to Claude Sonnet"
                )
                gemini_error = RuntimeError("Gemini Pro previously failed this session")

            try:
                if response_schema:
                    return await self._claude_complete(
                        system, message, clean_history,
                        max_tokens=max_tokens,
                        json_mode=True,
                        output_schema=response_schema,
                    )
                return await self._claude_complete(
                    system, message, clean_history,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except Exception as claude_exc:
                logger.error(f"Claude fallback also failed: {claude_exc}")
                raise RuntimeError(
                    f"All providers failed. Gemini error: {gemini_error}. Claude error: {claude_exc}"
                ) from gemini_error

        if backend == "gemini-customtools":
            # Session 58: thinking_level="LOW" prevents thinking-only responses.
            # Without an explicit level, Pro adaptive thinking runs unconstrained and can
            # produce only thought tokens (no text or functionCall), crashing downstream.
            # LOW gives enough reasoning headroom for complex tool calls without over-thinking.
            #
            # Session 58 (declare_done / ANY mode): callers can pass tool_mode="ANY" to force
            # a function call on every turn (making thinking-only physically impossible at the
            # API level). When tool_mode="ANY", thinkingConfig is skipped entirely — this also
            # avoids the MINIMAL+functionDeclarations HTTP 400 seen in the S58 production log.
            #
            # Internal fallback removed: the outer role chain (gemini-customtools →
            # gemini-flash-high → claude) handles escalation cleanly. The old internal
            # fallback was circular after GOOGLE_GEMINI_MODEL was changed to customtools.
            try:
                customtools_model = getattr(
                    self.settings,
                    "GEMINI_CUSTOMTOOLS_MODEL",
                    "gemini-3.1-pro-preview-customtools",
                )
                return await self._gemini_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    json_mode=json_mode,
                    response_schema=response_schema,
                    max_tokens=max_tokens,
                    model=customtools_model,
                    thinking_level=thinking_level or "LOW",
                    tool_mode=tool_mode,
                    tools=tools,
                    tool_dispatcher=tool_dispatcher,
                    max_tool_rounds=max_tool_rounds,
                )
            except ToolExhaustionError:
                raise  # propagate directly — Big Fixer handles this separately from transient failures
            except Exception as e:
                err_text = str(e)
                if hasattr(e, "response") and hasattr(e.response, "text"):
                    err_text += f"\nResponse body: {e.response.text}"
                logger.error(f"Gemini CustomTools failed: {err_text}")
                raise RuntimeError(f"Gemini CustomTools backend failed.\n\n*Error:*\n`{err_text}`")

        if backend == "gemini-pro-plain":
            # Plain Gemini Pro — adaptive thinking, NO responseSchema.
            # Session 58: reserved for interview role only where schema enforcement is not needed.
            # All other pipeline roles use gemini-customtools (adheres to schema).
            plain_model = getattr(
                self.settings,
                "GOOGLE_GEMINI_PLAIN_MODEL",
                "gemini-3.1-pro-preview",
            )
            try:
                return await self._gemini_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    json_mode=json_mode,
                    # Do NOT pass response_schema — plain Pro adaptive thinking + schema = HTTP 400
                    max_tokens=max_tokens,
                    model=plain_model,
                    tools=tools,
                    tool_dispatcher=tool_dispatcher,
                )
            except ToolExhaustionError:
                raise
            except Exception as e:
                err_text = str(e)
                if hasattr(e, "response") and hasattr(e.response, "text"):
                    err_text += f"\nResponse body: {e.response.text}"
                logger.error(f"Gemini Pro Plain failed: {err_text}")
                raise RuntimeError(f"Gemini Pro Plain backend failed.\n\n*Error:*\n`{err_text}`")

        if backend == "gemini-flash":
            try:
                flash_model = getattr(self.settings, "GEMINI_FLASH_MODEL", "gemini-3-flash-preview")
                return await self._gemini_complete(
                    system, message, clean_history,
                    json_mode=json_mode,
                    response_schema=response_schema,
                    max_tokens=max_tokens,
                    model=flash_model,
                    thinking_level="MINIMAL",  # Session 58: MINIMAL is the floor (NONE invalid); explicit level beats omitting
                    temperature=1.0,           # Gemini 3 docs: "keep at default 1.0 — lower values cause looping"
                    tools=tools,
                    tool_dispatcher=tool_dispatcher,
                )
            except ToolExhaustionError:
                raise
            except Exception as e:
                logger.error(f"Gemini Flash failed: {e}")
                raise RuntimeError(f"Gemini Flash backend failed.\n\n*Error:*\n`{str(e)}`")

        if backend == "gemini-flash-lite":
            try:
                # ADR-035: gemini-3.1-flash-lite-preview — 9.46 avg, 233 t/s, $0.25/$1.50
                flash_lite_model = getattr(self.settings, "GEMINI_FLASH_LITE_MODEL", "gemini-3.1-flash-lite-preview")
                return await self._gemini_complete(
                    system, message, clean_history,
                    json_mode=json_mode,
                    response_schema=response_schema,
                    max_tokens=max_tokens,
                    model=flash_lite_model,
                    thinking_level="MINIMAL",  # Session 58: MINIMAL is the floor; flash-lite-3.1 supports MINIMAL
                    temperature=1.0,           # Gemini 3 docs: "keep at default 1.0 — lower values cause looping"
                    tools=tools,
                    tool_dispatcher=tool_dispatcher,
                )
            except ToolExhaustionError:
                raise
            except Exception as e:
                logger.error(f"Gemini Flash Lite failed: {e}")
                raise RuntimeError(f"Gemini Flash Lite backend failed.\n\n*Error:*\n`{str(e)}`")

        if backend == "gemini-flash-minimal":
            try:
                # Reads from settings so model can be overridden without code changes
                flash_model = getattr(self.settings, "GEMINI_FLASH_MODEL", "gemini-3-flash-preview")
                return await self._gemini_complete(
                    system, message, clean_history,
                    json_mode=json_mode,
                    response_schema=response_schema,
                    max_tokens=max_tokens,
                    model=flash_model,
                    thinking_level="MINIMAL",
                    temperature=1.0,           # Gemini 3 docs: "keep at default 1.0 — lower values cause looping"
                    tools=tools,
                    tool_dispatcher=tool_dispatcher,
                )
            except ToolExhaustionError:
                raise
            except Exception as e:
                err_text = str(e)
                if hasattr(e, "response") and hasattr(e.response, "text"):
                    err_text += f"\nResponse body: {e.response.text}"
                logger.error(f"Gemini Flash Minimal failed: {err_text}")
                raise RuntimeError(f"Gemini Flash Minimal backend failed.\n\n*Error:*\n`{err_text}`")

        if backend == "gemini-flash-med":
            try:
                flash_model = getattr(self.settings, "GEMINI_FLASH_MODEL", "gemini-3-flash-preview")
                return await self._gemini_complete(
                    system, message, clean_history,
                    json_mode=json_mode,
                    response_schema=response_schema,
                    max_tokens=max_tokens,
                    model=flash_model,
                    thinking_level="MEDIUM",
                    temperature=1.0,           # Gemini 3 docs: "keep at default 1.0 — lower values cause looping"
                    tools=tools,
                    tool_dispatcher=tool_dispatcher,
                )
            except ToolExhaustionError:
                raise
            except Exception as e:
                logger.error(f"Gemini Flash Med failed: {e}")
                raise RuntimeError(f"Gemini Flash Med backend failed.\n\n*Error:*\n`{str(e)}`")

        if backend == "gemini-flash-high":
            try:
                flash_model = getattr(self.settings, "GEMINI_FLASH_MODEL", "gemini-3-flash-preview")
                return await self._gemini_complete(
                    system, message, clean_history,
                    json_mode=json_mode,
                    response_schema=response_schema,
                    max_tokens=max_tokens,
                    model=flash_model,
                    thinking_level=thinking_level or "HIGH",
                    tool_mode=tool_mode,
                    temperature=1.0,           # Gemini 3 docs: "keep at default 1.0 — lower values cause looping"
                    tools=tools,
                    tool_dispatcher=tool_dispatcher,
                    max_tool_rounds=max_tool_rounds,
                )
            except ToolExhaustionError:
                raise  # propagate directly — Big Fixer handles this separately from transient failures
            except Exception as e:
                err_text = str(e)
                if hasattr(e, "response") and hasattr(e.response, "text"):
                    err_text += f"\nResponse body: {e.response.text}"
                logger.error(f"Gemini Flash High failed: {err_text}")
                raise RuntimeError(f"Gemini Flash High backend failed.\n\n*Error:*\n`{err_text}`")

        if backend == "openai-nano":
            try:
                return await self._openai_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    max_tokens=max_tokens,
                )
            except Exception as e:
                logger.error(f"OpenAI Nano failed: {e}")
                raise RuntimeError(f"OpenAI Nano backend failed.\n\n*Error:*\n`{str(e)}`")

        if backend == "openai-codex":
            try:
                return await self._codex_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    max_tokens=max_tokens,
                )
            except Exception as e:
                err_msg = str(e) or repr(e)
                logger.error(f"OpenAI Codex failed: {err_msg}")
                raise RuntimeError(f"OpenAI Codex backend failed.\n\n*Error:*\n`{err_msg}`")

        # --- Qwen backends (ADR-034) ---
        if backend == "qwen-flash":
            try:
                return await self._qwen_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    max_tokens=max_tokens,
                    model=getattr(self.settings, "QWEN_FLASH_MODEL", "qwen3.5-flash"),
                )
            except Exception as e:
                err_msg = str(e) or repr(e)
                logger.error(f"Qwen Flash failed: {err_msg}")
                raise RuntimeError(f"Qwen Flash backend failed.\n\n*Error:*\n`{err_msg}`")

        if backend == "qwen-plus":
            try:
                return await self._qwen_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    max_tokens=max_tokens,
                    model=getattr(self.settings, "QWEN_PLUS_MODEL", "qwen3.5-plus"),
                )
            except Exception as e:
                err_msg = str(e) or repr(e)
                logger.error(f"Qwen Plus failed: {err_msg}")
                raise RuntimeError(f"Qwen Plus backend failed.\n\n*Error:*\n`{err_msg}`")

        if backend == "qwen-coder":
            # Legacy alias — routes to qwen-coder-plus (9.60 avg, $1.00/$5.00)
            try:
                return await self._qwen_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    max_tokens=max_tokens,
                    model=getattr(self.settings, "QWEN_CODER_MODEL", "qwen3-coder-plus-2025-07-22"),
                )
            except Exception as e:
                err_msg = str(e) or repr(e)
                logger.error(f"Qwen Coder failed: {err_msg}")
                raise RuntimeError(f"Qwen Coder backend failed.\n\n*Error:*\n`{err_msg}`")

        if backend == "qwen-coder-flash":
            # ADR-035: 9.30 avg, 106 t/s, $0.30/$1.50 — faster/cheaper coder
            try:
                return await self._qwen_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    max_tokens=max_tokens,
                    model=getattr(self.settings, "QWEN_CODER_FLASH_MODEL", "qwen3-coder-flash"),
                )
            except Exception as e:
                err_msg = str(e) or repr(e)
                logger.error(f"Qwen Coder Flash failed: {err_msg}")
                raise RuntimeError(f"Qwen Coder Flash backend failed.\n\n*Error:*\n`{err_msg}`")

        if backend == "qwen-coder-plus":
            # ADR-035: 9.60 avg, 76 t/s, $1.00/$5.00 — higher quality coder
            try:
                return await self._qwen_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    max_tokens=max_tokens,
                    model=getattr(self.settings, "QWEN_CODER_PLUS_MODEL", "qwen3-coder-plus-2025-07-22"),
                )
            except Exception as e:
                err_msg = str(e) or repr(e)
                logger.error(f"Qwen Coder Plus failed: {err_msg}")
                raise RuntimeError(f"Qwen Coder Plus backend failed.\n\n*Error:*\n`{err_msg}`")

        if backend == "qwen-122b":
            # ADR-036: qwen3.5-122b-a10b — 9.675 avg on standard tier, 156 t/s, $0.40/$3.20
            # MoE model (122B total / 10B active) — fast due to sparse activation
            try:
                return await self._qwen_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    max_tokens=max_tokens,
                    model=getattr(self.settings, "QWEN_122B_MODEL", "qwen3.5-122b-a10b"),
                )
            except Exception as e:
                err_msg = str(e) or repr(e)
                logger.error(f"Qwen 122B failed: {err_msg}")
                raise RuntimeError(f"Qwen 122B backend failed.\n\n*Error:*\n`{err_msg}`")

        if backend == "qwen-local":
            # Routes to LM Studio with the locally loaded Qwen model
            try:
                local_model = getattr(self.settings, "QWEN_LOCAL_MODEL", "Qwen/Qwen3.5-35B-A3B")
                return await self._lmstudio_complete(
                    system=system,
                    message=message,
                    history=clean_history,
                    max_tokens=max_tokens,
                    model_id=local_model,
                )
            except Exception as e:
                err_msg = str(e) or repr(e)
                logger.error(f"Qwen Local (LM Studio) failed: {err_msg}")
                raise RuntimeError(f"Qwen Local backend failed.\n\n*Error:*\n`{err_msg}`")

        # --- Auto-escalation check ---
        if backend == "lmstudio" and should_escalate_to_claude(message, self.settings):
            logger.info("Auto-escalating to Claude API based on message content")
            backend = "claude"
            # Strip the !claude / !api prefix if present
            for prefix in ESCALATION_PREFIXES:
                if message.lower().startswith(prefix):
                    message = message[len(prefix):].strip()
                    break

        # --- Claude primary ---
        if backend == "claude":
            try:
                return await self._claude_complete(
                    system, message, clean_history, max_tokens,
                    json_mode=json_mode,
                    thinking_budget=thinking_budget,
                    output_schema=output_schema,
                )
            except Exception as e1:
                logger.warning(f"Claude failed, falling back to LM Studio: {e1}")
                try:
                    fallback_response = await self._lmstudio_complete(
                        system,
                        message,
                        clean_history,
                        max_tokens=max_tokens,
                    )
                    # Prepend a visible warning so the user knows they're NOT talking to Claude.
                    return (
                        f"⚠️ *Claude failed — responded via LM Studio instead.*\n"
                        f"_Error: {e1}_\n\n"
                        f"{fallback_response}"
                    )
                except Exception as e2:
                    logger.error(f"Fallback to LM Studio also failed: {e2}")
                    raise RuntimeError(
                        f"All AI backends failed.\n\n"
                        f"*Claude Error:*\n`{str(e1)}`\n\n"
                        f"*LM Studio Error:*\n`{str(e2)}`"
                    )

        if backend == "claude-haiku":
            try:
                return await self._claude_complete(
                    system,
                    message,
                    clean_history,
                    max_tokens=max_tokens,
                    model="claude-haiku-4-5-20251001",
                    json_mode=json_mode,
                    thinking_budget=thinking_budget,
                    output_schema=output_schema,
                )
            except Exception as e:
                logger.error(f"Claude Haiku failed: {e}")
                raise RuntimeError(f"Claude Haiku backend failed.\n\n*Error:*\n`{str(e)}`")

        if backend == "claude-audit":
            # Primary: Claude with structured output + thinking budget.
            # Fallback: Gemini Pro with json_mode — used when Claude credits
            # are exhausted (400) or otherwise unavailable.
            try:
                return await self._claude_complete(
                    system,
                    message,
                    clean_history,
                    max_tokens=max_tokens,
                    json_mode=True,
                    thinking_budget=2048,
                    output_schema=GIANT_BRAIN_SCHEMA,
                )
            except Exception as claude_exc:
                logger.error(f"Claude Audit failed: {claude_exc}")
                logger.warning("Claude Audit failed — falling back to Gemini Pro for Giant Brain audit")
                try:
                    return await self._gemini_complete(
                        system,
                        message,
                        clean_history,
                        max_tokens=max_tokens,
                        json_mode=True,
                        response_schema=GIANT_BRAIN_SCHEMA,
                    )
                except Exception as gemini_exc:
                    logger.error(f"Gemini Audit fallback also failed: {gemini_exc}")
                    raise RuntimeError(
                        f"Claude Audit backend failed.\n\n*Error:*\n`{str(claude_exc)}`\n\n"
                        f"Gemini fallback also failed: `{str(gemini_exc)}`"
                    )

        # --- LM Studio primary (default) ---
        try:
            return await self._lmstudio_complete(system, message, clean_history, max_tokens=max_tokens)
        except Exception as e1:
            logger.warning(f"LM Studio failed, falling back to Claude: {e1}", exc_info=True)
            try:
                return await self._claude_complete(
                    system, message, clean_history, max_tokens,
                    json_mode=json_mode,
                    thinking_budget=thinking_budget,
                    output_schema=output_schema,
                )
            except Exception as e2:
                logger.error(f"Fallback to Claude also failed: {e2}")
                raise RuntimeError(
                    f"All AI backends failed.\n\n"
                    f"*LM Studio Error:*\n`{str(e1)}`\n\n"
                    f"*Claude Error:*\n`{str(e2)}`"
                )

    # -----------------------------------------------------------------
    # Backend implementations
    # -----------------------------------------------------------------

    async def _claude_complete(
        self, system: str, message: str,
        history: List[Dict], max_tokens: int = 2048,
        model: Optional[str] = None,
        json_mode: bool = False,
        thinking_budget: Optional[int] = None,
        output_schema: Optional[dict] = None,
    ) -> str:
        client = self._get_claude_client()
        if not client:
            raise ValueError("Claude API key not configured")

        # Enforce strict user/assistant alternation required by the Anthropic API.
        # Merge consecutive same-role messages so a bad history doesn't crash the call.
        raw_messages = history + [{"role": "user", "content": message}]
        messages: List[Dict] = []
        for msg in raw_messages:
            if messages and messages[-1]["role"] == msg["role"]:
                # Merge by appending content with a separator
                messages[-1]["content"] += f"\n\n{msg['content']}"
            else:
                messages.append({"role": msg["role"], "content": msg["content"]})

        call_kwargs = {
            "model": model or self.settings.CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }

        # output_schema and json_mode enforced via system prompt only.
        # The Anthropic streaming SDK rejects that request field with HTTP 400.

        if thinking_budget is not None:
            if thinking_budget <= 0:
                call_kwargs["thinking"] = {"type": "adaptive"}
            else:
                # Claude requires minimum 1024 budget tokens for 'enabled' type
                call_kwargs["thinking"] = {"type": "enabled", "budget_tokens": max(1024, thinking_budget)}

        async with client.messages.stream(**call_kwargs) as stream:
            if hasattr(stream, "get_final_message"):
                message_obj = await asyncio.wait_for(stream.get_final_message(), timeout=90.0)
                text = next((b.text for b in message_obj.content if hasattr(b, "text") and b.text), None)
                if text:
                    return text
                block_summary = [(getattr(b, "type", "unknown"), bool(getattr(b, "text", None))) for b in message_obj.content]
                logger.warning(f"Claude text extraction failed. Content blocks: {block_summary}")
                if "thinking" in call_kwargs:
                    logger.warning("Thinking-only response detected — retrying without thinking budget")
                    retry_kwargs = {k: v for k, v in call_kwargs.items() if k != "thinking"}
                    async with client.messages.stream(**retry_kwargs) as retry_stream:
                        retry_message = await asyncio.wait_for(retry_stream.get_final_message(), timeout=90.0)
                        text = next((b.text for b in retry_message.content if hasattr(b, "text") and b.text), None)
                        if text:
                            return text
                raise RuntimeError("Claude returned no text content from streaming response")
            else:
                text = await stream.get_final_text()

        if text:
            return text

        raise RuntimeError("Claude returned no text content from streaming response")

    async def _codex_complete(
        self,
        system: str,
        message: str,
        history: List[Dict],
        max_tokens: int = 16384,
        reasoning_effort: Optional[str] = None,
    ) -> str:
        """Call GPT-5.2-codex via the OpenAI Responses API.

        Uses /v1/responses (not /v1/chat/completions) because `reasoning: {effort}`
        is only supported on the Responses API. Temperature is intentionally omitted —
        it is not allowed when reasoning effort is set to anything other than 'none'.
        """
        import httpx

        api_key = getattr(self.settings, "OPENAI_API_KEY", "") or ""
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")

        model = getattr(self.settings, "OPENAI_CODEX_MODEL", "gpt-5.3-codex")
        effort = reasoning_effort or getattr(self.settings, "CODEX_REASONING_EFFORT", "medium")

        # Responses API uses a flat `input` list (not `messages`)
        input_items: List[dict] = []
        if system:
            input_items.append({"role": "system", "content": system})
        for msg in history:
            input_items.append({"role": msg["role"], "content": msg["content"]})
        input_items.append({"role": "user", "content": message})

        payload = {
            "model": model,
            "input": input_items,
            "reasoning": {"effort": effort},
            "max_output_tokens": max_tokens,
        }

        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        # Responses API: output is a list of items; find the first message output
        output_items = data.get("output", [])
        for item in output_items:
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text = part.get("text", "").strip()
                        if text:
                            return text
            # Flat text field on some response shapes
            if item.get("type") == "text":
                text = item.get("text", "").strip()
                if text:
                    return text

        raise RuntimeError(f"OpenAI Codex returned no usable text. Output items: {[i.get('type') for i in output_items]}")

    async def _qwen_complete(
        self,
        system: str,
        message: str,
        history: List[Dict],
        max_tokens: int = 16384,
        model: Optional[str] = None,
    ) -> str:
        """Call Qwen models via Alibaba DashScope OpenAI-compatible endpoint (ADR-034).

        Supports qwen3.5-flash, qwen3.5-plus, and qwen3-coder.
        Uses the international DashScope endpoint by default; override QWEN_BASE_URL
        for China region or third-party proxy (e.g. OpenRouter).

        Thinking mode: Qwen3.5 models support /think and /no-think suffixes in the
        first user message, or the enable_thinking parameter. We use enable_thinking=False
        for standard codegen calls to keep latency low; set enable_thinking=True via
        model suffix "qwen3.5-plus-think" if desired.
        """
        api_key = getattr(self.settings, "QWEN_API_KEY", "") or ""
        if not api_key:
            raise ValueError(
                "QWEN_API_KEY not configured. Set QWEN_API_KEY or DASHSCOPE_API_KEY in .env"
            )

        base_url = getattr(
            self.settings, "QWEN_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
        resolved_model = model or getattr(self.settings, "QWEN_FLASH_MODEL", "qwen3.5-flash")

        # Check for -think suffix to enable thinking mode
        enable_thinking = False
        if resolved_model.endswith("-think"):
            resolved_model = resolved_model[:-6]
            enable_thinking = True

        messages: List[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": message})

        payload: dict = {
            "model": resolved_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        # DashScope-specific: enable/disable thinking via extra_body param
        if enable_thinking:
            payload["extra_body"] = {"enable_thinking": True}

        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code != 200:
                body = response.text[:500]
                raise RuntimeError(
                    f"Qwen API returned {response.status_code}: {body}"
                )
            data = response.json()

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"Qwen API returned no choices. Response: {data}")

        content = choices[0].get("message", {}).get("content", "")
        # Thinking models may return a <think>...</think> block before the answer.
        # Strip it so downstream consumers only see the actual code/text output.
        if "<think>" in content and "</think>" in content:
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

        if not content:
            raise RuntimeError(f"Qwen returned empty content. Full response: {data}")

        return content


    async def _openai_complete(
        self,
        system: str,
        message: str,
        history: List[Dict],
        max_tokens: int = 2048,
    ) -> str:
        api_key = getattr(self.settings, "OPENAI_API_KEY", "") or ""
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")

        model = getattr(self.settings, "OPENAI_MODEL", "gpt-5-nano")
        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.4,
            )
            content = (response.choices[0].message.content or "").strip() if response.choices else ""
            if not content:
                raise RuntimeError("OpenAI returned empty content")
            return content
        except ImportError:
            import httpx

            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": 0.4,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                content = (
                    payload.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                if not content:
                    raise RuntimeError("OpenAI returned empty content")
                return content

    async def _lmstudio_ensure_model(self, model_id: str) -> None:
        """Ensure model_id is the only model loaded in LM Studio.
        Unloads any other loaded models first, then loads the requested one.
        Uses the /api/v0 management API (LM Studio 0.3+).
        Silently skips if the v0 API isn't available or model_id is empty.
        """
        if not model_id:
            return
        import httpx
        from urllib.parse import urlparse
        parsed = urlparse(self.settings.LMSTUDIO_BASE_URL)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        v0 = f"{origin}/api/v0"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                # Get list of currently loaded models
                r = await client.get(f"{v0}/models", timeout=10)
                loaded_ids = []
                if r.status_code == 200:
                    loaded_ids = [m.get("id", "") for m in r.json().get("data", [])]

                # Unload everything that isn't our target
                for loaded_id in loaded_ids:
                    if loaded_id != model_id:
                        logger.info(f"LM Studio: unloading {loaded_id}")
                        ru = await client.post(
                            f"{v0}/models/unload",
                            json={"model": loaded_id},
                            timeout=30,
                        )
                        if ru.status_code not in (200, 201):
                            logger.warning(f"LM Studio unload {loaded_id} returned {ru.status_code}: {ru.text[:200]}")

                # Already loaded — nothing more to do
                if model_id in loaded_ids:
                    logger.debug(f"LM Studio: {model_id} already loaded")
                    return

                # Load the requested model
                logger.info(f"LM Studio: loading model {model_id}")
                r2 = await client.post(
                    f"{v0}/models/load",
                    json={"model": model_id},
                    timeout=120,
                )
                if r2.status_code not in (200, 201):
                    logger.warning(f"LM Studio model load returned {r2.status_code}: {r2.text[:200]}")
                else:
                    logger.info(f"LM Studio: {model_id} loaded successfully")
        except Exception as e:
            logger.warning(f"LM Studio _ensure_model skipped: {e}")

    async def _lmstudio_complete(
        self, system: str, message: str, history: List[Dict], model_id: str = "", max_tokens: int = 4096
    ) -> str:
        import httpx

        # Build messages in OpenAI format for LM Studio
        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        logger.debug(
            f"LM Studio request: model={model_id or self.settings.LMSTUDIO_MODEL}, "
            f"messages={len(messages)}, last_user_msg={messages[-1]['content'][:100]}..."
        )

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.settings.LMSTUDIO_BASE_URL}/chat/completions",
                json={
                    "model": model_id or self.settings.LMSTUDIO_MODEL,
                    "messages": messages,
                    "temperature": 0.4,
                    "max_tokens": max_tokens,
                    "stream": False
                }
            )
            response.raise_for_status()
            response_json = response.json()
            raw_json = response_json["choices"][0]["message"]
            raw_content = raw_json.get("content") or ""
            logger.debug(f"LM Studio raw response ({len(raw_content)} chars): {raw_content[:200]}...")

            if "tool_calls" in raw_json and raw_json["tool_calls"]:
                logger.info(f"LM Studio response contains native tool_calls: {raw_json['tool_calls']}")

            content = raw_content

            if not content.strip() and "tool_calls" in raw_json and raw_json["tool_calls"]:
                logger.info("Model returned empty content with tool_calls — extracting from tool_calls")
                tool_calls = raw_json["tool_calls"]
                parts = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "unknown")
                    fn_args = fn.get("arguments", "{}")
                    parts.append(f"Tool call: {fn_name}({fn_args})")
                content = "\n".join(parts)
                logger.debug(f"Reconstructed content from tool_calls: {content[:200]}...")

        # --- Post-processing for LM Studio quirks ---

        # Strip internal reasoning from distill models.
        # Models like qwen3-reasoning wrap thinking in <think>...</think>.
        # Split on first closing tag; take everything after it.
        if "</think>" in content:
            content = content.split("</think>", 1)[-1]

        # Remove any remaining (unclosed) <think> blocks non-greedily so we
        # don't accidentally eat content that follows a closing tag.
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
        # If model output an unclosed <think> with no closing tag, strip from there on.
        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL)
        content = content.strip()
        logger.debug(f"LM Studio after think-strip ({len(content)} chars): {content[:200]}...")

        # If stripping think blocks left us with nothing, log and raise so
        # the caller can fall back to Claude instead of silently returning empty.
        if not content:
            logger.warning("LM Studio response was empty after stripping <think> blocks — falling back")
            raise RuntimeError("LM Studio returned empty response after think-block stripping")

        # Strip trailing JSON structure if the model hallucinated it
        tool_calls_match = re.search(r'\",\s*"tool_calls":', content)
        if tool_calls_match:
            content = content[:tool_calls_match.start()]

        # Unescape literal "\n" and "\"" if the model generated a JSON-encoded string
        if "\\n" in content:
            content = content.replace("\\n", "\n").replace("\\\"", "\"")
        if content.endswith("\""):
            content = content[:-1]
        if content.startswith("\""):
            content = content[1:]

        content = content.strip()
        logger.debug(f"LM Studio final response ({len(content)} chars): {content[:200]}...")
        return content

    async def _gemini_complete(
        self,
        system: str,
        message: str,
        history: List[Dict],
        json_mode: bool = False,
        response_schema: Optional[dict] = None,
        max_tokens: int = 2048,
        model: str = None,
        suppress_thinking: bool = False,
        thinking_level: Optional[str] = None,
        tool_mode: str = "AUTO",
        tools: Optional[List[Dict]] = None,
        tool_dispatcher: Optional[Callable] = None,
        temperature: float = 1.0,
        max_tool_rounds: Optional[int] = None,
    ) -> str:
        """Call Google Gemini API via REST (no SDK dependency needed)."""
        api_key = getattr(self.settings, "GOOGLE_API_KEY", "") or getattr(self.settings, "GEMINI_API_KEY", "") or ""
        if not api_key:
            raise ValueError("GOOGLE_API_KEY not configured")
        max_tokens = min(max_tokens, 65536)

        import httpx
        gemini_model = model or self.settings.GOOGLE_GEMINI_MODEL

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{gemini_model}:generateContent"
            f"?key={api_key}"
        )

        payload = {
            "systemInstruction": {
                "parts": [{"text": system}]
            },
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
        }

        if suppress_thinking or tool_mode == "ANY":
            # Omit thinkingConfig entirely — "NONE" is not a valid thinkingLevel;
            # MINIMAL is the floor. Omitting lets the model adapt (no thinking tokens
            # for flash/flash-lite in practice) without triggering an API 400.
            #
            # tool_mode="ANY": also skip thinkingConfig — ANY mode forces a function call
            # on every turn so thinking-only is physically impossible.  Additionally,
            # MINIMAL+functionDeclarations = HTTP 400 on customtools models (S58 prod log).
            pass
        elif thinking_level:
            # Note: Gemini 3.0 API uses thinkingLevel natively
            payload["generationConfig"]["thinkingConfig"] = {
                "thinkingLevel": thinking_level,
            }

        if tools:
            # Gemini functionDeclarations use "parameters" (OpenAI-style schema).
            # TOOL_DEFINITIONS use "input_schema" (MCP/Claude-style). Remap here.
            def _to_gemini_decl(t: dict) -> dict:
                return {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    # Finding 3: sanitize parameter schemas — unsupported JSON Schema
                    # fields (default, examples, minLength, pattern, etc.) can cause
                    # silent rejection or misinterpretation by the Gemini API.
                    "parameters": _sanitize_gemini_schema(
                        t.get("parameters") or t.get("input_schema", {})
                    ),
                }
            payload["tools"] = [{"functionDeclarations": [_to_gemini_decl(t) for t in tools]}]
            payload["toolConfig"] = {
                # tool_mode="ANY" forces a function call on every turn.  The model
                # CAN still call declare_done under ANY (it IS a declared function) —
                # it just tends to keep working instead.  allowedFunctionNames is used
                # on final rounds to force termination (see rounds-remaining nudge).
                # Must use camelCase — snake_case fields are silently ignored by the REST API.
                "functionCallingConfig": {"mode": tool_mode}
            }

        if json_mode and tools:
            logger.warning(
                "_gemini_complete: json_mode and tools are mutually exclusive — "
                "json_mode suppressed when tools are provided"
            )
        elif json_mode:
            # Session 57 fix: must be camelCase — snake_case silently ignored by Gemini REST API
            payload["generationConfig"]["responseMimeType"] = "application/json"

        if response_schema is not None:
            # Always apply responseJsonSchema when provided.
            # All standard pipeline models (customtools, flash with explicit thinking_level) support this.
            # The plain gemini-pro-preview (interview role only) never receives response_schema.
            # responseJsonSchema is the correct key for standard JSON Schema (lowercase types).
            # responseMimeType must also be set for schema enforcement to activate.
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseJsonSchema"] = _sanitize_gemini_schema(response_schema)

        stream_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{gemini_model}:streamGenerateContent"
            f"?alt=sse&key={api_key}"
        )

        # Build mutable contents list for multi-turn tool conversation
        contents = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        contents.append({"role": "user", "parts": [{"text": message}]})

        MAX_TOOL_ROUNDS = max_tool_rounds or 20  # default 20 rounds (0..19)
        # Callers can pass max_tool_rounds to lower the budget.  Big Fixer uses
        # 12 rounds: enough for 2 full read→write→test cycles, with the 2-attempt
        # retry at the agent_pool level providing a fresh context reset.  The extra
        # rounds (20) are kept as default for Giant Brain and other callers.
        tool_round = 0
        last_exc = None
        _context_recovery_done = False  # one-shot 400 recovery per tool session
        _empty_response_retry_done = False  # one-shot retry for 200-but-empty-parts
        _test_fail_streak = 0  # Finding 3: consecutive run_test calls that returned failures
        # Finding 1: ceiling timeout for any single tool call.
        # Each tool has its own internal timeout (run_test=120s, etc.), but if a tool
        # hangs *before* reaching its internal timeout (e.g. pip stall, blocked DNS),
        # this outer ceiling ensures the loop always makes forward progress.
        _TOOL_DISPATCH_TIMEOUT = 180  # 3 minutes

        # Skip streaming when responseSchema is set: Gemini must buffer the entire
        # JSON response before emitting anything, so streamGenerateContent gives zero
        # benefit and risks a read-timeout on large outputs (e.g. Phase 0 contracts).
        # Go straight to blocking generateContent for these calls.
        _skip_streaming = response_schema is not None

        while tool_round < MAX_TOOL_ROUNDS:  # Finding 6: < not <=, so rounds 0..13 = 14 total (was 0..14 = 15)
            # Update payload contents for this turn
            payload["contents"] = contents

            # --- attempt streaming, fall back to blocking ---
            raw_parts = []  # list of part dicts from this turn
            streamed_ok = False

            for attempt in range(3):
                # ── Streaming attempt (skipped when responseSchema is set) ──────
                # responseSchema forces Gemini to buffer the entire JSON response
                # before emitting any SSE events, so streaming provides zero
                # benefit and risks a read-timeout on large outputs.  Skip it and
                # go straight to blocking generateContent for those calls.
                if not _skip_streaming:
                    try:
                        # Pro 3.1 with thinking can legitimately take 3-5 minutes on
                        # complex prompts (orchestrator decompose, giant brain audit).
                        # The per-read timeout (read=120) catches true stalls where the
                        # server stops sending data entirely. Do NOT add a ceiling timeout
                        # here — it kills legitimate long-thinking requests.
                        timeout = httpx.Timeout(connect=30, read=120, write=30, pool=30)
                        async with httpx.AsyncClient(timeout=timeout) as client:
                            async with client.stream("POST", stream_url, json=payload) as response:
                                response.raise_for_status()
                                async for raw_line in response.aiter_lines():
                                    line = (raw_line or "").strip()
                                    if not line or line == "[DONE]":
                                        continue
                                    if line.startswith("data:"):
                                        line = line[5:].strip()
                                    if not line:
                                        continue
                                    try:
                                        event_data = json.loads(line)
                                    except json.JSONDecodeError:
                                        continue
                                    for candidate in event_data.get("candidates", []):
                                        for part in candidate.get("content", {}).get("parts", []):
                                            raw_parts.append(part)
                        streamed_ok = True
                        break
                    except Exception as stream_exc:
                        logger.warning(
                            f"Gemini streaming failed (attempt {attempt + 1}/3): {stream_exc}"
                        )
                        last_exc = stream_exc

                # ── Blocking fallback (always used when streaming skipped/failed) ─
                # 180s: thinking-enabled Pro 3.1 models can take >90s for complex tool tasks
                # or when buffering large JSON outputs like Phase 0 contracts.
                try:
                    async with httpx.AsyncClient(timeout=180) as client:
                        response = await client.post(url, json=payload)
                        response.raise_for_status()
                        data = response.json()
                    for candidate in data.get("candidates", []):
                        for part in candidate.get("content", {}).get("parts", []):
                            raw_parts.append(part)
                    streamed_ok = True
                    break
                except Exception as block_exc:
                    last_exc = block_exc
                    # Log 400 body — helps diagnose stale thoughtSignature vs context-too-large
                    if (
                        isinstance(block_exc, httpx.HTTPStatusError)
                        and block_exc.response.status_code == 400
                    ):
                        try:
                            _err_body = block_exc.response.text[:600]
                            logger.error(
                                f"Gemini 400 error body (tool round {tool_round}): {_err_body}"
                            )
                        except Exception:
                            pass
                    is_server_error = (
                        isinstance(block_exc, httpx.HTTPStatusError)
                        and block_exc.response.status_code in (429, 500, 502, 503)
                    )
                    if is_server_error and attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    break

            if not streamed_ok or not raw_parts:
                # ── Empty-response retry (200 OK but no parts) ─────────────────
                # Flash occasionally returns a well-formed 200 with zero candidate
                # parts — a transient API hiccup.  One brief sleep + retry before
                # giving up.  Only fires when there is no HTTP error (last_exc is
                # None or not an HTTPStatusError).
                _is_empty_200 = (
                    streamed_ok
                    and not raw_parts
                    and not (
                        isinstance(last_exc, httpx.HTTPStatusError)
                    )
                )
                if _is_empty_200 and not _empty_response_retry_done:
                    _empty_response_retry_done = True
                    logger.warning(
                        f"Gemini returned 200 with no parts at round {tool_round} — "
                        f"retrying after 2s"
                    )
                    await asyncio.sleep(2)
                    streamed_ok = False
                    raw_parts = []
                    last_exc = None
                    continue

                # Context recovery: on a mid-loop 400, strip all thoughtSignatures
                # and trim more aggressively, then retry the current round once.
                # This handles stale-thoughtSignature and context-too-large 400s.
                _is_400 = (
                    isinstance(last_exc, httpx.HTTPStatusError)
                    and last_exc.response.status_code == 400
                )
                if _is_400 and not _context_recovery_done and tool_round > 0:
                    logger.warning(
                        f"Gemini tool loop 400 at round {tool_round} — "
                        f"attempting context recovery (strip all thoughts, trim to 2 rounds)"
                    )
                    _context_recovery_done = True
                    # Strip ALL thought/thoughtSignature AND functionCall from every
                    # older model turn (thought+thoughtSignature+functionCall must
                    # travel together — stripping the signature while leaving
                    # functionCall causes "missing thought_signature" 400).
                    # Then prune any immediately following tool-result turns whose
                    # preceding functionCall was just removed (orphaned results also
                    # cause Gemini 400s).
                    cleaned: list = []
                    _skip_next_tool_result = False
                    for _ce in contents:
                        if _skip_next_tool_result:
                            # Check if this is the tool-result turn that follows a
                            # now-empty model turn (role=="user" with functionResponse)
                            _parts = _ce.get("parts", [])
                            if _ce.get("role") == "user" and all(
                                "functionResponse" in _p for _p in _parts if _p
                            ):
                                _skip_next_tool_result = False
                                continue  # drop this orphaned tool-result turn
                            _skip_next_tool_result = False
                        if _ce.get("role") == "model":
                            new_parts = [
                                _p for _p in _ce.get("parts", [])
                                if "thought" not in _p
                                and "thoughtSignature" not in _p
                                and "functionCall" not in _p
                            ]
                            _had_fc = any(
                                "functionCall" in _p for _p in _ce.get("parts", [])
                            )
                            _ce = dict(_ce)
                            _ce["parts"] = new_parts
                            if _had_fc:
                                # The following tool-result turn is now orphaned
                                _skip_next_tool_result = True
                        cleaned.append(_ce)
                    contents = cleaned
                    # Aggressive trim — keep only 2 round-pairs after the seed message
                    _init_len = len(history) + 1
                    _recovery_max = _init_len + 2 * 2
                    if len(contents) > _recovery_max:
                        contents = contents[:_init_len] + contents[-(2 * 2):]
                    payload["contents"] = contents
                    raw_parts = []
                    streamed_ok = False
                    last_exc = None
                    continue  # retry current round without incrementing tool_round
                if last_exc:
                    raise last_exc
                raise RuntimeError("Gemini returned no parts")

            # --- classify what we got ---
            text_parts = [p.get("text", "") for p in raw_parts if "text" in p and p.get("text")]
            thought_parts = [p.get("thought", "") for p in raw_parts if "thought" in p]
            function_calls = [p.get("functionCall") for p in raw_parts if "functionCall" in p]

            # If no function calls, we're done — return the text
            if not function_calls:
                final_text = "".join(text_parts).strip()
                if not final_text:
                    # Finding 4: with tool_mode="ANY" Gemini should always emit a function
                    # call, but edge cases exist (safety refusal, overloaded API returning a
                    # partial response that consists only of thought parts).  Retry once
                    # before raising — the retry reuses _empty_response_retry_done so we
                    # don't burn an extra slot if this follows the 200-empty-parts path.
                    if not _empty_response_retry_done:
                        _empty_response_retry_done = True
                        logger.warning(
                            f"_gemini_complete: thinking-only response at round {tool_round} "
                            f"(tool_mode={tool_mode!r}) — retrying this round once"
                        )
                        continue  # retry without incrementing tool_round
                    raise RuntimeError(
                        "Gemini response has no text and no functionCall parts "
                        "(thinking-only or empty response; single retry exhausted)"
                    )
                return final_text

            # --- we have function calls: must dispatch and continue ---

            # Session 58: intercept declare_done BEFORE dispatching — it's a termination
            # signal, not a real tool call.  Return its summary as the final completion text.
            for fc in function_calls:
                if fc.get("name") == "declare_done":
                    summary = (fc.get("args") or {}).get("summary", "")
                    logger.info(
                        f"_gemini_complete: declare_done received — "
                        f"summary: {summary[:200]}"
                    )
                    return summary or "done"

            if tool_dispatcher is None:
                # No dispatcher provided — return text if any, else raise
                logger.warning(
                    "_gemini_complete: Gemini returned functionCall but no tool_dispatcher provided"
                )
                fallback_text = "".join(text_parts).strip()
                if fallback_text:
                    return fallback_text
                raise RuntimeError(
                    "Gemini returned functionCall but no tool_dispatcher was provided"
                )

            # Append the model's turn (with thought + functionCall) to contents.
            # CRITICAL: echo each Part EXACTLY as received — thoughtSignature and
            # functionCall live on the SAME Part in Gemini 3.x.  Splitting them
            # into separate Parts causes "missing thought_signature" 400 errors.
            # Rule: "Always send the thought_signature back inside its original Part."
            model_parts = []
            for p in raw_parts:
                echoed: dict = {}
                if "thought" in p:
                    echoed["thought"] = p["thought"]
                if "thoughtSignature" in p:
                    echoed["thoughtSignature"] = p["thoughtSignature"]
                if "functionCall" in p:
                    echoed["functionCall"] = p["functionCall"]
                if "text" in p and p["text"]:
                    echoed["text"] = p["text"]
                if echoed:
                    model_parts.append(echoed)
            contents.append({"role": "model", "parts": model_parts})

            # Dispatch each function call and build functionResponse parts
            response_parts = []
            for fc in function_calls:
                fn_name = fc.get("name", "")
                fn_args = fc.get("args", {})
                logger.debug(f"Gemini native tool call: {fn_name}({fn_args})")
                try:
                    # Finding 1: wrap with ceiling timeout so a hung tool can never
                    # block the loop indefinitely (e.g. pip install stall, DNS hang).
                    result = await asyncio.wait_for(
                        tool_dispatcher(fn_name, fn_args),
                        timeout=_TOOL_DISPATCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    result = {
                        "error": (
                            f"Tool '{fn_name}' timed out after {_TOOL_DISPATCH_TIMEOUT}s. "
                            "The operation did not complete. Try a different approach."
                        ),
                        "timed_out": True,
                    }
                    logger.error(f"Tool dispatch timeout ({_TOOL_DISPATCH_TIMEOUT}s): {fn_name}")
                except Exception as dispatch_exc:
                    result = {"error": str(dispatch_exc)}
                    logger.warning(f"Tool dispatch error for {fn_name!r}: {dispatch_exc}")

                # Finding 3: track consecutive run_test failures to detect no-progress loops.
                # After 4 straight failures inject a strong hint; after 5 force-terminate.
                if fn_name == "run_test":
                    _passed = result.get("passed", False) if isinstance(result, dict) else False
                    if not _passed:
                        _test_fail_streak += 1
                    else:
                        _test_fail_streak = 0

                    if _test_fail_streak >= 5:
                        logger.warning(
                            f"_gemini_complete: {_test_fail_streak} consecutive test failures "
                            f"— force-terminating tool loop (no-progress guard)"
                        )
                        return (
                            "partial — force-terminated after "
                            f"{_test_fail_streak} consecutive test failures without progress"
                        )
                    elif _test_fail_streak >= 4:
                        # Soft nudge — injected into the tool result so the model sees it
                        if isinstance(result, dict):
                            result["_system_note"] = (
                                f"WARNING: you have called run_test {_test_fail_streak} times "
                                "in a row and every run failed. You are not making progress. "
                                "You MUST either fix the root cause in the source file or call "
                                "declare_done(summary='partial fix — tests still failing') NOW."
                            )
                        logger.warning(
                            f"_gemini_complete: {_test_fail_streak} consecutive test failures "
                            f"— injecting progress nudge into tool result"
                        )
                # Vision part extraction: if the tool result contains a _vision_part
                # (e.g. a screenshot PNG from the Manager's screenshot_app tool),
                # extract it and append it as a sibling inline_data part so Gemini
                # can see the image. The _vision_part key is removed from the text
                # response to avoid double-counting its base64 blob in the payload.
                _vision_part = None
                if isinstance(result, dict) and "_vision_part" in result:
                    _vision_part = result.pop("_vision_part")

                # Finding 4: Gemini docs require functionResponse.response to be
                # an object (dict), not a raw string/int/bool. Wrap non-dict results.
                if not isinstance(result, dict):
                    result = {"result": result}

                # Truncate oversized string values to prevent payload bloat that
                # triggers Gemini 400 context-too-large errors on later rounds.
                # Finding 6: use tighter limit for non-test results — test output
                # needs the full error trace, but file reads / search results don't.
                _MAX_RESULT_CHARS = 8000 if fn_name == "run_test" else 4000
                result = {
                    _k: (
                        _v[:_MAX_RESULT_CHARS]
                        + f"\n... [truncated {len(_v) - _MAX_RESULT_CHARS} chars]"
                        if isinstance(_v, str) and len(_v) > _MAX_RESULT_CHARS
                        else _v
                    )
                    for _k, _v in result.items()
                }
                response_parts.append({
                    "functionResponse": {
                        "name": fn_name,
                        "response": result,
                    }
                })
                # Append the vision part immediately after its functionResponse so
                # Gemini sees: [functionResponse(text metadata)] [inline_data(PNG)]
                if _vision_part:
                    response_parts.append(_vision_part)

            # Append user turn with all functionResponse parts
            contents.append({"role": "user", "parts": response_parts})
            tool_round += 1

            # ── Graduated budget awareness + force-terminate ────────────────
            # Thresholds are proportional to MAX_TOOL_ROUNDS so they scale
            # correctly when callers pass a lower max_tool_rounds (e.g. Big
            # Fixer uses 12 instead of the default 20).
            #   Tier 1 (50% budget consumed): soft budget note
            #   Tier 2 (75% budget consumed): strong urgency nudge
            #   Tier 3 (2 rounds left):       allowedFunctionNames=["declare_done"]
            _rounds_left = MAX_TOOL_ROUNDS - tool_round
            _tier1_threshold = MAX_TOOL_ROUNDS // 2   # 10 for 20, 6 for 12
            _tier2_threshold = max(MAX_TOOL_ROUNDS // 4, 3)  # 5 for 20, 3 for 12

            if _rounds_left <= 2 and _rounds_left > 0:
                # ── Tier 3: force-terminate via allowedFunctionNames ──────
                # With mode=ANY + allowedFunctionNames=["declare_done"], the
                # model MUST call declare_done — no other tool, no text.
                _urgency_note = (
                    f"⚠️ CRITICAL SYSTEM: You have {_rounds_left} tool round(s) remaining. "
                    "You are REQUIRED to call declare_done NOW. Your next response MUST be "
                    "declare_done(summary='...'). If tests are still failing, use: "
                    "declare_done(summary='partial fix — N tests remain'). "
                    "No other tool calls are permitted."
                )
                contents[-1]["parts"].insert(0, {"text": _urgency_note})

                if tools and "toolConfig" in payload:
                    payload["toolConfig"] = {
                        "functionCallingConfig": {
                            "mode": "ANY",
                            "allowedFunctionNames": ["declare_done"],
                        }
                    }

                logger.warning(
                    f"_gemini_complete: TIER 3 force-terminate — "
                    f"allowedFunctionNames=['declare_done'] "
                    f"({_rounds_left} rounds left at round {tool_round})"
                )

            elif _rounds_left <= _tier2_threshold and _rounds_left > 2:
                # ── Tier 2: strong urgency nudge ─────────────────────────
                _urgency_note = (
                    f"⚠️ SYSTEM: You have {_rounds_left} tool round(s) remaining. "
                    "You MUST call declare_done soon. If tests are still "
                    "failing, call declare_done(summary='partial fix — N tests remain'). "
                    "Do NOT spend remaining rounds reading files."
                )
                contents[-1]["parts"].insert(0, {"text": _urgency_note})

                logger.warning(
                    f"_gemini_complete: TIER 2 urgency nudge "
                    f"({_rounds_left} rounds left at round {tool_round})"
                )

            elif _rounds_left <= _tier1_threshold and _rounds_left > _tier2_threshold:
                # ── Tier 1: soft budget indicator ────────────────────────
                _budget_note = (
                    f"BUDGET: {_rounds_left} tool rounds remaining out of "
                    f"{MAX_TOOL_ROUNDS}. Plan your remaining work accordingly — "
                    "prioritize writing fixes and running tests over exploration."
                )
                contents[-1]["parts"].insert(0, {"text": _budget_note})

            elif _rounds_left == 0:
                # Final round — the while condition will exit, log for clarity
                logger.warning(
                    f"_gemini_complete: last round reached ({tool_round}/{MAX_TOOL_ROUNDS})"
                )

            # ── Context hygiene ─────────────────────────────────────────────
            # Two separate 400 triggers on gemini-customtools:
            #
            # 1. Stale thoughtSignatures: these are per-HTTP-request session
            #    tokens.  Replaying them from earlier rounds in a later request
            #    causes "invalid argument" 400.  Fix: scrub thought/
            #    thoughtSignature from every model turn EXCEPT the most recent
            #    one (index -2; index -1 is the tool-result just appended).
            #
            # 2. Payload size: contents grows unbounded; cap at 4 round-pairs
            #    after the initial seed message.
            # ────────────────────────────────────────────────────────────────

            # Step 1 — scrub stale thought tokens from all but newest model turn.
            # CRITICAL: never strip thoughtSignature from a turn that still has
            # functionCall parts — Gemini requires the two to travel together.
            # If the turn has functionCall, leave it fully intact; the trim in
            # Step 2 will eventually rotate it out of the context window.
            _newest_model_idx = len(contents) - 2  # model turn is always at -2
            for _i, _entry in enumerate(contents):
                if _entry.get("role") == "model" and _i != _newest_model_idx:
                    parts = _entry.get("parts", [])
                    has_fc = any("functionCall" in _p for _p in parts)
                    if not has_fc:
                        # No functionCall in this turn → safe to drop thought tokens
                        _entry["parts"] = [
                            _p for _p in parts
                            if "thought" not in _p and "thoughtSignature" not in _p
                        ]
                    # has_fc → keep entire turn intact (thought+thoughtSignature+functionCall
                    # must all be present or Gemini returns 400 "missing thought_signature")

            # Step 2 — trim oldest round-pairs once we exceed the cap
            _initial_len = len(history) + 1  # seed history rows + first user message
            _MAX_KEPT_ROUNDS = 4
            _max_len = _initial_len + _MAX_KEPT_ROUNDS * 2
            if len(contents) > _max_len:
                contents = contents[:_initial_len] + contents[-(_MAX_KEPT_ROUNDS * 2):]

        # Finding 2: raise a dedicated subclass so callers can distinguish "model used all
        # rounds but wrote files" (exhaustion — partial work happened) from a transient
        # API failure (nothing happened).  Catching RuntimeError for both cases causes the
        # Big Fixer to retry the same failure on the next backend, doubling stall time.
        raise ToolExhaustionError(
            f"Gemini tool loop exhausted after {MAX_TOOL_ROUNDS} rounds without declare_done"
        )

    async def _worker_complete(
        self, system: str, message: str, history: List[Dict]
    ) -> str:
        """Call the background worker model.

        Used for: fact extraction, memory maintenance, data processing,
        formatting tasks. Lightweight and fast on CPU at 24 t/s.
        Not for user-facing conversation — GLM handles that.
        """
        import httpx

        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.settings.WORKER_BASE_URL}/chat/completions",
                json={
                    "model": self.settings.WORKER_MODEL,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 1024,
                    "stream": False
                }
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]

        # Strip any think blocks if present
        if "</think>" in content:
            content = content.split("</think>", 1)[-1]
        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL)

        return content.strip()

    async def worker_complete_with_image(
        self, system: str, message: str,
        image_bytes: bytes, mime_type: str = "image/png"
    ) -> str:
        """Send image + text to the worker model via LM Studio vision API."""
        import httpx
        import base64

        b64_img = base64.b64encode(image_bytes).decode('utf-8')
        
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": message},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_img}"}}
            ]}
        ]

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.settings.WORKER_BASE_URL}/chat/completions",
                json={
                    "model": self.settings.WORKER_MODEL,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": 2048,
                    "stream": False
                }
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]

        # Strip any think blocks if present
        if "</think>" in content:
            content = content.split("</think>", 1)[-1]
        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL)

        return content.strip()

    async def complete_with_image(
        self,
        system: str,
        message: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        history: Optional[List[Dict]] = None,
        max_tokens: int = 2048,
    ) -> str:
        """Send an image + text prompt to Gemini Vision. Always uses Gemini."""
        if not self.settings.GOOGLE_API_KEY:
            raise ValueError(
                "GOOGLE_API_KEY not configured — image analysis requires Gemini Vision. "
                "Add GOOGLE_API_KEY via the Canopy Seed vault (launch and follow the vault setup screen)."
            )

        import base64
        import httpx

        history = history or []
        clean_history = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in history
            if "role" in msg and "content" in msg
        ]

        # Build Gemini contents array — history as text, then current turn with image
        contents = []
        for msg in clean_history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

        contents.append({
            "role": "user",
            "parts": [
                {"text": message},
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(image_bytes).decode("utf-8")
                    }
                }
            ]
        })

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.GOOGLE_GEMINI_MODEL}:generateContent"
            f"?key={self.settings.GOOGLE_API_KEY}"
        )

        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "temperature": 1.0,  # Gemini 3 docs: "keep at default 1.0"
                "maxOutputTokens": max_tokens,
            }
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            logger.error(f"Gemini vision response parse error: {e}\nResponse: {data}")
            raise RuntimeError(f"Failed to parse Gemini vision response: {e}")

    # -----------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------

    async def get_backend_status(self) -> str:
        """Status string for /status command"""
        claude_ok = bool(self.settings.ANTHROPIC_API_KEY)
        escalation = "enabled" if self.settings.CLAUDE_ESCALATION_ENABLED else "disabled"
        google_ok = bool(self.settings.GOOGLE_API_KEY)
        lmstudio_ok = await self._check_lmstudio()

        # Quick check if worker model is reachable
        worker_ok = False
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.settings.WORKER_BASE_URL.replace('/v1', '')}/v1/models",
                    timeout=2
                )
                worker_ok = r.status_code == 200
        except Exception:
            pass

        lines = ["*AI Backends:*"]
        lines.append(f"  LM Studio: {'✅' if lmstudio_ok else '❌'} ({self.settings.LMSTUDIO_MODEL})")
        lines.append(f"  Worker: {'✅' if worker_ok else '❌'} ({self.settings.WORKER_MODEL})")
        lines.append(f"  Claude API: {'✅' if claude_ok else '❌'} ({self.settings.CLAUDE_MODEL}) [escalation: {escalation}]")
        lines.append(f"  Google Gemini: {'✅' if google_ok else '❌'} ({self.settings.GOOGLE_GEMINI_MODEL})")
        lines.append(f"  Default: {self.settings.DEFAULT_AI_BACKEND}")
        return "\n".join(lines)

    def get_role_chain(self, role: str) -> List[str]:
        """Return the ordered backend list for a named agent role (ADR-036/037/040).

        Maps role names to the comma-separated ROLE_*_CHAIN setting and returns
        a list of backend identifiers in priority order (primary first).

        Role names (case-insensitive):
            interview, planner, orchestrator, chunker, auditor,
            dev_swarm_t1, dev_swarm_t2, dev_swarm_t3,
            test_swarm_t1, test_swarm_t2, big_fixer,
            mcp, memory  (ADR-037 — mechanical/relay roles, qwen-flash primary)

        Priority (highest to lowest — ADR-040):
            1. Per-role env var: ROLE_*_CHAIN setting on self.settings
            2. Active workflow profile: WORKFLOW_PROFILE → config/profiles.py
            3. Hardcoded _ROLE_DEFAULTS (legacy fallback)

        Falls back to ["claude"] if the role is unknown or settings are absent.
        """
        _ROLE_DEFAULTS = {
            # Session 58: role → thinking level mapping aligned with Gemini 3 architecture
            # interview: plain Pro (adaptive thinking, no schema) — deep reasoning for intake
            "interview":          "claude,gemini-pro-plain,gemini-flash-high",
            "planner":            "gemini-customtools,claude,gemini-flash-high",
            # orchestrator/chunker: customtools primary — Flash 3.0 HIGH+schema → HTTP 400 (S58 run log)
            "orchestrator":       "gemini-customtools,gemini-flash-high,claude-haiku",
            "chunker":            "gemini-customtools,gemini-flash-high,claude-haiku",
            # auditor: high thinking for quality audit
            "auditor":            "gemini-customtools,gemini-flash-high,claude",
            # swarm: MINIMAL thinking — fast, mechanical coding tasks
            "dev_swarm_t1":       "gemini-flash-minimal,gemini-flash,claude-haiku",
            "dev_swarm_t2":       "gemini-flash-minimal,gemini-customtools,claude-haiku",
            "dev_swarm_t3":       "claude,gemini-customtools,gemini-flash-high",
            "test_swarm_t1":      "gemini-flash-minimal,gemini-customtools,claude-haiku",
            "test_swarm_t2":      "gemini-customtools,claude,gemini-flash-high",
            # big_fixer: customtools primary (tool calling) + high thinking fallback
            "big_fixer":          "gemini-customtools,gemini-flash-high,claude",
            # ADR-039 — Phase 0 contract generation: customtools primary — Flash 3.0 HIGH+schema
            # produces non-JSON output ("response is not a JSON object") seen in S58 run log.
            "contract_generator": "gemini-customtools,gemini-flash-high,claude",
            # ADR-039 — Giant Brain audit: customtools primary — reliable JSON, known thinking level.
            # Flash 3.0 HIGH+JSON (no schema) also unreliable for large analysis prompts (S58 log).
            "giant_brain":        "gemini-customtools,gemini-flash-high,claude",
            # ADR-037 mechanical roles — qwen-flash primary (cheap, 480 t/s, ~$0.05/$0.15)
            "mcp":                "qwen-flash,gemini-flash-lite,claude-haiku",
            "memory":             "qwen-flash,gemini-flash-lite,claude-haiku",
        }
        _ROLE_SETTING = {
            "interview":          "ROLE_INTERVIEW_CHAIN",
            "planner":            "ROLE_PLANNER_CHAIN",
            "orchestrator":       "ROLE_ORCHESTRATOR_CHAIN",
            "chunker":            "ROLE_CHUNKER_CHAIN",
            "auditor":            "ROLE_AUDITOR_CHAIN",
            "contract_generator": "ROLE_CONTRACT_CHAIN",
            "giant_brain":        "ROLE_GIANT_BRAIN_CHAIN",
            "dev_swarm_t1":       "ROLE_DEV_SWARM_T1_CHAIN",
            "dev_swarm_t2":       "ROLE_DEV_SWARM_T2_CHAIN",
            "dev_swarm_t3":       "ROLE_DEV_SWARM_T3_CHAIN",
            "test_swarm_t1":      "ROLE_TEST_SWARM_T1_CHAIN",
            "test_swarm_t2":      "ROLE_TEST_SWARM_T2_CHAIN",
            "big_fixer":          "ROLE_BIG_FIXER_CHAIN",
            "mcp":                "ROLE_MCP_CHAIN",
            "memory":             "ROLE_MEMORY_CHAIN",
        }
        key = (role or "").strip().lower().replace("-", "_")

        # ── Priority 1: per-role env var override ─────────────────────────
        setting_name = _ROLE_SETTING.get(key)
        if setting_name and self.settings is not None:
            env_val = getattr(self.settings, setting_name, None)
            if env_val and env_val.strip():
                return [b.strip() for b in env_val.split(",") if b.strip()]

        # ── Priority 2: active workflow profile (ADR-040) ─────────────────
        try:
            from config.profiles import get_profile_chain
            active_profile = (
                getattr(self.settings, "WORKFLOW_PROFILE", None) or "gemini"
                if self.settings is not None else "gemini"
            )
            profile_chain = get_profile_chain(active_profile, key)
            if profile_chain and profile_chain.strip():
                return [b.strip() for b in profile_chain.split(",") if b.strip()]
        except Exception:
            pass  # profiles module not available — continue to defaults

        # ── Priority 3: hardcoded defaults (legacy fallback) ──────────────
        default = _ROLE_DEFAULTS.get(key, "claude")
        return [b.strip() for b in default.split(",") if b.strip()]

    def get_primary_backend_for_role(self, role: str) -> str:
        """Convenience: return just the primary (first) backend for a role."""
        chain = self.get_role_chain(role)
        return chain[0] if chain else "claude"

    async def _ollama_complete(
        self, system: str, message: str, history: list,
        endpoint: str = "", model_id: str = "", max_tokens: int = 4096
    ) -> str:
        """Call Ollama generate API."""
        import httpx, re
        base_url = endpoint or getattr(self.settings, 'OLLAMA_BASE_URL', 'http://localhost:11434')
        payload = {
            "model": model_id, "system": system, "prompt": message,
            "stream": False, "options": {"temperature": 0.7, "num_predict": max_tokens}
        }
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(f"{base_url}/api/generate", json=payload)
            response.raise_for_status()
            content = response.json().get("response", "")
        if "</think>" in content:
            content = content.split("</think>", 1)[-1]
        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL)
        return content.strip()

    async def targeted_complete(
        self, backend: str, model_id: str, system: str, message: str,
        history: list = None, endpoint: str = "", api_key: str = "", max_tokens: int = 4096,
    ) -> dict:
        """Send a message to a specific backend+model. Used by Dev Hub."""
        t0 = time.time()
        history = history or []
        clean_history = [{"role": m["role"], "content": m["content"]} for m in history if "role" in m and "content" in m]
        if backend == "lmstudio":
            await self._lmstudio_ensure_model(model_id)
            content = await self._lmstudio_complete(system, message, clean_history, model_id=model_id, max_tokens=max_tokens)
        elif backend == "claude":
            old_model = self.settings.CLAUDE_MODEL
            if model_id:
                self.settings.CLAUDE_MODEL = model_id
            try:
                content = await self._claude_complete(system, message, clean_history, max_tokens)
            finally:
                self.settings.CLAUDE_MODEL = old_model
        elif backend == "gemini":
            old_model = self.settings.GOOGLE_GEMINI_MODEL
            if model_id:
                self.settings.GOOGLE_GEMINI_MODEL = model_id
            try:
                content = await self._gemini_complete(
                    system or '',
                    message,
                    clean_history,
                    max_tokens=max_tokens,
                )
            finally:
                self.settings.GOOGLE_GEMINI_MODEL = old_model
        elif backend == "ollama":
            content = await self._ollama_complete(system, message, clean_history, endpoint=endpoint, model_id=model_id, max_tokens=max_tokens)
        elif backend == "worker":
            content = await self._worker_complete(system, message, clean_history)
        else:
            raise ValueError(f"Unknown backend: {backend}")
        return {"content": content, "model_used": model_id or "default", "backend": backend, "duration_ms": round((time.time() - t0) * 1000)}
