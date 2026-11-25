"""Catalog and fallback rules. No site: these do not import Frappe."""

from __future__ import annotations

import csv
import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from kamra.ui_languages import (
	BASE,
	CATALOG_PATH,
	UnknownLanguage,
	clean_requested_disabled,
	enabled_list,
	language_label,
	load_catalog,
	parse_stored_disabled,
	resolve_language,
)

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_BRAND = re.compile(
	r"(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY|HUF|Tridz|BuildSuite)(?![a-z0-9])|kamrapms\.com|HeyKoala",
	re.I,
)


class UiLanguageLogicTest(unittest.TestCase):
	def test_shipped_catalog_starts_with_english_and_marks_arabic_rtl(self):
		rows = load_catalog()
		codes = [row["code"] for row in rows]
		self.assertEqual(codes[0], BASE)
		self.assertIn("es", codes)
		self.assertIn("pt", codes)
		self.assertIn("ar", codes)
		self.assertIn("de", codes)
		self.assertIn("id", codes)
		self.assertIn("fil", codes)
		self.assertIn("zh-CN", codes)
		self.assertIn("fr", codes)
		self.assertTrue(CATALOG_PATH.is_file())
		arabic = next(row for row in rows if row["code"] == "ar")
		self.assertEqual(arabic["dir"], "rtl")
		french = next(row for row in rows if row["code"] == "fr")
		self.assertEqual(french["dir"], "ltr")
		self.assertEqual(french["nativeLabel"], "Français")
		self.assertEqual(french["englishLabel"], "French")
		self.assertEqual(language_label("es", rows), "Spanish")
		self.assertEqual(language_label("fr", rows), "French")
		self.assertEqual(language_label("nope", rows), "nope")

	def test_backend_french_translations_are_complete_and_white_label_clean(self):
		fr_csv = ROOT / "kamra" / "translations" / "fr.csv"
		self.assertTrue(fr_csv.is_file(), "kamra/translations/fr.csv must exist")
		proc = subprocess.run(
			[sys.executable, str(ROOT / "scripts" / "i18n" / "check_backend_translations.py"), "--lang", "fr"],
			cwd=str(ROOT),
			capture_output=True,
			text=True,
		)
		self.assertEqual(proc.returncode, 0, f"check_backend_translations.py --lang fr failed:\n{proc.stdout}\n{proc.stderr}")

		leaks = []
		with fr_csv.open(encoding="utf-8", newline="") as handle:
			for lineno, row in enumerate(csv.reader(handle), 1):
				if len(row) >= 2 and FORBIDDEN_BRAND.search(row[1]):
					leaks.append(f"fr.csv:{lineno}: {row[1]}")
		self.assertEqual(leaks, [], f"Vendor leaks in fr.csv: {leaks[:10]}")

	def test_generic_pack_locale_never_hardcodes_en_us(self):
		fake_frappe = ModuleType("frappe")
		sys_lang = {"value": None}
		fake_frappe.db = SimpleNamespace(
			get_value=lambda *args, **kwargs: "$",
			get_single_value=lambda doctype, field: sys_lang["value"] if (doctype, field) == ("System Settings", "language") else None,
		)
		fake_frappe.get_cached_doc = lambda *args, **kwargs: {"gst_rate_low": 0, "gst_rate_high": 0}
		with patch.dict(sys.modules, {"frappe": fake_frappe}):
			spec = importlib.util.spec_from_file_location(
				"generic_pack_under_test", ROOT / "kamra" / "localization" / "generic.py"
			)
			mod = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(mod)

			class PropDoc(dict):
				@property
				def name(self):
					return self.get("name", "PROP-1")

			# 1. Explicit property locale wins
			out_prop = mod.locale(PropDoc(name="P1", currency="EUR", locale="fr-FR"))
			self.assertEqual(out_prop["locale"], "fr-FR")

			# 2. System Settings language used when property locale unset
			sys_lang["value"] = "fr"
			out_sys = mod.locale(PropDoc(name="P1", currency="EUR"))
			self.assertEqual(out_sys["locale"], "fr")

			# 3. Neutral international fallback en-GB when both unset (never en-US)
			sys_lang["value"] = None
			out_default = mod.locale(PropDoc(name="P1", currency="USD"))
			self.assertEqual(out_default["locale"], "en-GB")
			self.assertNotEqual(out_default["locale"], "en-US")

	def test_missing_storage_enables_every_language_including_a_new_one(self):
		known = ["en", "es", "pt", "ar", "id"]
		self.assertEqual(parse_stored_disabled(None, known), [])
		self.assertEqual(parse_stored_disabled("", known), [])
		self.assertEqual(parse_stored_disabled("not-json", known), [])
		self.assertEqual(enabled_list(known, []), known)

	def test_stored_list_drops_english_unknown_and_removed_codes(self):
		known = ["en", "es", "pt", "ar"]
		self.assertEqual(
			parse_stored_disabled(["en", "es", "zz", "", "es"], known),
			["es"],
		)
		self.assertEqual(parse_stored_disabled('["pt", "fil"]', known), ["pt"])

	def test_a_new_language_stays_on_when_it_is_not_in_the_disabled_list(self):
		# id is registered after es was switched off: it is on, es stays off
		self.assertEqual(enabled_list(["en", "es", "pt", "ar", "id"], ["es"]),
		                 ["en", "pt", "ar", "id"])

	def test_save_refuses_an_unknown_code_and_cannot_disable_english(self):
		known = ["en", "es", "pt", "ar"]
		self.assertEqual(clean_requested_disabled(["en", "ar", "ar"], known), ["ar"])
		self.assertEqual(clean_requested_disabled("[]", known), [])
		with self.assertRaises(UnknownLanguage) as caught:
			clean_requested_disabled(["zz"], known)
		self.assertEqual(caught.exception.code, "zz")
		with self.assertRaises(UnknownLanguage):
			clean_requested_disabled({"es": False}, known)

	def test_resolve_falls_back_to_the_property_default_then_english(self):
		enabled = ["en", "pt"]
		self.assertEqual(resolve_language("es", enabled, "pt"), "pt")
		self.assertEqual(resolve_language("es", enabled, "ar"), "en")
		self.assertEqual(resolve_language("pt", enabled, "en"), "pt")
		self.assertEqual(resolve_language(None, enabled), "en")
		self.assertEqual(resolve_language("ES", ["en", "es"]), "es")


if __name__ == "__main__":
	unittest.main()
