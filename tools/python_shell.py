# tools/python_shell.py
"""
Persistent Python shell executor for Canopy Seed Tool Call.
Maintains a single Python subprocess per session with shared state.
Results are returned as compact strings suitable for AI context consumption.

Subprocess approach chosen: multiprocessing
A long-lived worker process running a loop to execute code in a persistent
namespace dictionary, using a Queue for input and output, is used. This is
more reliable on Windows than `subprocess.Popen` with stdin/stdout buffering,
which can avoid deadlocks from reading streams without known EOFs.
"""

import asyncio
import io
import sys
import traceback
import queue
import multiprocessing as mp
from typing import Optional
import logging

logger = logging.getLogger(__name__)

def _worker_loop(task_queue: mp.Queue, result_queue: mp.Queue):
    """
    The worker process loop. It maintains a persistent namespace.
    """
    # Create persistent namespace
    namespace = {}
    
    while True:
        try:
            # Wait for code to execute
            code = task_queue.get()
            if code is None:
                # Exit signal
                break
                
            # Capture stdout and stderr
            output_buffer = io.StringIO()
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            sys.stdout = output_buffer
            sys.stderr = output_buffer
            
            error_traceback = ""
            try:
                # Compile and run code in the shared namespace
                exec(code, namespace, namespace)
            except BaseException:
                error_traceback = traceback.format_exc()
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                
            output = output_buffer.getvalue()
            if error_traceback:
                output += "\n" + error_traceback
                
            result_queue.put(output)
            
        except Exception as e:
            # Fatal error in worker loop
            result_queue.put(f"⚠️ Fatal worker error: {e}")
            break

class PythonShellExecutor:
    def __init__(self, timeout: int = 15, max_output_chars: int = 2000):
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self._process: Optional[mp.Process] = None
        self._task_queue: Optional[mp.Queue] = None
        self._result_queue: Optional[mp.Queue] = None
        self._start_worker()

    def _start_worker(self):
        self._task_queue = mp.Queue()
        self._result_queue = mp.Queue()
        self._process = mp.Process(target=_worker_loop, args=(self._task_queue, self._result_queue), daemon=True)
        self._process.start()
        logger.debug("PythonShellExecutor worker started.")

    def _stop_worker(self):
        if self._process and self._process.is_alive():
            try:
                self._task_queue.put(None) # try graceful exit
                self._process.join(timeout=1.0)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=1.0)
            except Exception as e:
                logger.error(f"Error stopping worker: {e}")
        self._process = None
        self._task_queue = None
        self._result_queue = None

    async def execute(self, code: str) -> str:
        """
        Execute Python code in the persistent shell.
        Returns: stdout + stderr as a single string, truncated to max_output_chars.
        Raises: TimeoutError if execution exceeds self.timeout seconds.
        """
        if not self.is_alive():
            self._start_worker()

        # Send code to worker
        self._task_queue.put(code)
        
        async def poll_result():
            while True:
                try:
                    # Blocking get with short timeout in a thread
                    return await asyncio.to_thread(self._result_queue.get, True, 0.1)
                except queue.Empty:
                    if not self._process.is_alive():
                        raise RuntimeError("Worker process died unexpectedly")
                    # Allow asyncio to loop
                    continue

        try:
            output = await asyncio.wait_for(poll_result(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self._stop_worker()
            self._start_worker()
            return f"⏱️ Execution timed out after {self.timeout}s. Shell restarted."
        except Exception as e:
            self._stop_worker()
            self._start_worker()
            return "⚠️ Shell crashed and was restarted. Session state cleared."

        output = str(output).strip()
        if not output:
            output = "(no output)"

        if len(output) > self.max_output_chars:
            output = output[:self.max_output_chars] + "\n...[truncated]"

        return output

    async def reset(self) -> str:
        """Kill and restart the subprocess, clearing all session state."""
        self._stop_worker()
        self._start_worker()
        return "Session state cleared. Shell restarted."

    def is_alive(self) -> bool:
        """True if subprocess is running and responsive."""
        return self._process is not None and self._process.is_alive()
