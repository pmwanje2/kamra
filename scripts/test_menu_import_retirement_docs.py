"""Check the published guides, API descriptions and menu translation sources."""
import csv
import json
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs-site"


class MenuImportDocumentationTest(TestCase):
	def test_guides_direct_menu_management_to_the_pos(self):
		for guide, obsolete in (
			("fnb.md", "Bulk upload"),
			("ar/fnb.md", "رفع جماعي"),
			("es/fnb.md", "Importación en lote"),
		):
			with self.subTest(guide=guide):
				text = (DOCS / guide).read_text()
				self.assertNotIn(obsolete, text)
				self.assertIn("ERPNext", text)

	def test_api_reference_and_collection_describe_each_retirement(self):
		reference = (DOCS / "api-reference.md").read_text()
		collection = json.loads((DOCS / "public/kamra.postman_collection.json").read_text())
		requests = {
			item["request"]["url"]["path"][-1]: item["request"]["description"]
			for group in collection["item"] for item in group["item"]
		}
		for name in ("menu_template", "preview_menu_import", "run_menu_import"):
			method = f"kamra.menu_import.{name}"
			with self.subTest(method=method):
				description = requests[method]
				self.assertIn("retired", description.lower())
				self.assertIn("ValidationError", description)
				section = reference.split(f"### `{method}`\n", 1)[1].split("\n##", 1)[0]
				self.assertIn(description, section)

	def test_catalog_no_longer_attributes_translations_to_the_import_dialog(self):
		with (ROOT / "frontend/src/i18n/catalog.csv").open(encoding="utf-8-sig", newline="") as f:
			stale = [row["key"] for row in csv.DictReader(f) if "screens/MenuImport.tsx" in row["sources"].split("; ")]
		self.assertEqual(stale, [])
