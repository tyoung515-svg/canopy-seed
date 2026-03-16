"""
context_builder.py — Big Brain Conversation Manager

WHY THIS EXISTS:
Most people fail at AI-assisted building because they can't articulate
what they want with enough structure for the system to act on.
This module is the solution: it manages a guided conversation between
the user and a Big Brain AI that extracts project requirements naturally,
without asking the user to think like a developer.

DESIGN DECISIONS:
- RESEARCH: and CONTEXT_READY are hidden signals, never shown to the user
- One question per response is enforced in the Big Brain system prompt
- Context accumulates incrementally — the sidebar updates live as understanding builds
- finalize_context() makes one dedicated AI call to structure everything — 
  the same model that learned the context summarizes it best

OWNED BY: Agent CS1 (Anti/Gemini Pro) — Canopy Seed V1, 2026-02-25
REVIEWED BY: Claude Sonnet 4.6 (Orchestrator)
"""

import re
import json
import uuid
import logging
import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class ResearchEntry:
    query: str
    summary: str
    citations: list
    timestamp: str = field(default_factory=lambda: datetime.datetime.now().isoformat())

@dataclass
class ProjectContext:
    name: str = ""
    description: str = ""
    goals: list = field(default_factory=list)
    constraints: list = field(default_factory=list)
    target_users: str = ""
    tech_preferences: list = field(default_factory=list)
    architecture_notes: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    research_log: list = field(default_factory=list)   # list of ResearchEntry dicts
    conversation_summary: str = ""
    created_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    last_updated: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    ready: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

# ── System Prompt ──────────────────────────────────────────────────────────────

BIG_BRAIN_SYSTEM_PROMPT = """You are a friendly, patient project advisor named Canopy.
Your only job is to understand what someone wants to build well enough that a team of AI agents can build it correctly.

Rules:
1. Ask ONE question at a time. Never ask multiple questions in one message.
2. Keep your language simple and non-technical unless the user is clearly technical.
3. Acknowledge what the user shared before asking your next question.
4. If you receive an image, describe what you see and ask if it represents what they want to build.
5. When you need external information to make a better recommendation, emit on its own line:
   RESEARCH: <specific search query>
6. When you have enough information to create a complete project specification (you understand: what it does, who uses it, what the key features are, what the constraints are), emit on its own line:
   CONTEXT_READY
7. Do not mention RESEARCH: or CONTEXT_READY to the user. These are internal signals.
8. Be encouraging. Many users have never described software before. Make them feel capable.

You are not building the software. You are understanding it."""

FINALIZE_PROMPT = """Based on our conversation, produce a structured project specification as JSON.

Return ONLY valid JSON matching this exact structure — no markdown, no explanation:
{
  "name": "short project name",
  "description": "2-3 sentence plain description",
  "goals": ["goal 1", "goal 2"],
  "constraints": ["constraint 1"],
  "target_users": "who will use this",
  "tech_preferences": ["preference 1"],
  "architecture_notes": ["note 1"],
  "open_questions": ["question still unanswered"],
  "conversation_summary": "1-2 sentence summary of what was discussed"
}"""

# ── ContextBuilder ─────────────────────────────────────────────────────────────

class ContextBuilder:
    """
    Manages a Big Brain seeding session.
    Call start() to initialize, send_message() for each user turn.
    Detects RESEARCH: and CONTEXT_READY markers automatically.
    """

    def __init__(self, ai_backend, settings, sse_broadcast=None):
        self.ai_backend = ai_backend
        self.settings = settings
        self.sse_broadcast = sse_broadcast       # async callable(event_type, data)
        self.messages: list = []                  # Full conversation [{role, content}]
        self.context = ProjectContext()
        self.session_id: str = str(uuid.uuid4())
        self._research_engine = None             # Injected after init if available
        self._model: str = "gemini"              # Set by start()

    def set_research_engine(self, engine):
        self._research_engine = engine

    async def start(self, model: str = "gemini") -> dict:
        """
        Initialize a new seeding session.
        Returns the opening message from Big Brain.
        """
        self._model = model
        self.messages = []
        self.context = ProjectContext()

        opening = "What would you like to build today? Start by describing the problem you're trying to solve — no technical language needed."
        self.messages.append({"role": "assistant", "content": opening})

        logger.info(f"Canopy session {self.session_id} started with model={model}")
        return {
            "session_id": self.session_id,
            "opening_message": opening,
            "model": model
        }

    async def send_message(self, user_text: str, image_data: bytes = None) -> dict:
        """
        Process a user message (text + optional image).

        Returns:
            {
              "reply": str,
              "context_delta": dict,   # fields that changed this turn
              "researching": bool,     # True if a research call is in flight
              "ready": bool            # True if context is finalized
            }
        """
        # Build the user message payload
        user_content = user_text
        if image_data:
            # Encode image as base64 for vision-capable models
            import base64
            b64 = base64.b64encode(image_data).decode("utf-8")
            user_content = [
                {"type": "text", "text": user_text or "Here is an image of what I have in mind."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]

        self.messages.append({"role": "user", "content": user_content})

        # Call Big Brain
        raw_reply = await self._call_big_brain(self._model, self.messages)

        # Strip internal signals from the user-visible reply
        visible_reply, research_queries, context_ready = self._parse_reply(raw_reply)

        # Add cleaned reply to history (Big Brain sees its own signals on replay)
        self.messages.append({"role": "assistant", "content": raw_reply})

        # Handle research requests
        researching = False
        for query in research_queries:
            researching = True
            if self.sse_broadcast:
                await self.sse_broadcast("canopy_research_start", {"query": query})

            entry = await self._handle_research_marker(query)
            if entry:
                self.context.research_log.append(entry.__dict__)
                self.context.last_updated = datetime.datetime.now().isoformat()
                if self.sse_broadcast:
                    await self.sse_broadcast("canopy_research_complete", {"entry": entry.__dict__})

                # Inject research result as hidden system context
                self.messages.append({
                    "role": "user",
                    "content": f"[Research result for \"{query}\": {entry.summary}. Sources: {', '.join(entry.citations)}]"
                })

        # Check for context-ready signal
        if context_ready:
            await self.finalize_context()
            return {
                "reply": visible_reply or "I have a good understanding of your project now. Let me put the blueprint together.",
                "context_delta": self.context.to_dict(),
                "researching": False,
                "ready": True
            }

        # Broadcast incremental context update
        delta = self._extract_context_delta(visible_reply)
        if delta and self.sse_broadcast:
            await self.sse_broadcast("canopy_context_update", {"context_delta": delta})

        return {
            "reply": visible_reply,
            "context_delta": delta,
            "researching": researching,
            "ready": False
        }

    async def _call_big_brain(self, model: str, messages: list) -> str:
        """Route to the appropriate AI backend based on model selection.

        Tries the requested model first; if it fails with a hard error (e.g.
        depleted credits, 400, auth failure) falls back through the chain:
        claude → gemini → local.  This means a depleted Anthropic key never
        leaves the interviewer stuck — it silently steps to Gemini.
        """
        # Split messages into history + last user message
        if messages and messages[-1]["role"] == "user":
            history = messages[:-1]
            last_content = messages[-1]["content"]
            if isinstance(last_content, list):
                message = next(
                    (p["text"] for p in last_content if p.get("type") == "text"), ""
                )
            else:
                message = last_content
        else:
            history = messages
            message = ""

        async def _try_claude():
            return await self.ai_backend._claude_complete(
                system=BIG_BRAIN_SYSTEM_PROMPT, message=message, history=history
            )

        async def _try_gemini():
            return await self.ai_backend._gemini_complete(
                system=BIG_BRAIN_SYSTEM_PROMPT, message=message, history=history
            )

        async def _try_local():
            return await self.ai_backend._lmstudio_complete(
                system=BIG_BRAIN_SYSTEM_PROMPT, message=message, history=history
            )

        # Build the attempt order based on the requested model
        if model == "claude":
            attempts = [("claude", _try_claude), ("gemini", _try_gemini), ("local", _try_local)]
        elif model == "gemini":
            attempts = [("gemini", _try_gemini), ("claude", _try_claude), ("local", _try_local)]
        else:
            attempts = [("local", _try_local), ("gemini", _try_gemini), ("claude", _try_claude)]

        last_error = None
        for backend_name, attempt_fn in attempts:
            try:
                result = await attempt_fn()
                if backend_name != model:
                    logger.warning(
                        f"Big Brain fell back from '{model}' → '{backend_name}' "
                        f"(primary error: {last_error})"
                    )
                    # Keep the session on the working backend for subsequent turns
                    self._model = backend_name
                return result
            except Exception as e:
                last_error = e
                logger.warning(f"Big Brain '{backend_name}' failed: {e}")
                continue

        logger.error(f"All Big Brain backends failed. Last error: {last_error}")
        return "I'm having trouble connecting to any AI backend. Please check your configuration and try again."

    def _parse_reply(self, raw: str) -> tuple:
        """
        Extract RESEARCH: queries and CONTEXT_READY signal from raw AI reply.
        Returns: (visible_reply, [research_queries], context_ready_bool)
        """
        research_queries = []
        context_ready = False
        lines = raw.split('\n')
        visible_lines = []

        for line in lines:
            research_match = re.match(r'^RESEARCH:\s+(.+)$', line.strip())
            if research_match:
                research_queries.append(research_match.group(1).strip())
            elif line.strip() == 'CONTEXT_READY':
                context_ready = True
            else:
                visible_lines.append(line)

        visible_reply = '\n'.join(visible_lines).strip()
        return visible_reply, research_queries, context_ready

    async def _handle_research_marker(self, query: str) -> Optional[ResearchEntry]:
        """Call the research engine for a given query. Returns ResearchEntry or None."""
        if self._research_engine is None:
            logger.warning("Research engine not set — skipping research for: %s", query)
            return None
        try:
            return await self._research_engine.research(query)
        except Exception as e:
            logger.error(f"Research failed for '{query}': {e}")
            return None

    def _extract_context_delta(self, reply: str) -> dict:
        """
        Lightweight heuristic extraction of context from Big Brain's reply and
        the conversation so far. Updates self.context in place and returns a
        delta dict for the sidebar. Best-effort — finalize_context() does the
        authoritative extraction at the end.
        """
        delta = {}
        r_lower = reply.lower()

        # ── Project name ──────────────────────────────────────────────────
        # Big Brain often says "called X", "named X", "X system", etc.
        if not self.context.name:
            name_patterns = [
                r"(?:called|named|project\s+called|app\s+called|system\s+called)\s+['\"]?([A-Z][A-Za-z0-9 ]{2,30})['\"]?",
                r"(?:build|create|make)\s+(?:a\s+|an\s+)?([A-Z][A-Za-z0-9 ]{2,30}?)(?:\s+(?:system|app|tool|platform|portal))",
            ]
            for pat in name_patterns:
                m = re.search(pat, reply)
                if m:
                    candidate = m.group(1).strip()
                    if len(candidate) > 3:
                        self.context.name = candidate
                        delta["name"] = candidate
                        break

        # ── Goal / problem statement ───────────────────────────────────────
        # Look for "to help", "so that", "in order to" phrases in user messages
        if not self.context.description and len(self.messages) >= 2:
            user_msgs = [m["content"] for m in self.messages
                         if m["role"] == "user" and isinstance(m.get("content"), str)]
            if user_msgs:
                first_user = user_msgs[0]
                if len(first_user) > 20:
                    self.context.description = first_user[:200]
                    delta["description"] = self.context.description

        # ── Target users ───────────────────────────────────────────────────
        if not self.context.target_users:
            user_patterns = [
                r"(?:for|used by|target(?:ed)?\s+at)\s+([a-z][a-z\s]{3,40}?)(?:\s+who|\s+to|\.|,)",
                r"(?:your\s+(?:users|customers|clients)\s+(?:are|will be))\s+([a-z][a-z\s]{3,40}?)(?:\.|,|$)",
            ]
            for pat in user_patterns:
                m = re.search(pat, r_lower)
                if m:
                    candidate = m.group(1).strip()
                    if 3 < len(candidate) < 60:
                        self.context.target_users = candidate
                        delta["target_users"] = candidate
                        delta["users"] = candidate
                        break

        return delta

    async def finalize_context(self) -> ProjectContext:
        """
        Called when CONTEXT_READY is detected.
        Makes one dedicated AI call asking Big Brain to structure everything
        it learned into a ProjectContext JSON, then writes PROJECT_CONTEXT.json.
        """
        logger.info(f"Finalizing context for session {self.session_id}")

        # Add finalization request to conversation
        self.messages.append({"role": "user", "content": FINALIZE_PROMPT})

        def _list_from_keys(data: dict, keys: list[str]) -> list:
            for key in keys:
                value = data.get(key)
                if isinstance(value, list):
                    return value
            return []

        try:
            raw = await self._call_big_brain(self._model, self.messages)
            # Extract JSON — handle cases where model wraps it in markdown
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                self.context.name = data.get("name", "")
                self.context.description = data.get("description", "")
                self.context.goals = _list_from_keys(data, ["goals", "key_features", "features", "objectives"])
                self.context.constraints = data.get("constraints", [])
                self.context.target_users = str(data.get("target_users") or "").strip()
                self.context.tech_preferences = data.get("tech_preferences", [])
                self.context.architecture_notes = _list_from_keys(data, ["architecture_notes", "architecture", "technical_notes", "tech_stack"])
                self.context.open_questions = data.get("open_questions", [])
                self.context.conversation_summary = data.get("conversation_summary", "")
        except Exception as e:
            logger.error(f"Context finalization parsing failed: {e}")

        self.context.ready = True
        self.context.last_updated = datetime.datetime.now().isoformat()

        # Write to disk (non-fatal — log but don't crash if path is unwritable)
        try:
            output_path = Path(getattr(self.settings, 'context_output_path', 'exports/PROJECT_CONTEXT.json'))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(self.context.to_json(), encoding='utf-8')
            logger.info(f"PROJECT_CONTEXT.json written to {output_path}")
        except Exception as e:
            logger.warning(f"Could not write PROJECT_CONTEXT.json: {e}")

        # Broadcast via SSE (non-fatal — log but don't crash)
        if self.sse_broadcast:
            try:
                await self.sse_broadcast("canopy_context_ready", {"context": self.context.to_dict()})
            except Exception as e:
                logger.warning(f"SSE broadcast failed: {e}")

        return self.context

    def get_context_snapshot(self) -> dict:
        """Return current context state for live sidebar updates."""
        return self.context.to_dict()

    def get_messages(self) -> list:
        """Return conversation history (stripped of hidden research injections)."""
        return [m for m in self.messages if not (
            isinstance(m.get("content"), str) and m["content"].startswith("[Research result")
        )]
