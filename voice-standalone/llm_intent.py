from google import genai
from google.genai import types

from config import GEMINI_API_KEY, INTENT_MODEL

_client = genai.Client(api_key=GEMINI_API_KEY)

_TOOLS = [
    types.FunctionDeclaration(
        name="open_app",
        description="Launch a desktop application by name, e.g. firefox, code, spotify.",
        parameters={
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
    ),
    types.FunctionDeclaration(
        name="web_search",
        description="Search Google or YouTube in the default browser.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "site": {"type": "string", "enum": ["google", "youtube"]},
            },
            "required": ["query", "site"],
        },
    ),
    types.FunctionDeclaration(
        name="set_volume",
        description="Set system output volume to an exact percentage (0-100).",
        parameters={
            "type": "object",
            "properties": {"percent": {"type": "integer"}},
            "required": ["percent"],
        },
    ),
    types.FunctionDeclaration(
        name="set_brightness",
        description="Set screen brightness to an exact percentage (0-100).",
        parameters={
            "type": "object",
            "properties": {"percent": {"type": "integer"}},
            "required": ["percent"],
        },
    ),
    types.FunctionDeclaration(
        name="run_terminal",
        description="Run an arbitrary shell command on the user's Linux machine.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    ),
]

_TOOL = types.Tool(function_declarations=_TOOLS)


def resolve_intent(text: str) -> tuple[str | None, dict | None]:
    response = _client.models.generate_content(
        model=INTENT_MODEL,
        contents=[
            "You turn a spoken command into exactly one tool call. "
            "Pick the single best matching tool and fill its arguments from the command. "
            "If nothing matches, do not call any tool.",
            text,
        ],
        config=types.GenerateContentConfig(tools=[_TOOL]),
    )
    for part in response.candidates[0].content.parts:
        if part.function_call:
            return part.function_call.name, dict(part.function_call.args)
    return None, None
