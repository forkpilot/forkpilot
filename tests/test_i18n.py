import json
import os
import re
import string
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from forkpilot import explain, fix, i18n, investigate, oracle, timeline


def fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


class CatalogueTest(unittest.TestCase):
    def tearDown(self):
        i18n._lang = None
        os.environ.pop("FP_LANG", None)

    def test_every_key_in_every_language(self):
        base = set(i18n.MSG[i18n.DEFAULT])
        for code in i18n.LANGS:
            self.assertEqual(set(i18n.MSG[code]) ^ base, set(), code)

    def test_placeholders_match_between_languages(self):
        for key, text in i18n.MSG["en"].items():
            for code in i18n.LANGS:
                self.assertEqual(fields(i18n.MSG[code][key]), fields(text), f"{code}:{key}")

    def test_no_empty_text(self):
        for code in i18n.LANGS:
            for key, text in i18n.MSG[code].items():
                self.assertTrue(text.strip(), f"{code}:{key}")

    def test_default_is_english_and_switch(self):
        os.environ.pop("FP_LANG", None)
        self.assertEqual(i18n.lang(), "en")
        os.environ["FP_LANG"] = "tr"
        self.assertEqual(i18n.lang(), "tr")
        os.environ["FP_LANG"] = "xx"          # unknown: fall back, never crash a run
        self.assertEqual(i18n.lang(), "en")
        i18n.set_lang("tr")
        self.assertEqual(i18n.lang(), "tr")
        self.assertEqual(os.environ["FP_LANG"], "tr")      # child processes follow
        self.assertEqual(i18n.t("report.k.date", "en"), "Date")
        self.assertEqual(i18n.t("report.k.date"), "Tarih")
        with self.assertRaises(ValueError):
            i18n.set_lang("de")

    def test_missing_key_falls_back_to_english(self):
        i18n.MSG["tr"].pop("report.k.date")
        self.addCleanup(i18n.MSG["tr"].__setitem__, "report.k.date", "Tarih")
        self.assertEqual(i18n.t("report.k.date", "tr"), "Date")

    def test_english_catalogue_has_no_turkish_letters(self):
        for key, text in i18n.MSG["en"].items():
            self.assertIsNone(re.search("[ğüşıöçĞÜŞİÖÇ]", text), key)


class LanguageFollowsSettingTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FP_LANG", None)
        i18n._lang = None
        self.addCleanup(os.environ.pop, "FP_LANG", None)
        self.addCleanup(setattr, i18n, "_lang", None)

    def test_oracle_messages(self):
        f = oracle.Finding("m", "drift", 1.0, "[2.0, 3.0]")
        rule = oracle.check_rules({"completed": 1.0}, {"x": {"lt": 1}})[0]
        self.assertIn("(expected [2.0, 3.0])", str(f))
        self.assertIn("metric must be produced", rule.expected)
        i18n.set_lang("tr")
        self.assertIn("(beklenen [2.0, 3.0])", str(f))
        self.assertIn("metrik üretilmeli", oracle.check_rules({"completed": 1.0}, {"x": {"lt": 1}})[0].expected)

    def test_timeline_text(self):
        analysis = {"first_divergence_t": 12.0, "candidate_error": "boom",
                    "events": [{"t": 12.0, "status": "new", "text": i18n.t("tl.mode", mode="LOITER"),
                                "key": "mode:LOITER"}],
                    "signals": [{"signal": "horizontal", "t": 12.0, "baseline": "(N 1, E 2)",
                                 "candidate": "(N 1, E 3)", "note": ""}]}
        en = timeline.render("hover", analysis)
        self.assertIn("first divergence t=12.0 s", en)
        self.assertIn("horizontal position diverged", en)
        self.assertIn("candidate run error: boom", en)
        i18n.set_lang("tr")
        tr = timeline.render("hover", analysis)
        self.assertIn("ilk ayrışma t=12.0 s", tr)
        self.assertIn("yatay konum", tr)
        self.assertIn("aday koşu hatası: boom", tr)

    def test_evidence_pack(self):
        result = {"culprit": "c" * 40, "subject": "S", "range": 4, "tests": 2, "ambiguous_with": ["d" * 40]}
        report = {"hover": ("DRIFT", ["DRIFT m: 1.000 (expected [2.000, 3.000])"])}
        tri = {"hover": {"class": "consistent", "non_pass": 3, "runs": 3}}

        def pack():
            with tempfile.TemporaryDirectory() as d, mock.patch.object(investigate, "git", return_value="diff line"):
                path = investigate.write_evidence(SimpleNamespace(dir=Path(d)), Path(d), report, tri,
                                                  {"hover": "TIMELINE"}, result)
                return path.read_text()
        en = pack()
        for text in ("# Evidence pack", "## Triage", "## First divergence", "(3/3 runs not PASS)",
                     "Found in 2 tests among 4 commits", "Unbuildable commits"):
            self.assertIn(text, en)
        i18n.set_lang("tr")
        tr = pack()
        for text in ("# Kanıt paketi", "## İlk ayrışma", "(3/3 koşu PASS değil)",
                     "4 commit içinde 2 testte bulundu", "Derlenemeyen"):
            self.assertIn(text, tr)

    def test_explain_follows_setting_and_prompts_match(self):
        data = {"summary": "s", "mechanism": "m", "culprit_lines": [{"file": "a", "quote": "q", "why": ""}],
                "affected_situations": [], "confidence": "low", "open_questions": []}
        self.assertIn("## Mechanism", explain.render(data))
        self.assertIn("**[unverified]**", explain.render(data))
        self.assertIn("English", explain.system_prompt())
        i18n.set_lang("tr")
        self.assertIn("## Mekanizma", explain.render(data))
        self.assertIn("**[doğrulanamadı]**", explain.render(data))
        self.assertIn("Turkish", explain.system_prompt())
        self.assertIn("English", explain.system_prompt("en"))      # an explicit argument wins

    def test_quote_check_is_language_independent(self):
        data = {"culprit_lines": [{"quote": "+int x = 1;"}, {"quote": "+nothing"}]}
        for code in i18n.LANGS:
            i18n.set_lang(code)
            out = explain.verify_quotes(json.loads(json.dumps(data)), "diff\n+int x = 1;\n")
            self.assertEqual([c["verified"] for c in out["culprit_lines"]], [True, False])

    def test_fix_render_and_errors(self):
        data = {"candidates": [{"name": "revert", "source": "git revert", "outcome": "fixes", "diff_lines": 9,
                                "full_battery": False, "error": "e"}]}
        en = fix.render(data)
        self.assertIn("| revert | git revert | fixes | 9 |", en)
        self.assertIn("Simulation does not replace flight testing", en)
        i18n.set_lang("tr")
        tr = fix.render(data)
        self.assertIn("| revert | git revert | düzeltiyor | 9 |", tr)
        self.assertIn("uçuş testinin yerini tutmaz", tr)
        with self.assertRaises(fix.Unapplicable) as cm:
            fix.apply_edits(Path("."), [], [])
        self.assertEqual(str(cm.exception), "düzenleme yok")
        i18n.set_lang("en")
        with self.assertRaises(fix.Unapplicable) as cm:
            fix.apply_edits(Path("."), [], [])
        self.assertEqual(str(cm.exception), "no edits")

    def test_backend_errors(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaises(explain.ExplainError) as cm:
                explain.AnthropicBackend()
            self.assertIn("is not set", str(cm.exception))
            i18n.set_lang("tr")
            with self.assertRaises(explain.ExplainError) as cm:
                explain.AnthropicBackend()
            self.assertIn("tanımlı değil", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
