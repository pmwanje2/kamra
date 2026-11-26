"""Check published destinations, code examples and installer instructions against the release."""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs-site"
UPSTREAM = re.compile(r"\b(?:kamrapms\.com|heykoala\.ai)\b", re.I)


class PublishedPage(HTMLParser):
	def __init__(self):
		super().__init__()
		self.text = []
		self.hrefs = []
		self.hidden = False

	def handle_starttag(self, tag, attrs):
		if tag in ("script", "style"):
			self.hidden = True
		if tag == "a":
			self.hrefs.append(dict(attrs).get("href") or "")

	def handle_endtag(self, tag):
		if tag in ("script", "style"):
			self.hidden = False

	def handle_data(self, data):
		if not self.hidden:
			self.text.append(data)


class PublishedReleaseTest(unittest.TestCase):
	def test_all_rendered_text_code_and_links_use_public_hosts(self):
		dist = DOCS / ".vitepress/dist"
		self.assertTrue((dist / "developers.html").is_file())
		for page in sorted(dist.rglob("*.html")):
			parser = PublishedPage()
			parser.feed(page.read_text())
			for area, content in (("rendered content", "".join(parser.text)), ("links", "\n".join(parser.hrefs))):
				with self.subTest(page=page.relative_to(dist), area=area):
					self.assertEqual(UPSTREAM.findall(content), [])

	def test_guide_and_site_creator_agree_on_readiness(self):
		guide = (DOCS / "quickstart.md").read_text()
		stack = (ROOT / "deploy/compose/quickstart.yaml").read_text()
		for source in (guide, stack):
			with self.subTest(source="compose" if source == stack else "guide"):
				self.assertIn("Maximus is ready", source)

	def test_installer_guide_and_default_checkout_use_the_maximus_release(self):
		guide = (DOCS / "quickstart.md").read_text()
		installer = (ROOT / "deploy/install.sh").read_text()
		for name, source in (("guide", guide), ("installer", installer)):
			with self.subTest(source=name):
				self.assertNotIn("Kamra-PMS/kamra-pms", source)
				self.assertIn("pmwanje2/kamra", source)
				self.assertIn("maximus/v2.8.0", source)


if __name__ == "__main__":
	unittest.main()
