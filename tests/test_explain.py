"""Offline tests for the explain stage. unittest-style, so they run with pytest or `python -m unittest`."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forkpilot import explain as ex

EVIDENCE = """# Kanıt paketi

- gps_loss: DRIFT

```diff
diff --git a/ArduCopter/mode.cpp b/ArduCopter/mode.cpp
-int32_t Mode::get_alt_above_ground_cm(void)
+float Mode::get_alt_above_ground_m(void)
```
"""

GOOD = {"summary": "özet", "mechanism": "birim değişti",
        "culprit_lines": [{"file": "ArduCopter/mode.cpp",
                           "quote": "+float Mode::get_alt_above_ground_m(void)", "why": "m döner"}],
        "affected_situations": ["GPS kaybı"], "confidence": "medium", "open_questions": ["x?"]}


class FakeBackend:
    name, model = "fake", "fake-1"

    def __init__(self, reply):
        self.reply, self.seen = reply, None

    def complete(self, system, evidence):
        self.seen = (system, evidence)
        return json.dumps(self.reply)


def inv_dir():
    d = Path(tempfile.mkdtemp())
    (d / "evidence.md").write_text(EVIDENCE)
    return d


class ExplainTest(unittest.TestCase):
    def test_render_and_files(self):
        d, be = inv_dir(), FakeBackend(GOOD)
        path = ex.explain(d, be, "tr")
        md = path.read_text()
        self.assertEqual(path, d / "explanation.md")
        self.assertIn("## Mekanizma", md)
        self.assertIn("get_alt_above_ground_m", md)
        self.assertNotIn("doğrulanamadı", md)
        self.assertEqual(be.seen[1], EVIDENCE)
        self.assertIn("Turkish", be.seen[0])
        self.assertIn("never change a verdict", be.seen[0])
        self.assertTrue(json.loads((d / "explanation.json").read_text())["culprit_lines"][0]["verified"])

    def test_english(self):
        d, be = inv_dir(), FakeBackend(GOOD)
        self.assertIn("## Mechanism", ex.explain(d, be, "en").read_text())
        self.assertIn("English", be.seen[0])

    def test_fake_quote_flagged(self):
        bad = json.loads(json.dumps(GOOD))
        bad["culprit_lines"].append({"file": "a.cpp", "quote": "x = never_in_evidence();", "why": "?"})
        md = ex.explain(inv_dir(), FakeBackend(bad), "tr").read_text()
        self.assertEqual(md.count("doğrulanamadı"), 1)
        self.assertIn("`a.cpp` **[doğrulanamadı]**", md)
        self.assertIn("`ArduCopter/mode.cpp`\n", md)

    def test_unverified_english_and_empty_quote(self):
        bad = dict(GOOD, culprit_lines=[{"file": "a", "quote": "  ", "why": ""}])
        self.assertIn("[unverified]", ex.explain(inv_dir(), FakeBackend(bad), "en").read_text())

    def test_evidence_and_prompt_follow_language(self):
        for lang, heading, word in (("en", "## Culprit lines", "English"), ("tr", "## Şüpheli satırlar", "Turkish")):
            d, be = inv_dir(), FakeBackend(GOOD)
            self.assertIn(heading, ex.explain(d, be, lang).read_text())
            self.assertIn(word, be.seen[0])
            self.assertEqual(json.loads((d / "explanation.json").read_text())["lang"], lang)

    def test_bad_json_and_missing_fields(self):
        with self.assertRaises(ex.ExplainError):
            ex.parse("not json")
        with self.assertRaises(ex.ExplainError):
            ex.parse('{"summary": "x"}')
        self.assertEqual(ex.parse("```json\n" + json.dumps(GOOD) + "\n```")["confidence"], "medium")

    def test_missing_evidence(self):
        with self.assertRaises(ex.ExplainError):
            ex.explain(Path(tempfile.mkdtemp()), FakeBackend(GOOD))


class BackendTest(unittest.TestCase):
    def test_anthropic_missing_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ex.ExplainError) as c:
                ex.make_backend("anthropic")
        self.assertIn("ANTHROPIC_API_KEY", str(c.exception))

    def test_anthropic_default_model(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=True):
            self.assertEqual(ex.make_backend("anthropic").model, "claude-opus-5-5")
            self.assertEqual(ex.make_backend("anthropic", "other").model, "other")

    def test_local_requires_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ex.ExplainError):
                ex.make_backend("local")

    def test_local_request_shape(self):
        env = {"FP_LLM_BASE_URL": "http://localhost:8000/v1/", "FP_LLM_MODEL": "qwen"}
        reply = json.dumps({"choices": [{"message": {"content": json.dumps(GOOD)}}]}).encode()
        with mock.patch.dict(os.environ, env, clear=True):
            be = ex.make_backend("local")
            with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(reply)) as op:
                out = be.complete("SYS", "EVID")
        req = op.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "http://localhost:8000/v1/chat/completions")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(body["model"], "qwen")
        self.assertEqual(body["messages"], [{"role": "system", "content": "SYS"},
                                            {"role": "user", "content": "EVID"}])
        self.assertEqual(body["response_format"]["json_schema"]["schema"], ex.SCHEMA)
        self.assertNotIn("Authorization", req.headers)
        self.assertEqual(json.loads(out)["summary"], "özet")

    def test_local_unreachable(self):
        import urllib.error
        env = {"FP_LLM_BASE_URL": "http://x/v1", "FP_LLM_MODEL": "m"}
        with mock.patch.dict(os.environ, env, clear=True):
            be = ex.make_backend("local")
            with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
                with self.assertRaises(ex.ExplainError):
                    be.complete("s", "e")


if __name__ == "__main__":
    unittest.main()
