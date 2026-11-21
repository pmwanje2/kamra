"""Password reset belongs to the site's central sign-in (maximus D23).

Kamra shares its Frappe site with ERPNext Desk users. Frappe's reset email
links every user to /update-password?key=..., so a site-wide redirect from
that path into /kamra would hand Desk users' reset keys to one app's screen.
SAP rule: credential reset is owned by the central identity provider, never
by one application on a shared site.

Siteless: the rules are read from hooks.py and matched the way Frappe's
path_resolver.resolve_redirect matches them.
"""

from __future__ import annotations

import re
import unittest

from kamra import hooks


def _redirected(path: str) -> list[dict]:
	"""The website_redirects rules Frappe would apply to `path`."""
	hits = []
	for rule in hooks.website_redirects:
		pattern = rule["source"].strip("/ ") + "$"
		if re.match(pattern, path.strip("/")):
			hits.append(rule)
	return hits


class CentralPasswordResetTest(unittest.TestCase):
	def test_no_site_wide_update_password_redirect(self):
		self.assertEqual(_redirected("/update-password"), [])
		self.assertEqual([r for r in hooks.website_redirects
		                  if re.search(r"update-password", r["source"])], [])

	def test_kamra_guest_redirects_are_kept(self):
		self.assertEqual(_redirected("/book")[0]["target"], "/kamra/book")
		self.assertEqual(_redirected("/stay/abc")[0]["target"], r"/kamra/stay/\1")
		self.assertEqual(_redirected("/hk")[0]["target"], "/kamra/hk")
		self.assertEqual(_redirected("/checkin/tok")[0]["target"], r"/kamra/checkin/\1")

	def test_kamra_own_update_password_page_is_still_served(self):
		# /kamra/update-password is a SPA route behind the /kamra catch-all
		self.assertIn({"from_route": "/kamra/<path:app_path>", "to_route": "kamra"},
		              hooks.website_route_rules)
		self.assertEqual(_redirected("/kamra/update-password"), [])


if __name__ == "__main__":
	unittest.main()
