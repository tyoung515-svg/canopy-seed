"""
File Tools - Read/write with path safety checks
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class FileTool:
    def __init__(self, settings):
        self.settings = settings

    def _is_allowed_read(self, path: str) -> bool:
        p = Path(path).resolve()
        return any(
            str(p).startswith(str(Path(allowed).resolve()))
            for allowed in self.settings.ALLOWED_READ_PATHS
        )

    def _is_allowed_write(self, path: str) -> bool:
        p = Path(path).resolve()
        return any(
            str(p).startswith(str(Path(allowed).resolve()))
            for allowed in self.settings.ALLOWED_WRITE_PATHS
        )

    async def read(self, path: str) -> str:
        if not self._is_allowed_read(path):
            return f"⛔ Read not allowed: {path}\nAllowed paths: {self.settings.ALLOWED_READ_PATHS}"
        
        try:
            p = Path(path)
            if not p.exists():
                return f"File not found: {path}"
            if p.is_dir():
                # List directory
                files = sorted(p.iterdir())
                listing = "\n".join(
                    f"{'📁' if f.is_dir() else '📄'} {f.name}" 
                    for f in files[:50]
                )
                return f"Directory listing: {path}\n\n{listing}"
            
            return p.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            return f"Read error: {e}"

    async def write(self, path: str, content: str) -> str:
        if not self._is_allowed_write(path):
            return f"⛔ Write not allowed: {path}\nAllowed paths: {self.settings.ALLOWED_WRITE_PATHS}"
        
        try:
            p = Path(path)
            if not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
            return f"✅ Written: {path} ({len(content)} chars)"
        except Exception as e:
            return f"Write error: {e}"

    async def write_elevated(self, path: str, content: str) -> str:
        """
        Write to any path — caller must have already obtained explicit approval
        via set_pending_command / confirmation flow. No path gating is applied here.
        """
        try:
            p = Path(path)
            if not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"✅ Written (elevated): {path} ({len(content)} chars)"
        except Exception as e:
            return f"Write error: {e}"

    async def diff_preview(self, path: str, new_content: str) -> str:
        """
        Show a unified diff between the current file and new_content.
        Returns the diff as a string. Does NOT write anything.
        """
        if not self._is_allowed_read(path):
            return f"⛔ Read not allowed: {path}"

        import difflib

        try:
            p = Path(path)
            if not p.exists():
                return f"File not found: {path}"
            original = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            proposed = new_content.splitlines(keepends=True)
            diff = list(
                difflib.unified_diff(
                    original,
                    proposed,
                    fromfile=f"a/{p.name}",
                    tofile=f"b/{p.name}",
                    lineterm="",
                )
            )
            if not diff:
                return "No changes."
            diff_str = "\n".join(diff)
            if len(diff_str) > 3000:
                diff_str = diff_str[:3000] + "\n...(truncated)"
            return f"```diff\n{diff_str}\n```"
        except Exception as e:
            return f"Diff error: {e}"

    async def search(self, path: str, pattern: str, case_sensitive: bool = False) -> str:
        """
        Search for a text pattern in all files under path.
        Returns matching file paths and line numbers.
        Respects allowed_read_paths.
        """
        if not self._is_allowed_read(path):
            return f"⛔ Read not allowed: {path}"

        import re

        try:
            root = Path(path)
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern, flags)
            matches = []
            files_checked = 0

            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                if f.suffix in (".pyc", ".exe", ".dll", ".pyd", ".bin", ".db"):
                    continue
                files_checked += 1
                if files_checked > 500:
                    matches.append("_(Search limit reached: 500 files)_")
                    break
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                    for i, line in enumerate(text.splitlines(), 1):
                        if regex.search(line):
                            rel = f.relative_to(root)
                            matches.append(f"{rel}:{i}: {line.strip()[:120]}")
                            if len(matches) > 50:
                                matches.append("_(Result limit reached: 50 matches)_")
                                break
                except Exception:
                    continue
                if len(matches) > 50:
                    break

            if not matches:
                return f"No matches for `{pattern}` in `{path}`"
            return f"Found {len(matches)} match(es):\n```\n" + "\n".join(matches) + "\n```"
        except re.error as e:
            return f"Invalid regex: {e}"
        except Exception as e:
            return f"Search error: {e}"

    async def find_files(self, path: str, glob_pattern: str) -> str:
        """
        Find files matching a glob pattern under path.
        Example: find_files("C:/Users/tyoun/Desktop", "*.pdf")
        """
        if not self._is_allowed_read(path):
            return f"⛔ Read not allowed: {path}"

        try:
            root = Path(path)
            matches = list(root.rglob(glob_pattern))[:100]
            if not matches:
                return f"No files matching `{glob_pattern}` in `{path}`"
            lines = [str(m.relative_to(root)) for m in matches]
            header = f"Found {len(matches)} file(s) matching `{glob_pattern}`:"
            return header + "\n```\n" + "\n".join(lines) + "\n```"
        except Exception as e:
            return f"Find error: {e}"

    async def edit_line(self, path: str, line_number: int, new_line: str) -> str:
        """
        Replace a single line in a file (1-indexed).
        Requires path to be in allowed_write_paths.
        Shows a 3-line context diff and writes the change.
        """
        if not self._is_allowed_write(path):
            return f"⛔ Write not allowed: {path}"

        try:
            p = Path(path)
            if not p.exists():
                return f"File not found: {path}"
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if line_number < 1 or line_number > len(lines):
                return f"Line {line_number} out of range (file has {len(lines)} lines)"

            old_line = lines[line_number - 1].rstrip("\n")
            lines[line_number - 1] = new_line + "\n"
            p.write_text("".join(lines), encoding="utf-8")

            return (
                f"✅ Line {line_number} updated in `{p.name}`\n"
                f"  Before: `{old_line[:120]}`\n"
                f"  After:  `{new_line[:120]}`"
            )
        except Exception as e:
            return f"Edit error: {e}"
