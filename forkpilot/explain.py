"""LLM explain stage: reads evidence.md, writes explanation.md.

The oracle decides PASS/DRIFT/FAIL; the model only explains the evidence. It sees evidence.md and
nothing else. Backends: Anthropic (ANTHROPIC_API_KEY) or any OpenAI-compatible endpoint
(FP_LLM_BASE_URL + FP_LLM_MODEL: vLLM, llama.cpp server, Ollama), so nothing has to leave the site.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .i18n import lang as ui_lang, t

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
LANGS = {"tr": "Turkish", "en": "English"}
CONFIDENCE = ["low", "medium", "high"]

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "mechanism": {"type": "string"},
        "culprit_lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"file": {"type": "string"}, "quote": {"type": "string"},
                               "why": {"type": "string"}},
                "required": ["file", "quote", "why"],
                "additionalProperties": False,
            },
        },
        "affected_situations": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": CONFIDENCE},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "mechanism", "culprit_lines", "affected_situations", "confidence",
                 "open_questions"],
    "additionalProperties": False,
}

SYSTEM = """You explain the evidence pack of a flight-software regression investigation.

Rules:
- A deterministic oracle already decided the verdicts (PASS/DRIFT/FAIL) and a bisect already found the \
commit. You never change a verdict and never decide whether a regression exists. You only explain: \
which commit, which code change, the likely mechanism, which flight situations are affected, and how \
sure you are.
- Use ONLY the evidence document. You have no repository access and no tools. If the evidence does not \
support a claim, put it in open_questions instead of asserting it.
- culprit_lines: each quote must be one line copied verbatim from a diff in the evidence (character for \
character, without inventing or fixing anything). file is the path from the diff header.
- confidence: low, medium or high, reflecting how well the evidence supports the mechanism.
- Write all free text in {language}. Keep identifiers, paths and quotes untouched.
- Answer with one JSON object matching this schema and nothing else:
{schema}"""


class ExplainError(Exception):
    pass


def system_prompt(lang: str | None = None) -> str:
    lang = ui_lang(lang)
    return SYSTEM.format(language=LANGS.get(lang, lang), schema=json.dumps(SCHEMA))


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, model: str | None = None):
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ExplainError(t("err.explain.nokey"))

    def complete(self, system: str, evidence: str, schema: dict = SCHEMA) -> str:
        try:
            import anthropic
        except ImportError:
            raise ExplainError(t("err.explain.nopkg"))
        client = anthropic.Anthropic()
        with client.messages.stream(
            model=self.model, max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": evidence}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}},
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise ExplainError(t("err.explain.refusal"))
        if msg.stop_reason == "max_tokens":
            raise ExplainError(t("err.explain.maxtok"))
        return next(b.text for b in msg.content if b.type == "text")


class LocalBackend:
    """OpenAI-compatible /chat/completions endpoint (vLLM, llama.cpp server, Ollama)."""
    name = "local"

    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: float = 600):
        self.base_url = (base_url or os.environ.get("FP_LLM_BASE_URL") or "").rstrip("/")
        self.model = model or os.environ.get("FP_LLM_MODEL")
        self.timeout = timeout
        if not self.base_url or not self.model:
            raise ExplainError(t("err.explain.localcfg"))

    def request(self, system: str, evidence: str, schema: dict = SCHEMA) -> urllib.request.Request:
        body = {
            "model": self.model, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": evidence}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "schema": schema, "strict": True}},
        }
        headers = {"Content-Type": "application/json"}
        if os.environ.get("FP_LLM_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["FP_LLM_API_KEY"]
        return urllib.request.Request(self.base_url + "/chat/completions", json.dumps(body).encode(),
                                      headers, method="POST")

    def complete(self, system: str, evidence: str, schema: dict = SCHEMA) -> str:
        try:
            with urllib.request.urlopen(self.request(system, evidence, schema), timeout=self.timeout) as r:
                data = json.load(r)
            return data["choices"][0]["message"]["content"]
        except (urllib.error.URLError, OSError) as e:
            raise ExplainError(t("err.explain.unreach", url=self.base_url, e=e))
        except (KeyError, IndexError, ValueError) as e:
            raise ExplainError(t("err.explain.badresp", e=repr(e)))


def make_backend(kind: str, model: str | None = None):
    if kind == "anthropic":
        return AnthropicBackend(model)
    if kind == "local":
        return LocalBackend(model=model)
    raise ExplainError(t("err.explain.backend", kind=kind))


def parse(text: str, schema: dict = SCHEMA) -> dict:
    """Parse the model's JSON (tolerating a code fence from local models) and check the shape."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        d = json.loads(body)
    except ValueError as e:
        raise ExplainError(t("err.explain.json", e=e))
    missing = [k for k in schema["required"] if not isinstance(d, dict) or k not in d]
    if missing:
        raise ExplainError(t("err.explain.missing", missing=missing))
    if "confidence" in d and d["confidence"] not in CONFIDENCE:
        d["confidence"] = "low"
    return d


def verify_quotes(data: dict, evidence: str) -> dict:
    """Deterministic anti-hallucination check: a quote counts only if it appears literally in the evidence."""
    for c in data["culprit_lines"]:
        q = c.get("quote", "").strip()
        c["verified"] = bool(q) and q in evidence
    return data


def render(data: dict, lang: str | None = None) -> str:
    def L(key):
        return t("explain." + key, lang)
    out = [L("title"), "", f"_{L('note')}_", "", f"## {L('summary')}", "", data["summary"], "",
           f"## {L('mech')}", "", data["mechanism"], "", f"## {L('lines')}", ""]
    for c in data["culprit_lines"]:
        tag = "" if c.get("verified") else f" **[{L('unv')}]**"
        out.append(f"- `{c['file']}`{tag}")
        out += ["  ```", "  " + c["quote"], "  ```"]
        if c.get("why"):
            out.append(f"  {c['why']}")
    if not data["culprit_lines"]:
        out.append(L("none"))
    out += ["", f"## {L('aff')}", ""]
    out += [f"- {s}" for s in data["affected_situations"]] or [L("none")]
    out += ["", f"## {L('conf')}", "", data["confidence"], "", f"## {L('oq')}", ""]
    out += [f"- {s}" for s in data["open_questions"]] or [L("none")]
    return "\n".join(out) + "\n"


def explain(inv_dir: Path, backend, lang: str | None = None) -> Path:
    """Explain `<inv_dir>/evidence.md`; writes explanation.md and explanation.json next to it."""
    inv_dir = Path(inv_dir)
    ev = inv_dir / "evidence.md"
    if not ev.exists():
        raise ExplainError(t("err.explain.noevidence", path=ev))
    lang = ui_lang(lang)
    evidence = ev.read_text()
    data = verify_quotes(parse(backend.complete(system_prompt(lang), evidence)), evidence)
    (inv_dir / "explanation.json").write_text(json.dumps(
        {"backend": backend.name, "model": backend.model, "lang": lang, **data}, indent=1, ensure_ascii=False))
    path = inv_dir / "explanation.md"
    path.write_text(render(data, lang))
    return path
