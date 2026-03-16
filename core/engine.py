"""
Canopy Seed runtime engine.
Contains bot lifecycle, handlers, and persistence wrapper.
"""

import asyncio
import atexit
import ctypes
import json
import logging
import msvcrt
import os
import psutil
import shutil
import subprocess
import sys
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config.settings import Settings
from core.agent_pool import AgentPool
from core.brain_state import BrainStateManager
from core.context import AgentContext
from core.logger import setup_logging
from core.router import MessageRouter
# from memory import core_db # Used instead of product_db
from memory.canopy import MemoryStore
from skills.registry import SkillRegistry


_settings = Settings()
setup_logging()
logger = logging.getLogger(__name__)

_single_instance_mutex = None
_single_instance_lock_handle = None
_pid_guard_path = Path("logs/canopy.pid")


def _acquire_single_instance_mutex() -> bool:
    global _single_instance_mutex

    mutex_name = "Global\\Canopy_Seed_SingleInstance"
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_uint32

    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        logger.warning("Could not create single-instance mutex; continuing without lock.")
        return True

    _single_instance_mutex = handle
    ERROR_ALREADY_EXISTS = 183
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        logger.error("Another Canopy Seed instance is already running. Exiting this process.")
        return False

    return True


def _acquire_single_instance_file_lock() -> bool:
    global _single_instance_lock_handle

    try:
        lock_path = Path("logs/canopy.instance.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        handle.seek(0)
        handle.write(str(os.getpid()))
        handle.flush()
        handle.seek(0)

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            logger.error("Another Canopy Seed instance holds the lock file. Exiting this process.")
            return False

        _single_instance_lock_handle = handle
        return True
    except Exception as e:
        logger.warning(f"Could not acquire single-instance file lock; continuing without it: {e}")
        return True


def _acquire_pid_guard() -> bool:
    _pid_guard_path.parent.mkdir(parents=True, exist_ok=True)

    if _pid_guard_path.exists():
        try:
            existing_pid = int(_pid_guard_path.read_text(encoding="utf-8").strip())
            if existing_pid and psutil.pid_exists(existing_pid):
                process = psutil.Process(existing_pid)
                cmdline = " ".join(process.cmdline()).lower()
                if "agent.py" in cmdline:
                    logger.error(f"Canopy Seed already running with PID {existing_pid}. Exiting this process.")
                    return False
        except Exception:
            pass

    _pid_guard_path.write_text(str(os.getpid()), encoding="utf-8")

    def _cleanup_pid_guard():
        try:
            if _pid_guard_path.exists():
                current = _pid_guard_path.read_text(encoding="utf-8").strip()
                if current == str(os.getpid()):
                    _pid_guard_path.unlink()
        except Exception:
            pass

    atexit.register(_cleanup_pid_guard)
    return True


class CanopySeed:
    def __init__(self):
        self.settings = Settings()
        self.memory_store = MemoryStore("memory/canopy.db")
        self.context = AgentContext(self.settings, memory_store=self.memory_store)
        self.skill_registry = SkillRegistry()
        self.router = MessageRouter(
            self.settings,
            self.context,
            self.skill_registry,
            self.memory_store,
        )
        self.app = None
        self.restart_requested = False
        self.model_lock = None  # None, "claude", "gemini", "lmstudio", "think"

        from core.api_server import DashboardAPI
        self.dashboard_api = None
        if getattr(self.settings, 'DASHBOARD_API_ENABLED', True):
            self.dashboard_api = DashboardAPI(
                settings=self.settings,
                context=self.context,
                router=self.router,
                ai_backend=self.router.ai,
            )

        # Dev Hub: Agent pool + brain state
        # AgentPool and BrainStateManager are imported at module level
        self.brain_state = BrainStateManager(root_dir=".")
        self.agent_pool = AgentPool(self.router.ai, self.brain_state)
        
        if self.dashboard_api:
            self.dashboard_api.agent_pool = self.agent_pool
            self.dashboard_api.brain_state = self.brain_state

    def _is_authorized(self, user_id: int) -> bool:
        """Only respond to whitelisted Telegram user IDs"""
        return user_id in self.settings.AUTHORIZED_USER_IDS

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            logger.warning(f"Unauthorized access attempt from user_id: {user_id}")
            await update.message.reply_text("Unauthorized.")
            return

        if self.model_lock:
            if update.message.text and update.message.text.lower().startswith("!release"):
                await self.handle_release(update, context)
                return

            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING
            )
            logger.info(f"Message from {user_id} (LOCKED to {self.model_lock}): {update.message.text[:100]}...")
            try:
                response = await self.router.route(
                    update.message.text,
                    update,
                    force_backend=self.model_lock
                )
                if response:
                    await update.message.reply_text(response, parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Error during model lock override: {e}", exc_info=True)
                await update.message.reply_text(f"Error with locked model '{self.model_lock}': {e}")
            return

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING
        )

        if update.message.voice:
            await self._handle_voice(update, context)
            return

        user_message = update.message.text
        logger.info(f"Message from {user_id}: {user_message[:100]}...")

        try:
            response = await self.router.route(user_message, update)
            if response:
                chunks = (
                    [response[i:i+4096] for i in range(0, len(response), 4096)]
                    if len(response) > 4096 else [response]
                )
                for chunk in chunks:
                    try:
                        await update.message.reply_text(chunk, parse_mode='Markdown')
                    except Exception as parse_err:
                        logger.warning(f"Markdown parse failed, falling back to plain text: {parse_err}")
                        await update.message.reply_text(chunk)
        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            await update.message.reply_text(f"Error: {str(e)}")

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Transcribe voice note and process as text"""
        try:
            await update.message.reply_text("🎙️ Transcribing voice note...")

            voice_file = await update.message.voice.get_file()
            voice_path = Path(f"logs/voice_{update.message.message_id}.ogg")
            await voice_file.download_to_drive(voice_path)

            transcription = await self.router.transcribe_audio(voice_path)
            voice_path.unlink(missing_ok=True)

            await update.message.reply_text(
                f"📝 *Heard:* {transcription}", parse_mode='Markdown'
            )

            response = await self.router.route(transcription, update)
            chunks = (
                [response[i:i+4096] for i in range(0, len(response), 4096)]
                if len(response) > 4096 else [response]
            )
            for chunk in chunks:
                try:
                    await update.message.reply_text(chunk, parse_mode='Markdown')
                except Exception as parse_err:
                    logger.warning(
                        f"Markdown parse failed in voice handler, falling back: {parse_err}"
                    )
                    await update.message.reply_text(chunk)
        except Exception as e:
            logger.error(f"Voice handling error: {e}", exc_info=True)
            await update.message.reply_text(f"Voice error: {str(e)}")

    async def handle_screenshot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        from tools.screenshot_tool import capture_screen
        result = await capture_screen(update)
        if result:
            await update.message.reply_text(result)

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return

        # Simplified for Canopy Seed - just route to vision backend if available
        # or acknowledge receipt.
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING
        )

        try:
            photo = update.message.photo[-1]
            file_obj = await context.bot.get_file(photo.file_id)

            extract_dir = Path("exports/photos")
            extract_dir.mkdir(parents=True, exist_ok=True)

            temp_path = extract_dir / f"telegram_photo_{photo.file_id}.jpg"
            await file_obj.download_to_drive(str(temp_path))
            
            # Read image for vision processing
            with open(temp_path, "rb") as f:
                image_bytes = f.read()
            
            caption = update.message.caption or "What is in this image?"
            
            # Route to router which handles vision
            response = await self.router.route_with_image(
                message=caption,
                image_bytes=image_bytes,
                mime_type="image/jpeg",
                update=update
            )
            
            # Clean up temp file
            if temp_path.exists():
                temp_path.unlink()

            if response:
                await update.message.reply_text(response, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Photo handling error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Photo processing failed: {str(e)}")

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return

        doc = update.message.document
        if not doc:
            return
        
        # Canopy Seed: Just save to generic downloads or exports for now
        file_name = doc.file_name or f"document_{doc.file_id}"
        
        try:
            file_obj = await context.bot.get_file(doc.file_id)
            
            dl_dir = Path("exports/downloads")
            dl_dir.mkdir(parents=True, exist_ok=True)
            
            target_path = dl_dir / file_name
            await file_obj.download_to_drive(str(target_path))
            
            await update.message.reply_text(f"💾 File saved to `{target_path}`", parse_mode="Markdown")
            
        except Exception as e:
             logger.error(f"Document handling error: {e}", exc_info=True)
             await update.message.reply_text(f"❌ Download failed: {str(e)}")

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        from tools.system_tools import get_system_status
        status = await get_system_status()
        backend_status = await self.router.ai.get_backend_status()
        await update.message.reply_text(
            f"{status}\n\n{backend_status}", parse_mode='Markdown'
        )

    async def handle_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        self.context.clear_history()
        await update.message.reply_text(
            "🧹 Conversation history cleared. Starting fresh with lower context size."
        )

    async def handle_lock(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return

        args = context.args
        if not args:
            await update.message.reply_text(
                "Please specify a model to lock. Options: `claude`, `gemini`, `lmstudio`, `think`.",
                parse_mode='Markdown'
            )
            return

        model_choice = args[0].lower()
        valid_models = ["claude", "gemini", "lmstudio", "worker"]
        if model_choice in valid_models:
            self.model_lock = model_choice
            await update.message.reply_text(
                f"🔒 Model locked to `{self.model_lock}`. All messages will be sent to this model.\n"
                "Use `/release` to unlock.",
                parse_mode='Markdown'
            )
            logger.info(f"Model locked to {self.model_lock} by user {update.effective_user.id}")
        else:
            await update.message.reply_text(
                f"Invalid model. Please choose from: `{', '.join(valid_models)}`.",
                parse_mode='Markdown'
            )

    async def handle_release(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return

        if self.model_lock:
            self.model_lock = None
            await update.message.reply_text("🔓 Model lock released. Returning to automatic routing.")
            logger.info(f"Model lock released by user {update.effective_user.id}")
        else:
            await update.message.reply_text("No model is currently locked.")

    async def handle_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        await update.message.reply_text("🔄 Restarting Canopy Seed... I'll be back in a few seconds.")
        logger.info("Restarting...")

        if getattr(self, "dashboard_api", None):
            await self.dashboard_api.stop()

        self.restart_requested = True
        try:
            if self.app:
                self.app.stop_running()
        except Exception as e:
            logger.warning(f"Error while signaling stop during restart: {e}")

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        dynamic_skills = []
        for skill in self.skill_registry.list_skills():
            if not getattr(skill, "enabled", True):
                continue
            triggers = ", ".join(f"!{trigger}" for trigger in getattr(skill, "triggers", []))
            if triggers:
                dynamic_skills.append(f"{triggers} — {getattr(skill, 'description', '')}")

        skills_block = "\n".join(dynamic_skills) if dynamic_skills else "(none)"

        help_text = f"""
*Canopy Seed Commands*

/status - System status (CPU, RAM, GPU, disk, AI backends)
/restart - Restart Canopy Seed remotely
/screenshot - Grab screenshot of desktop
/clear - Clear conversation history to free up token space
/help - This message

*Model Persistence:*
`/lock <model>` - Lock conversation to a specific model.
  (e.g., `/lock claude`, `/lock gemini`)
`/release` - Release the lock and return to auto-routing.

*AI Backend Prefixes:*
`!claude <prompt>` — send to Claude API (auto-escalates for complex tasks)
`!gemini <prompt>` — send to Google Gemini
`!deep <prompt>` or `!think <prompt>` — Claude deep analysis

*Direct Tool Prefixes:*
`!shell <cmd>` — run a shell command
`!file <path>` — read a file
`!write <path>\\n<content>` — write a file
`!tool <name> [args]` — run a verified system tool
`!audit [focus]` — run a full security audit via Nemotron

*Memory Commands:*
`!remember #tag1 #tag2 <text>` — save a note into persistent memory
`!recall <search terms>` — search memory notes
`!recall recent` — list the 10 most recent notes
`!recall #tag` — filter notes by tag
`!forget <note_id>` — delete a note by ID

*Registered Skills:*
{skills_block}

*Natural Language:*
Just type, send a voice note, or send a photo. Examples:
- "What files are on my Desktop?"
- "Check GPU temperature"

*Photos:*
Send any image — Gemini Vision will analyze it.
Add a caption to guide the analysis.

*Verified Tools:*
`!tool help` — full tool list
`!tool process <n>` — check process
`!tool port <host> <port>` — test port
`!tool ping <host>` — ping host
`!tool path <path>` — check file exists
`!tool tasks` — list scheduled tasks
"""
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def _post_init(self, application):
        """Called after Application.initialize() — start dashboard API."""
        if getattr(self, "dashboard_api", None):
            try:
                await self.dashboard_api.start()
            except Exception as e:
                logger.warning(f"Dashboard API failed to start: {e}")

    def run(self):
        logger.info("Starting Canopy Seed...")

        self.app = (
            Application.builder()
            .token(self.settings.TELEGRAM_BOT_TOKEN)
            .connect_timeout(30.0)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .pool_timeout(30.0)
            .post_init(self._post_init)
            .build()
        )

        self.app.add_handler(CommandHandler("start", self.handle_help))
        self.app.add_handler(CommandHandler("help", self.handle_help))
        self.app.add_handler(CommandHandler("status", self.handle_status))
        self.app.add_handler(CommandHandler("restart", self.handle_restart))
        self.app.add_handler(CommandHandler("screenshot", self.handle_screenshot))
        self.app.add_handler(CommandHandler("clear", self.handle_clear))
        self.app.add_handler(CommandHandler("lock", self.handle_lock))
        self.app.add_handler(CommandHandler("release", self.handle_release))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.app.add_handler(MessageHandler(filters.VOICE, self.handle_message))
        self.app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))

        logger.info("Canopy Seed running. Polling for messages...")
        logger.info("Bot started")

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        self.app.run_polling(drop_pending_updates=True)


def _restart_process() -> None:
    agent_entrypoint = Path(__file__).resolve().parent.parent / "agent.py"
    python_executable = sys.executable
    argv = [python_executable, str(agent_entrypoint)]

    logger.info("Restarting... launching fresh process")

    try:
        subprocess.Popen(
            argv,
            cwd=str(agent_entrypoint.parent),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        logger.info("Restarting... child process launched, exiting parent")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Restart launch failed: {e}")


def run_with_persistence():
    import signal
    import time

    if not _acquire_single_instance_mutex():
        return
    if not _acquire_single_instance_file_lock():
        return
    if not _acquire_pid_guard():
        return

    settings = Settings()
    max_restarts = settings.MAX_RESTARTS
    restart_delay = settings.RESTART_DELAY
    restart_reset_after = settings.RESTART_RESET_AFTER

    restart_count = 0
    shutdown_requested = False
    last_start_time = None

    def _shutdown_handler(signum, frame):
        nonlocal shutdown_requested
        logger.info(f"Shutdown signal received ({signum}). Exiting cleanly...")
        shutdown_requested = True
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)
    try:
        signal.signal(signal.SIGBREAK, _shutdown_handler)
    except AttributeError:
        pass

    logger.info("Canopy Seed persistence wrapper started. Press Ctrl+C to stop.")

    while not shutdown_requested:
        if restart_count >= max_restarts:
            logger.error(f"Canopy Seed crashed {max_restarts} times. Giving up.")
            break

        try:
            last_start_time = time.time()
            if restart_count > 0:
                logger.warning(f"Restarting Canopy Seed (attempt {restart_count}/{max_restarts})...")
                time.sleep(restart_delay)

            agent = CanopySeed()
            agent.run()

            if agent.restart_requested:
                logger.info("Restarting... app shutdown complete")
                _restart_process()

            logger.info("Canopy Seed exited cleanly.")
            break

        except SystemExit as e:
            if e.code == 0:
                logger.info("Clean exit via sys.exit(0).")
                break
            logger.warning(f"Canopy Seed exited with code {e.code}, restarting...")
            restart_count += 1

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt. Shutting down.")
            break

        except Exception as e:
            uptime = time.time() - (last_start_time or time.time())
            logger.error(f"Canopy Seed crashed after {uptime:.0f}s: {e}", exc_info=True)

            if uptime > restart_reset_after:
                logger.info(f"Was stable for {uptime:.0f}s, resetting restart counter.")
                restart_count = 0
            else:
                restart_count += 1

    logger.info("Canopy Seed persistence wrapper stopped.")
