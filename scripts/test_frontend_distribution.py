"""The installer builds the SPA from source in its image build stage."""
import subprocess
import unittest
from pathlib import Path


class FrontendDistributionTest(unittest.TestCase):
	def test_release_bundle_is_excluded_from_source_reviews(self):
		root = Path(__file__).resolve().parents[1]
		tracked = subprocess.check_output(["git", "ls-files", "kamra/public/frontend"], cwd=root)
		self.assertFalse(tracked, "Release bundles belong in the image build")
		self.assertNotIn("regenerated `kamra/public/frontend` output",
		                 (root / ".github/PULL_REQUEST_TEMPLATE.md").read_text())
