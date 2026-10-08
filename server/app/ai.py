"""AI fallback: Claude (Anthropic API) or Azure OpenAI. Used only for content that heading matching could not place."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .config import settings


class AIError(Exception):
    pass


@dataclass
class AIUsage:
    provider: str
    model: str
    calls: int = 0
    chars_sent: int = 0


SYSTEM = (
    "You organise documents. You map pieces of a source document onto the sections of a document template, "
    "and you extract field values that are stated in the source. You never invent content. "
    "When something does not clearly belong, you say so with null. Reply with JSON only."
)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise AIError("The AI reply was not valid JSON.")


def _claude(prompt: str) -> str:
    c = settings.claude
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": c.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": c.model, "max_tokens": 8000, "system": SYSTEM, "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    if r.status_code >= 400:
        raise AIError(f"Claude API error {r.status_code}: {r.text[:300]}")
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def _azure(prompt: str) -> str:
    a = settings.azure
    r = httpx.post(
        f"{a.endpoint}/openai/deployments/{a.deployment}/chat/completions",
        params={"api-version": a.api_version},
        headers={"api-key": a.api_key, "content-type": "application/json"},
        json={
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=180,
    )
    if r.status_code >= 400:
        raise AIError(f"Azure OpenAI error {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"] or ""


class AIClient:
    def __init__(self, provider: str):
        self.provider = provider
        if provider == "claude":
            if not settings.claude.configured:
                raise AIError("Claude is not set up on the server (ANTHROPIC_API_KEY).")
            self.usage = AIUsage("Claude", settings.claude.model)
        elif provider == "azure":
            if not settings.azure.configured:
                raise AIError("Azure OpenAI is not set up on the server (AZURE_OPENAI_*).")
            self.usage = AIUsage("Azure OpenAI", settings.azure.deployment)
        elif provider == "demo" and settings.demo:
            self.usage = AIUsage("Demo AI", "keyword matching")
        else:
            raise AIError(f"Unknown AI provider '{provider}'.")

    def ask(self, prompt: str, demo_answer=None) -> dict:
        self.usage.calls += 1
        self.usage.chars_sent += len(prompt)
        if self.provider == "demo":
            return demo_answer()
        text = _claude(prompt) if self.provider == "claude" else _azure(prompt)
        return _extract_json(text)

    # ---------- tasks ----------

    def place_units(self, sections: list[dict], units: list[dict]) -> dict[str, dict]:
        """units: [{id, heading, text}] -> {unit_id: {"section": key|None, "confidence": str, "reason": str}}"""
        prompt = (
            "TEMPLATE SECTIONS (key, path, title, what the template expects):\n"
            + "\n".join(
                f'- {s["key"]}: {" > ".join(s["path"] + [s["title"]])}' + (f' | expects: {s["guidance"][:300]}' if s["guidance"] else "")
                for s in sections
            )
            + "\n\nSOURCE PIECES that could not be matched by heading:\n"
            + "\n".join(f'- {u["id"]}: heading: {u["heading"] or "(none)"} | text: {u["text"][:900]}' for u in units)
            + "\n\nFor each source piece, choose the ONE template section key where its content belongs, or null if it fits none. "
            'Reply as {"assignments": [{"unit": "<id>", "section": "<key or null>", "confidence": "high|medium|low", "reason": "<few words>"}]}'
        )

        def demo():
            return {"assignments": [_demo_place(u, sections) for u in units]}

        data = self.ask(prompt, demo)
        out = {}
        valid = {s["key"] for s in sections}
        for a in data.get("assignments", []):
            uid, key = a.get("unit"), a.get("section")
            if uid:
                out[uid] = {"section": key if key in valid else None, "confidence": a.get("confidence", ""), "reason": a.get("reason", "")}
        return out

    def extract_fields(self, fields: list[dict], text: str) -> dict[str, dict]:
        """fields: [{key, name}] -> {key: {"value": str, "quote": str}} for fields stated in the text."""
        prompt = (
            "FIELDS to fill:\n"
            + "\n".join(f'- {f["key"]}: {f["name"]}' for f in fields)
            + "\n\nSOURCE DOCUMENT TEXT:\n"
            + text[:20000]
            + '\n\nReturn only values explicitly stated in the source. Reply as {"fields": [{"field": "<key>", "value": "<value>", "quote": "<exact source words>"}]}. '
            "Omit fields that are not stated."
        )

        def demo():
            return {"fields": []}

        data = self.ask(prompt, demo)
        valid = {f["key"] for f in fields}
        return {
            f["field"]: {"value": str(f.get("value", "")).strip(), "quote": str(f.get("quote", ""))[:200]}
            for f in data.get("fields", [])
            if f.get("field") in valid and str(f.get("value", "")).strip()
        }


def _demo_place(unit: dict, sections: list[dict]) -> dict:
    """Stand-in for a model in demo mode: word overlap between the piece and each section's title and guidance."""
    words = set(re.findall(r"[a-z]{4,}", (unit["heading"] + " " + unit["text"]).lower()))
    best, score = None, 0.0
    for s in sections:
        sw = set(re.findall(r"[a-z]{4,}", (s["title"] + " " + s["guidance"]).lower()))
        if not sw:
            continue
        sc = len(words & sw) / len(sw)
        if sc > score:
            best, score = s["key"], sc
    return {"unit": unit["id"], "section": best if score >= 0.3 else None, "confidence": "medium" if score >= 0.35 else "low", "reason": "shared terms"}
