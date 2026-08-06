"""Anthropic API wrapper. All prompts live in prompts/ as editable text files."""
import os
import re

from dotenv import load_dotenv

load_dotenv()

PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts"
)

_client = None


class ClaudeError(Exception):
    pass


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ClaudeError(
                "No Anthropic API key found. Copy .env.example to .env and set "
                "ANTHROPIC_API_KEY, then restart the server (or set it on the Settings page)."
            )
        import anthropic
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def set_api_key(key: str) -> None:
    """Update the key at runtime (from the Settings page)."""
    global _client
    os.environ["ANTHROPIC_API_KEY"] = key.strip()
    _client = None


def render_prompt(name: str, **variables) -> str:
    """Load prompts/<name>.txt and substitute {{variable}} placeholders."""
    path = os.path.join(PROMPTS_DIR, f"{name}.txt")
    if not os.path.exists(path):
        raise ClaudeError(f"Prompt file not found: prompts/{name}.txt")
    with open(path, encoding="utf-8") as f:
        text = f.read()

    def sub(match):
        key = match.group(1).strip()
        return str(variables.get(key, match.group(0)))

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", sub, text)


def ask_claude(prompt: str, model: str = "claude-sonnet-4-6",
               system: str = "", max_tokens: int = 4000) -> str:
    """Single-turn text generation call."""
    client = _get_client()
    import anthropic
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    try:
        response = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        raise ClaudeError("Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.RateLimitError:
        raise ClaudeError("Anthropic API rate limit hit — wait a moment and try again.")
    except anthropic.APIConnectionError:
        raise ClaudeError("Could not reach the Anthropic API. Check your internet connection.")
    except anthropic.APIStatusError as e:
        raise ClaudeError(f"Anthropic API error ({e.status_code}): {e.message}")

    if response.stop_reason == "refusal":
        raise ClaudeError("Claude declined to generate this content.")
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text.strip():
        raise ClaudeError("Claude returned an empty response — try again.")
    return text.strip()
