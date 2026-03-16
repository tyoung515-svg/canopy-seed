"""
example_skill.py — Template for Creating New Skills

WHY THIS EXISTS:
Skills are the extensible command system for Canopy Seed. This file demonstrates
how to write a new skill by extending the Skill base class. Use this as a template
when adding new commands like !weather, !fetch, !slack, or any custom domain tool.

DESIGN DECISIONS:
- Async execute() method for non-blocking I/O (allow multiple skills running in parallel)
- String arguments (not parsed) — router handles basic parsing; skill interprets args
- Returns string response (always, for consistency)
- One skill can have multiple triggers (!example, !ex, !sample all call same skill)
- No skill touches the filesystem directly (uses core/tools instead)

OWNED BY: Agent CS1 (Anti/Gemini Pro) — Canopy Seed V1, 2026-02-25
REVIEWED BY: Claude Sonnet 4.6 (Orchestrator)
"""

import logging
from skills.base import Skill

logger = logging.getLogger(__name__)


class ExampleSkill(Skill):
    """
    Example skill demonstrating the Skill interface.
    
    This skill shows all the pieces you need to create a new command.
    Replace this with your own implementation.
    
    Commands:
        !example <text>           — Echo the text back
        !ex <text>                — Shorthand alias for !example
    """
    
    # Required class attributes
    name = "example"
    description = "Example skill (template for creating new skills)"
    triggers = ["example", "ex"]  # Command names that activate this skill
    enabled = True
    
    async def execute(self, args: str, update, context) -> str:
        """
        Execute the skill.
        
        Called when user types a command matching one of the triggers.
        For example, user types "!example hello world" → execute() is called
        with args="hello world".
        
        Args:
            args: Everything after the command name (stripped of leading/trailing whitespace)
            update: The incoming message object (Telegram Update or similar)
            context: Canopy's shared context object (conversation history, settings, etc.)
        
        Returns:
            A string response to send back to the user. Can include:
            - Plain text
            - Formatted markdown
            - Error messages (prefix with "Error: ")
        """
        
        # Example 1: Simple echo
        if not args:
            return "Usage: !example <text>"
        
        # Example 2: Process arguments
        parts = args.split()
        if len(parts) < 2:
            return f"Got one argument: {parts[0]}"
        
        # Example 3: Return formatted response
        return f"Echo: {args}\n\nWord count: {len(parts)}"
    
    # (Optional) Add validation, error handling, logging as needed
    
    async def validate(self, args: str) -> tuple[bool, str]:
        """
        Optional: Validate arguments before execution.
        
        Return (valid, error_message). If valid=False, the error_message
        is shown to the user instead of calling execute().
        """
        if not args:
            return False, "This skill requires arguments"
        return True, ""


# ============================================================================
# HOWTO: Create Your Own Skill
# ============================================================================
#
# 1. Copy this file to skills/my_skill.py
#
# 2. Rename the class (e.g., WeatherSkill, SlackSkill):
#    class MySkill(Skill):
#        name = "myskill"
#        triggers = ["myskill", "my"]
#
# 3. Implement execute() with your logic
#
# 4. Register the skill in skills/registry.py:
#    from skills.my_skill import MySkill
#    SKILL_REGISTRY = {
#        "myskill": MySkill(),
#        ...
#    }
#
# 5. For complex operations (web fetch, file I/O, shell commands),
#    use tools/ modules instead of implementing directly:
#
#    ✓ Good: Call tools/web_fetch.fetch(url)
#    ✗ Bad:  import requests; requests.get(url)
#
#    This keeps skills thin and reusable.
#
# 6. Add tests in tests/test_my_skill.py:
#    
#    @pytest.mark.asyncio
#    async def test_my_skill_basic():
#        skill = MySkill()
#        result = await skill.execute("test args", None, None)
#        assert "expected" in result
#
# 7. Document your skill:
#    - Add a docstring explaining what it does
#    - List all commands and their syntax
#    - Add examples of usage
#
# For questions, see CONTRIBUTING.md or ARCHITECTURE.md.
