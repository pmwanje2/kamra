"""Check the published text of every page, including page titles, image labels and metadata."""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

DIST = Path(__file__).resolve().parents[1] / ".vitepress" / "dist"
BRAND = re.compile(r"\bKamra\b|kamrapms\.com|كامرا", re.I)


class PublishedText(HTMLParser):
	def __init__(self):
		super().__init__()
		self.hidden = False
		self.code = False
		self.parts = []

	def handle_starttag(self, tag, attrs):
		if tag in ("script", "style"):
			self.hidden = True
		if tag == "code":
			self.code = True
		for name, value in attrs:
			if value and (name in ("alt", "aria-label", "title") or (tag == "meta" and name == "content")):
				self.parts.append(value)

	def handle_endtag(self, tag):
		if tag in ("script", "style"):
			self.hidden = False
		if tag == "code":
			self.code = False

	def handle_data(self, data):
		# Source-level branding checks cover prose inside code examples. The published
		# page also contains syntax-highlighted wire identifiers split between spans.
		if not self.hidden and not self.code:
			self.parts.append(data)


class PublishedDocsTest(unittest.TestCase):
	def test_all_published_pages(self):
		for name in ("index", "fr/index", "es/index", "ar/index", "how-it-works", "api-reference"):
			with self.subTest(required=name):
				self.assertTrue((DIST / (name + ".html")).is_file())
		for path in sorted(DIST.rglob("*.html")):
			with self.subTest(page=str(path.relative_to(DIST))):
				parser = PublishedText()
				parser.feed(path.read_text())
				self.assertEqual([text.strip()[:100] for text in parser.parts if BRAND.search(text)][:5], [])


if __name__ == "__main__":
	unittest.main()

