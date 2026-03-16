"""
Message Router
Parses intent from incoming messages and routes to appropriate handler
"""

import logging
import asyncio
from pathlib import Path
from typing import Optional

from telegram import Update

from config.settings import Settings
from core.context import AgentContext
from core.ai_backend import AIBackend
from core.extractor import FactExtractor
from core.nl_action import classify_as_action
from tools.shell_tools import ShellTool
from tools.file_tools import FileTool
from tools.system_tools import get_system_status
from tools.common_tools import run_tool

logger = logging.getLogger(__name__)


class MessageRouter:
    def __init__(self, settings: Settings, context: AgentContext, skill_registry=None, memory_store=None):
        self.settings = settings
        self.context = context
        self.skill_registry = skill_registry
        self.memory_store = memory_store
        self.ai = AIBackend(settings)
        self.context.ai_backend = self.ai
        self.context.ai = self.ai
        self.extractor = FactExtractor(self.ai, self.memory_store)
        self.shell = ShellTool(settings)
        self.files = FileTool(settings)

    async def route(self, message: str, update: Update, force_backend: Optional[str] = None) -> str:
        """
        Route message to appropriate handler based on prefix or intent
        """
        msg = message.strip()
        logger.debug(f"Routing message: {msg[:100]}...")

        # --- Pending command confirmation handler ---
        lower_msg_check = msg.lower().strip()
        pending = self.context.get_pending_command()

        if pending and lower_msg_check in ("yes", "y", "yeah", "yep", "do it", "confirm"):
            self.context.clear_pending_command()
            logger.info(f"Pending command confirmed: {pending}")
            try:
                if pending.startswith("!fetch "):
                    url = pending[7:].strip()
                    from tools.web_fetch import fetch
                    try:
                        raw = await fetch(url)
                        import json
                        try:
                            data = json.loads(raw)
                            formatted = json.dumps(data, indent=2)
                            if len(formatted) > 3000:
                                formatted = formatted[:3000] + "\n...[truncated]"
                            result = f"✅ Fetched `{url}`:\n```json\n{formatted}\n```"
                        except json.JSONDecodeError:
                            if len(raw) > 2000:
                                raw = raw[:2000] + "\n...[truncated]"
                            result = f"✅ Fetched `{url}`:\n```\n{raw}\n```"
                    except Exception as e:
                        result = f"❌ Fetch failed for `{url}`: {e}"
                else:
                    # Shell command — already confirmed, execute directly
                    cmd_result = await self.shell.execute(pending)
                    if len(cmd_result) > 3000:
                        cmd_result = cmd_result[:3000] + "\n...(truncated)"
                    result = f"✅ Executed:\n```\n{cmd_result}\n```"
            except Exception as e:
                result = f"❌ Execution failed: {e}"
                logger.error(f"Pending command execution error: {e}", exc_info=True)

            self.context.add_to_history("user", msg)
            self.context.add_to_history("assistant", result)
            return result

        if pending and lower_msg_check in ("no", "n", "nah", "cancel", "nevermind"):
            self.context.clear_pending_command()
            result = f"🚫 Cancelled: `{pending}`"
            self.context.add_to_history("user", msg)
            self.context.add_to_history("assistant", result)
            return result

        # --- Screenshot Routing ---
        lower_msg = msg.lower()
        screenshot_triggers = [
            "take a screenshot", "screenshot", "show me the screen",
            "what's on your screen", "capture screen"
        ]
        if any(lower_msg.startswith(t) or lower_msg == t for t in screenshot_triggers):
            from tools.screenshot_tool import capture_screen
            return await capture_screen(update)

        # --- Skill routing (pluggable skills, layered on top) ---
        if msg.startswith("!") and self.skill_registry:
            command_parts = msg[1:].split(" ", 1)
            command_name = command_parts[0].strip().lower() if command_parts else ""
            command_args = command_parts[1].strip() if len(command_parts) > 1 else ""

            if command_name:
                skill = self.skill_registry.get(command_name)
                if skill and skill.enabled:
                    try:
                        self.context.current_trigger = command_name
                        return await skill.execute(command_args, update, self.context)
                    except Exception as e:
                        logger.error(f"Skill '{command_name}' failed: {e}", exc_info=True)
                        return f"Skill error in `{command_name}`: {e}"
                    finally:
                        self.context.current_trigger = None

        # --- Explicit prefix routing ---

        if msg.startswith("!shell "):
            cmd = msg[7:].strip()
            return await self._handle_shell(cmd, update)

        if msg.startswith("!file "):
            path = msg[6:].strip()
            return await self._handle_file_read(path)

        if msg.startswith("!write "):
            # Format: !write /path/to/file <content>
            parts = msg[7:].split("\n", 1)
            if len(parts) == 2:
                return await self._handle_file_write(parts[0].strip(), parts[1])
            return "Usage: !write /path/to/file\\n<content>"

        # --- Common Tools Routing ---
        if lower_msg.startswith("!tool"):
            parts = msg[5:].strip().split(None, 1)
            tool_name = parts[0] if parts else "help"
            tool_args = parts[1] if len(parts) > 1 else ""
            return await run_tool(tool_name, tool_args)

        # --- Deep Thinking Routing (uses Claude for complex analysis) ---
        if lower_msg.startswith("!deep ") or lower_msg.startswith("!think "):
            cmd = msg.split(" ", 1)[1] if " " in msg else ""
            if cmd:
                return await self._handle_ai(cmd, update, backend="claude")
            return "Usage: !deep <your prompt>"

        # --- Explicit Claude Routing ---
        if lower_msg.startswith("!claude ") or lower_msg.startswith("!api "):
            cmd = msg.split(" ", 1)[1] if " " in msg else ""
            if cmd:
                return await self._handle_ai(cmd, update, backend="claude")
            return "Usage: !claude <your prompt>"

        # --- Explicit Gemini Routing ---
        if lower_msg.startswith("!gemini "):
            cmd = msg.split(" ", 1)[1] if " " in msg else ""
            if cmd:
                return await self._handle_ai(cmd, update, backend="gemini")
            return "Usage: !gemini <your prompt>"

        # --- !unlock — list elevated access tier actions ---
        if lower_msg.strip() == "!unlock":
            from core.permissions import unlock_status_message
            return unlock_status_message()

        # --- Natural language intent classification (non-! messages only) ---
        if not msg.startswith("!"):
            from core.nl_intent import classify_intent
            intent_result = classify_intent(msg)
            if intent_result:
                if intent_result["confidence"] == "medium":
                    matched = intent_result.get("matched_intents") or []
                    options = ", ".join(f"`{m}`" for m in matched)
                    return f"🤔 Ambiguous — did you mean one of: {options}?\nTry being more specific, or use `!<command>` directly."

                handler = intent_result["handler"]
                arg = intent_result.get("arg")

                # skill:X — route to skill registry
                if handler.startswith("skill:") and self.skill_registry:
                    skill_name = handler[6:]
                    skill = self.skill_registry.get(skill_name)
                    if skill and skill.enabled:
                        try:
                            self.context.current_trigger = skill_name
                            return await skill.execute(arg or "", update, self.context)
                        except Exception as e:
                            logger.error(f"NL skill '{skill_name}' failed: {e}", exc_info=True)
                            return f"Skill error in `{skill_name}`: {e}"
                        finally:
                            self.context.current_trigger = None

                elif handler == "shell":
                    if not arg:
                        return "What command do you want me to run?"
                    return await self._handle_shell(arg, update)

                elif handler == "file_read":
                    if not arg:
                        return "What file do you want me to read?"
                    return await self._handle_file_read(arg)

                elif handler == "fetch":
                    if not arg:
                        return "What URL do you want me to fetch?"
                    return await self._handle_ai(f"FETCH_URL: {arg}", update, backend=force_backend)

        # --- NL catch-all action classifier ---
        if not msg.startswith("!"):
            cmd = await classify_as_action(msg, self.ai)
            if cmd:
                logger.info(f"NL action classifier matched: '{msg}' → '{cmd}'")
                return await self._handle_shell(cmd, update)

        # --- AI-driven routing ---
        return await self._handle_ai(msg, update, backend=force_backend)

    async def route_with_image(self, message: str, image_bytes: bytes, mime_type: str, update) -> str:
        """
        Route an image (with optional text prompt) to Gemini Vision.
        Logs the exchange to conversation history so context is maintained.
        """
        system_prompt = self._build_system_prompt()
        history = self.context.get_history()

        response = await self.ai.complete_with_image(
            system=system_prompt,
            message=message,
            image_bytes=image_bytes,
            mime_type=mime_type,
            history=history,
        )

        # Store in history — represent the image as a text note so context carries forward
        self.context.add_to_history("user", f"[Sent an image] {message}")
        self.context.add_to_history("assistant", response)
        return response

    async def _handle_ai(self, message: str, update: Update, backend: Optional[str] = None) -> str:
        """
        Pass message to AI with context about available tools.
        Runs autonomously in a loop up to 15 times if the AI uses tools.
        """
        system_prompt = self._build_system_prompt()
        history = self.context.get_history()
        current_message = message
        loop_history = history.copy()

        import re

        for iteration in range(15):
            # Send typing action to keep Telegram session alive
            try:
                from telegram.constants import ChatAction
                await update.message.chat.send_action(ChatAction.TYPING)
            except Exception:
                pass

            if hasattr(update, 'active_build') and update.active_build and update.active_build.get("cancelled"):
                logger.info("Build cancelled from dashboard.")
                if hasattr(update, 'build_sse'):
                    await update.build_sse("build_log", {"line": "[OVERSIGHT] Build cancellation requested. Stopping."})
                return "❌ Build cancelled."

            if hasattr(update, 'inject_queue') and update.inject_queue:
                injections = update.inject_queue.copy()
                update.inject_queue.clear()
                inject_text = "\n\n[OVERSIGHT INJECTION — HIGH PRIORITY]\n" + "\n".join(injections)
                current_message += inject_text
                if hasattr(update, 'build_sse'):
                    await update.build_sse("build_log", {"line": f"[OVERSIGHT] Applied {len(injections)} injected messages to context."})

            response = await self.ai.complete(
                system=system_prompt,
                history=loop_history,
                message=current_message,
                backend=backend
            )
            logger.debug(f"AI response (iter {iteration}): {response[:200]}...")

            # Check if AI attempted to use any tools
            file_read_match = re.search(r'READ_FILE:\s*([^\n]+)', response)
            fetch_url_match = re.search(r'FETCH_URL:\s*(https?://\S+)', response)
            shell_match = re.search(
                r'```(?:shell|bash|cmd|powershell)\n*(.*?)\n*```',
                response, re.DOTALL | re.IGNORECASE
            )
            execute_match = re.search(r'EXECUTE:\s*`([^`]+)`', response)
            # Python exec removed/commented out if pyshell skill is gone, or kept if engine supports it. 
            # Prompt says remove pyshell SKILL registration. 
            # tools/python_shell.py is kept. 
            # To be safe, I will allow PYEXEC if the code is simple or ask for confirm.
            
            pyexec_match = re.search(
                r'PYEXEC:\s*```(?:python)?\n*(.*?)\n*```',
                response, re.DOTALL | re.IGNORECASE
            )

            if not file_read_match and not shell_match and not execute_match and not fetch_url_match and not pyexec_match:
                # No tools called — AI has finished its task
                logger.debug("No tool patterns detected — returning final response to user")
                self.context.add_to_history("user", message)
                self.context.add_to_history("assistant", response)
                if self.settings.AUTO_EXTRACT_ENABLED:
                    asyncio.create_task(self._extract_facts(message, response))
                return response

            # --- Tool execution with safety checks ---
            tool_output = ""

            if file_read_match:
                path = file_read_match.group(1).strip()
                file_content = await self._handle_file_read(path)
                tool_output = f"File contents of `{path}`:\n\n{file_content}"

            elif fetch_url_match:
                url = fetch_url_match.group(1).strip()
                from tools.web_fetch import fetch, get_domain_policy

                policy, domain = get_domain_policy(url)

                if policy == "blocked":
                    tool_output = f"⛔ FETCH_URL blocked for `{domain}`. Internal/private addresses are not allowed."
                elif policy == "confirm":
                    self.context.set_pending_command(f"!fetch {url}")
                    final_resp = (
                        f"{response}\n\n"
                        f"🌐 *Fetch request:* `{url}`\n"
                        f"Domain `{domain}` requires confirmation.\n"
                        "Reply `yes` to fetch or `no` to cancel."
                    )
                    self.context.add_to_history("user", message)
                    self.context.add_to_history("assistant", final_resp)
                    return final_resp
                else:
                    try:
                        raw = await fetch(url)
                        tool_output = f"Fetched `{url}`:\n```\n{raw[:3000]}\n```"
                    except Exception as e:
                        tool_output = f"❌ Fetch failed for `{url}`: {e}"

            elif shell_match or execute_match:
                cmd = (shell_match.group(1).strip() if shell_match
                       else execute_match.group(1).strip())

                # *** SAFETY CHECK — enforce allowlist/confirm for AI-initiated commands ***
                is_safe, reason = self.shell.is_safe(cmd)

                if reason == "needs_confirmation":
                    # Destructive command — ask user before running
                    self.context.set_pending_command(cmd)
                    final_resp = (
                        f"{response}\n\n"
                        f"⚠️ *Confirm execution:*\n`{cmd}`\n\n"
                        f"Reply `yes` to run or `no` to cancel."
                    )
                    self.context.add_to_history("user", message)
                    self.context.add_to_history("assistant", final_resp)
                    return final_resp

                if reason == "elevated_pending":
                    # Command is not on allowlist — offer elevated approval gate
                    self.context.set_pending_command(cmd)
                    final_resp = (
                        f"{response}\n\n"
                        f"🔓 *Elevated command — not on standard allowlist:*\n`{cmd}`\n\n"
                        f"Reply `yes` to approve and run, or `no` to cancel."
                    )
                    self.context.add_to_history("user", message)
                    self.context.add_to_history("assistant", final_resp)
                    return final_resp

                if not is_safe:
                    # Not in allowlist — tell the AI it can't run this
                    tool_output = (
                        f"⛔ Command blocked by safety policy: `{cmd}`\n"
                        f"Only allowlisted commands can be run autonomously. "
                        f"The user can run this manually with `!shell {cmd}`."
                    )
                else:
                    # Safe to run autonomously
                    try:
                        await update.message.reply_text(
                            f"*(⚙️ Running: `{cmd[:60]}`...)*",
                            parse_mode='Markdown'
                        )
                    except Exception:
                        pass

                    result = await self.shell.execute(cmd)

                    # Truncate giant outputs so we don't blow out the LLM context window
                    if len(result) > 3000:
                        result = result[:3000] + "\n...(truncated)"

                    tool_output = f"Command output:\n```\n{result}\n```"

            elif pyexec_match:
                # If pyshell skill is removed, we might not have the executor accessible easily via skill_registry.
                # However, `tools.python_shell` is available.
                # I'll import it directly to support PYEXEC from the AI, assuming it's desired.
                # The strict "remove pyshell skill" might just mean the !pyshell user command wrapper.
                # But typically PYEXEC relies on the same underlying tool. 
                # I'll support it via direct tool usage.
                
                code = pyexec_match.group(1).strip()
                from tools.python_shell import PythonShell
                # We need a context-aware shell.
                
                # Check for confirmation first
                if self.context.get_pending_command():
                     tool_output = "⛔ A confirmation is already pending."
                else:
                     self.context.set_pending_command(f"!pyshell\n{code}")
                     final_resp = (
                         f"{response}\n\n"
                         f"⚠️ *Confirm Python Execution:*\n```python\n{code}\n```\n\n"
                         f"Reply `yes` to run or `no` to cancel."
                     )
                     self.context.add_to_history("user", message)
                     self.context.add_to_history("assistant", final_resp)
                     return final_resp

            # Prepare the next iteration
            loop_history.append({"role": "user", "content": current_message})
            loop_history.append({"role": "assistant", "content": response})
            current_message = (
                f"Tool result:\n{tool_output}\n\n"
                f"Given this result, please continue. If you have finished the task, "
                f"provide your final response to the user IN NATURAL LANGUAGE. "
                f"Do NOT output any more tools or commands if you are done."
            )

        # Fallback if it loops endlessly
        self.context.add_to_history("user", message)
        self.context.add_to_history("assistant", response)
        if self.settings.AUTO_EXTRACT_ENABLED:
            asyncio.create_task(self._extract_facts(message, response))
        return response + "\n\n*(Reached maximum 15 autonomous iterations)*"

    async def _extract_facts(self, user_message: str, assistant_response: str):
        try:
            if not self.settings.AUTO_EXTRACT_ENABLED:
                return
            if not self.memory_store:
                return
            await self.extractor.extract_and_store(user_message, assistant_response)
        except Exception as e:
            logger.warning(f"Auto extraction failed (ignored): {e}")

    async def _handle_shell(self, cmd: str, update: Update) -> str:
        """Direct shell execution with safety checks"""
        is_safe, reason = self.shell.is_safe(cmd)

        if reason == "needs_confirmation":
            self.context.set_pending_command(cmd)
            return f"⚠️ Confirm: `{cmd}`\nReply `yes` to execute or `no` to cancel."

        if reason == "elevated_pending":
            self.context.set_pending_command(cmd)
            return (
                f"🔓 *Elevated command* — `{cmd}` is not on the standard allowlist.\n"
                f"Reply `yes` to approve and run, or `no` to cancel."
            )

        if not is_safe:
            return (
                f"⛔ Command not in allowlist: `{cmd}`\n"
                f"Add to SHELL_ALLOWLIST in settings if intended."
            )

        result = await self.shell.execute(cmd)
        return f"```\n{result}\n```"

    async def _handle_file_read(self, path: str) -> str:
        content = await self.files.read(path)
        if len(content) > 3000:
            return f"```\n{content[:3000]}\n```\n_(truncated, {len(content)} chars total)_"
        return f"```\n{content}\n```"

    async def _handle_file_write(self, path: str, content: str) -> str:
        result = await self.files.write(path, content)
        return result

    async def transcribe_audio(self, audio_path: Path) -> str:
        """Transcribe voice note using Whisper"""
        try:
            import whisper
            model = whisper.load_model(
                self.settings.WHISPER_MODEL,
                device=self.settings.WHISPER_DEVICE
            )
            result = model.transcribe(str(audio_path))
            return result["text"].strip()
        except Exception as e:
            logger.error(f"Whisper transcription failed: {e}")
            return f"[Transcription failed: {e}]"

    def _build_system_prompt(self) -> str:
        """Build system prompt with context about the environment"""
        return """You are Canopy Seed — a project advisor and architect.
You help Travis turn ideas into working software specifications.

## Your Capabilities
- Read/write files in allowed paths
- Execute Windows shell/PowerShell commands
- Run Python scripts for analysis
- Check system status
- Perform web research via `!fetch` or `FETCH_URL`

## Verified Tools
- `!tool process <name>`
- `!tool service <name>`
- `!tool port <host> <port>`
- `!tool ping <host>`
- `!tool path <path>`
- `!tool find <dir> <pattern>`
- `!tool help`

## Guidelines
- Be friendly, encouraging, and clear.
- Understand what the user wants to build.
- Ask clarifying questions one by one.
- When you have enough info, you will eventually generate a project spec.
- Use `FETCH_URL` to look up documentation or tech details if needed.

## CRITICAL RULE: USE TOOLS, NEVER GUESS
- Verify files exist before reading them.
- Don't invent API endpoints.
"""
