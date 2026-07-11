"""
Study-guide generation from a lecture transcript.

Two paths:
  1. If an Anthropic API key is configured -> call the API (Claude Haiku)
     and return a markdown study guide. Costs a few cents per lecture.
  2. No key -> the app exposes the full prompt so the user can paste it
     into Claude.ai themselves (free path).
"""

import json
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

PROMPT_TEMPLATE = """You are preparing exam-focused study notes for a university lecture.

From the lecture transcript below, produce a well-structured markdown study guide with these sections:

## Session summary
Five bullets capturing what this session covered.

## Key concepts
Each concept the lecturer taught, with a 2-4 sentence plain-language explanation. Preserve the lecturer's own examples and analogies where they help.

## Formulas, definitions & worked examples
Every formula, formal definition, or worked example mentioned - reproduced fully and verified for correctness. If the lecturer worked an exercise, show the complete solution step by step.

## Likely exam material
Questions the lecturer asked the class, points they repeated or emphasized, and anything they hinted would appear in assessments.

## Announcements & action items
Assignments, deadlines, readings, or administrative announcements mentioned.

Rules:
- The transcript is auto-generated: if a technical term is garbled, infer the correct term from context and use it (note the correction in brackets the first time).
- Ignore greetings, small talk, and connection issues.
- If a section has nothing, write "None mentioned." rather than inventing content.
- Write for a student revising for an exam: precise, complete, no filler.

TRANSCRIPT:
{transcript}
"""


def build_prompt(transcript: str) -> str:
    return PROMPT_TEMPLATE.format(transcript=transcript.strip())


def generate(api_key: str, transcript: str, model: str = DEFAULT_MODEL) -> str:
    """Call the Anthropic API and return the study guide markdown.

    Raises RuntimeError with a human-readable message on any failure.
    """
    body = json.dumps({
        "model": model,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": build_prompt(transcript)}],
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("error", {}).get("message", str(exc))
        except Exception:
            message = str(exc)
        if exc.code == 401:
            message = "The API key was rejected. Check it in Settings. (" + message + ")"
        raise RuntimeError(f"Anthropic API error {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the Anthropic API: {exc.reason}") from exc

    parts = [blk.get("text", "") for blk in data.get("content", []) if blk.get("type") == "text"]
    guide = "\n".join(parts).strip()
    if not guide:
        raise RuntimeError("The API returned an empty response.")
    return guide
