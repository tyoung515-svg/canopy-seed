# core/nl_action.py
"""
NL Action Classifier — dynamic catch-all for OS-level natural language requests.
Single-shot AI call to determine if a message maps to a Windows shell command.
Returns the command string if actionable, None otherwise.
"""

ACTION_CLASSIFIER_PROMPT = """You are a Windows shell command extractor.

Your only job: determine if the user's message is a request to perform a concrete,
single Windows shell/PowerShell action on a PC.

If YES: respond with ONLY the Windows shell command to execute. Nothing else. No explanation.
If NO or UNSURE: respond with only the word: NONE

Rules:
- Only emit commands that are a single logical action
- Use Windows syntax only (start, taskkill, powershell, etc.)
- Never emit multi-step scripts or chained commands with &&
- Never emit commands that modify system files, registry, or security settings
- Never emit commands that require elevation (runas, admin)
- If the request is a question, conversation, or analysis task → NONE
- If the request is ambiguous → NONE

Examples:
User: "open chrome" → start chrome
User: "close spotify" → taskkill /IM Spotify.exe /F
User: "mute the volume" → powershell -c "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"
User: "what's the weather" → NONE  (handled by weather skill)
User: "how do I use git" → NONE  (question, not an action)
User: "analyze this data" → NONE  (analysis task)
User: "open file explorer to downloads" → start explorer %USERPROFILE%\\Downloads
User: "turn off wifi" → netsh interface set interface "Wi-Fi" disabled
"""

async def classify_as_action(message: str, ai_backend) -> str | None:
    """
    Ask the AI if this message maps to a single Windows shell command.
    
    Returns:
        str — the Windows command to run, if actionable
        None — if not actionable or AI is unsure
    """
    try:
        response = await ai_backend.complete(
            system=ACTION_CLASSIFIER_PROMPT,
            message=message,
            history=[],
            backend="lmstudio",   # always use local model — keep cost at zero
            max_tokens=64,        # command only, no prose needed
        )
        response = response.strip()
        if not response or response.upper() == "NONE" or len(response) > 200:
            return None
            
        # Sanity check — must look like a real command, not prose
        first_word = response.split()[0].lower()
        KNOWN_CMD_STARTS = {
            "start", "taskkill", "powershell", "cmd", "netsh", "reg",
            "sc", "net", "wmic", "rundll32", "explorer", "msiexec",
            "shutdown", "ping", "ipconfig", "nslookup", "curl",
        }
        
        # also allow executable names that end in .exe e.g. "notepad.exe"
        if first_word not in KNOWN_CMD_STARTS and not response.startswith("%") and not first_word.endswith(".exe"):
            return None
            
        return response
    except Exception:
        return None
