"""
Canopy Seed — Authoritative SDK & Model Reference
--------------------------------------------------
Single source of truth for Google AI SDK usage injected into all agent
prompts (dev swarm, Big Fixer, Canopy Manager, contract generator).

When Gemini releases new models, update GOOGLE_AI_SDK_REFERENCE["models"]
here and every prompt location picks up the change automatically.
"""

# ── Google AI SDK reference ────────────────────────────────────────────────

GOOGLE_AI_SDK_REFERENCE = {
    # pip package name (NOT google-generativeai)
    "package": "google-genai",

    # Correct import
    "import": "import google.genai as genai",

    # Client initialisation
    "client": "client = genai.Client(api_key=key)",

    # Current live models (March 2026) — verified against pricing page
    # Stable: gemini-2.5-flash, gemini-2.5-pro, gemini-2.5-flash-lite
    # Preview: gemini-3-flash-preview, gemini-3.1-pro-preview (pipeline only)
    "models": {
        "fast":    "gemini-2.5-flash",       # stable, current, cheapest capable
        "quality": "gemini-2.5-pro",         # stable, current, best quality
    },

    # Packages that must never appear in generated code
    "deprecated_packages": ["google.generativeai"],

    # Model strings that must never appear in generated code
    "deprecated_models": [
        "gemini-3.0-flash",   # hallucinated — never existed
        "gemini-2.0-flash",   # deprecated, shuts down June 2026, blocked for new users
        "gemini-2.0-flash-lite",  # deprecated, shuts down June 2026
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "imagen-3.0-generate-002",  # deprecated Nov 2025 on Gemini Developer API
        "imagen-3.0-generate-001",  # deprecated
        "imagen-3.0-fast-generate-001",  # never worked on Developer API (Vertex only)
    ],

    # Image generation models (Imagen 4 — current as of March 2026)
    "image_models": {
        "standard": "imagen-4.0-generate-001",
        "ultra":    "imagen-4.0-ultra-generate-001",
        "fast":     "imagen-4.0-fast-generate-001",
    },
}


def render_sdk_guidance(context: str = "swarm") -> str:
    """
    Return a concise SDK reference block for injection into agent prompts.

    context:
        "swarm"    — dev agent writing a new source file (most detailed)
        "manager"  — Manager debugging a running app (focused on failure patterns)
        "fixer"    — Big Fixer repairing test failures (anti-web-search emphasis)
        "contract" — Phase 0 contract generator (dependency + mock guidance)
    """
    ref = GOOGLE_AI_SDK_REFERENCE
    fast  = ref["models"]["fast"]
    qual  = ref["models"]["quality"]
    img   = ref["image_models"]["standard"]
    depr_models  = ", ".join(f"`{m}`" for m in ref["deprecated_models"])
    depr_pkgs    = ", ".join(f"`{p}`" for p in ref["deprecated_packages"])

    _img_block = (
        f"- Image generation (Imagen 4): `client.models.generate_images("
        f"model='{img}', prompt=..., "
        f"config=types.GenerateImagesConfig(number_of_images=1))` "
        f"→ `response.generated_images[0].image.image_bytes`\n"
        f"  SYNC ONLY — `generate_images` has NO async/aio variant. Call it synchronously.\n"
        f"  No `http_options` or `api_version` needed — plain `genai.Client(api_key=key)` works.\n"
        f"  `imagen-3.0-*` is DEPRECATED (removed Nov 2025) — always use `{img}`.\n"
        f"  Config options: `number_of_images` (1-4), `aspect_ratio` ('1:1','3:4','4:3','9:16','16:9'), "
        f"`image_size` ('1K','2K')\n"
    )

    base = (
        f"## Google AI SDK (authoritative — do not web-search for this)\n"
        f"- Package: `{ref['package']}` — `{ref['import']}; from google.genai import types`\n"
        f"- Client: `{ref['client']}`\n"
        f"- Current models: `{fast}` (fast/cheap), `{qual}` (best quality)\n"
        f"- DEPRECATED — never use: {depr_models}, {depr_pkgs}\n"
    )

    if context == "swarm":
        return base + (
            f"- Generate content: `client.models.generate_content(model='{fast}', contents=[...])`\n"
            f"- Structured output: `config=types.GenerateContentConfig("
            f"response_mime_type='application/json', response_schema=YourPydanticModel)` "
            f"→ check `response.parsed` first, fall back to `json.loads(response.text)`\n"
            f"- PDF input: `types.Part.from_bytes(data=pdf_bytes, mime_type='application/pdf')`\n"
            f"- WRONG API: `genai.GenerativeModel()` — that is the deprecated beta v1 API\n"
            + _img_block
        )

    if context == "manager":
        return (
            f"## Google AI SDK (authoritative — do not web-search for this)\n"
            f"- Package: `{ref['package']}` — `{ref['import']}; from google.genai import types`\n"
            f"- Client: `{ref['client']}` — create ONCE at module/class init, reuse for all calls.\n"
            f"  **Common bug**: creating client per-request causes 'Cannot send a request, as the "
            f"client has been closed' — fix by storing as `self.client` in `__init__`.\n"
            f"- Current models: `{fast}` (fast/cheap), `{qual}` (best quality)\n"
            f"- DEPRECATED — never use: {depr_models}, {depr_pkgs}\n"
            f"- API: `client.models.generate_content(model='{fast}', contents=[...])`\n"
            f"- WRONG API: `genai.GenerativeModel()` — that is the deprecated beta v1 API.\n"
            + _img_block
        )

    if context == "fixer":
        return base + (
            f"- API call: `client.models.generate_content(model='{fast}', contents=[...])`\n"
            f"- Do NOT web_search for Google AI SDK patterns — use this reference.\n"
            f"- If code uses any deprecated model/package above, that IS the bug — replace it.\n"
            + _img_block
        )

    if context == "contract":
        return (
            f"## Google AI SDK Contract Requirements\n"
            f"- Dependency: `{ref['package']}` in pyproject.toml (NOT `google-generativeai`)\n"
            f"- Import: `{ref['import']}; from google.genai import types`\n"
            f"- Client: `{ref['client']}`\n"
            f"- Model: `{fast}` for all extraction/generation tasks\n"
            f"- Image model: `{img}` (NOT `imagen-3.0-*` which is deprecated)\n"
            f"- Image gen is SYNC only: `client.models.generate_images(...)` — no aio variant\n"
            f"- Test mocks must patch `client.models.generate_content`, "
            f"NOT `genai.GenerativeModel`\n"
            f"- For image gen tests, mock `client.models.generate_images`\n"
        )

    return base
