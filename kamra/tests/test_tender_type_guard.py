"""Guard test: money paths must classify tenders by payment_tender_type, never literal names.

PR #47 R2 (P1 #2 / P2 #4):
- No literal comparisons against "Cash", "Foreign Cash", "Bank Transfer", or "Card"
  (or CASH_MODES / CARD_MODES tuples) on payment mode variables in backend money paths.
- No SQL literal mode comparisons (`mode = 'Cash'`, `mode = 'Foreign Cash'`, etc.).
- No literal `mode === "Foreign Cash"` / `"Bank Transfer"` / `"Cash"` checks in
  `frontend/src/components/RowCorrection.tsx`.

Comparisons against a resolved *tender_type* variable (e.g. `tender == "Foreign Cash"`,
`dest_tender == "Bank Transfer"`, `row_tender == "Cash"`) are allowed — the guard
specifically blocks comparing a Payment Method *code / mode* against system code names.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MONEY_PATH_MODULES: tuple[str, ...] = (
	"kamra/api.py",
	"kamra/cashier.py",
	"kamra/corrections.py",
	"kamra/night_audit.py",
	"kamra/deposit.py",
	"kamra/pos.py",
	"kamra/checkout.py",
	"kamra/folio.py",
	"kamra/ledger.py",
	"kamra/crs.py",
	"kamra/localization/__init__.py",
)

FORBIDDEN_MODE_LITERALS = frozenset({"Cash", "Foreign Cash", "Bank Transfer", "Card"})
FORBIDDEN_CONSTANTS = frozenset({"CASH_MODES", "CARD_MODES", "FX_PAYMENT_MODES"})

# Variable / attribute names that explicitly hold a resolved Payment Method.tender_type
TENDER_TYPE_NAMES = frozenset({
	"tender",
	"tender_type",
	"dest_tender",
	"row_tender",
	"pay_tender",
	"mode_tender",
	"effective_tender",
	"tt",
})

SQL_LITERAL_MODE_RE = re.compile(
	r"\b(?:\w+\.)?mode\s*(?:=|!=|<>|IN\s*\()\s*'(?:Cash|Foreign Cash|Bank Transfer|Card)'",
	re.IGNORECASE,
)

FRONTEND_LITERAL_MODE_RE = re.compile(
	r"""\bmode\s*(?:===|!==|==|!=)\s*["'](?:Cash|Foreign Cash|Bank Transfer|Card)["']"""
)


def _is_tender_type_expr(node: ast.AST) -> bool:
	"""True when the AST expression represents a resolved tender_type, not a mode code."""
	if isinstance(node, ast.Name):
		return node.id in TENDER_TYPE_NAMES
	if isinstance(node, ast.Attribute):
		return node.attr in TENDER_TYPE_NAMES
	if isinstance(node, ast.Call):
		fn = node.func
		if isinstance(fn, ast.Name) and fn.id == "payment_tender_type":
			return True
		if isinstance(fn, ast.Attribute) and fn.attr in ("payment_tender_type", "get"):
			if fn.attr == "payment_tender_type":
				return True
			if isinstance(fn.value, ast.Name) and fn.value.id in ("tender", "tender_types", "tenders"):
				return True
			if isinstance(fn.value, ast.Attribute) and fn.value.attr in ("tender", "tender_types"):
				return True
	return False


def _is_mode_like_expr(node: ast.AST) -> bool:
	"""True when an expression is a mode/payment-method code (not a tender_type)."""
	if _is_tender_type_expr(node):
		return False
	if isinstance(node, ast.Name):
		low = node.id.lower()
		return low in ("m", "mode", "pay_mode", "payment_mode", "destination", "dest", "destination_mode", "old", "new_mode") or low.endswith("_mode")
	if isinstance(node, ast.Attribute):
		return node.attr in ("mode", "payment_mode")
	if isinstance(node, ast.Subscript):
		if isinstance(node.slice, ast.Constant) and node.slice.value in ("mode", "payment_mode"):
			return True
	return False


def _extract_literal_strings(node: ast.AST) -> set[str]:
	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		return {node.value}
	if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
		out: set[str] = set()
		for elt in node.elts:
			if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
				out.add(elt.value)
		return out
	return set()


def find_literal_mode_violations(source: str, filename: str = "<memory>") -> list[str]:
	"""Scan Python source for literal mode comparisons or forbidden constants/SQL."""
	violations: list[str] = []
	tree = ast.parse(source, filename=filename)

	for node in ast.walk(tree):
		# Flag forbidden hardcoded mode tuples like CASH_MODES = ("Cash",)
		if isinstance(node, ast.Assign):
			for target in node.targets:
				if isinstance(target, ast.Name) and target.id in FORBIDDEN_CONSTANTS:
					violations.append(f"{filename}:{node.lineno}: forbidden constant {target.id}")

		# Flag SQL strings with `mode = 'Cash'` etc.
		if isinstance(node, ast.Constant) and isinstance(node.value, str):
			if SQL_LITERAL_MODE_RE.search(node.value):
				violations.append(f"{filename}:{node.lineno}: SQL literal mode comparison")

		# Flag AST comparisons of a mode-like variable against literal tender names
		if isinstance(node, ast.Compare):
			operands = [node.left, *node.comparators]
			for left, op, right in zip(operands[:-1], node.ops, operands[1:], strict=True):
				if not isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
					continue
				# Reject `mode in CASH_MODES` / `mode == "Cash"` / `"Cash" == mode`
				for a, b in ((left, right), (right, left)):
					if _is_mode_like_expr(a):
						if isinstance(b, ast.Name) and b.id in FORBIDDEN_CONSTANTS:
							violations.append(
								f"{filename}:{node.lineno}: literal mode constant comparison ({ast.unparse(node)})"
							)
							break
						lits = _extract_literal_strings(b)
						bad = lits & FORBIDDEN_MODE_LITERALS
						if bad:
							violations.append(
								f"{filename}:{node.lineno}: literal mode comparison against {sorted(bad)} ({ast.unparse(node)})"
							)
							break
	return violations


class TenderTypeGuardTestCase(unittest.TestCase):
	def test_money_path_modules_have_no_literal_mode_comparisons(self):
		all_violations: list[str] = []
		for rel in MONEY_PATH_MODULES:
			path = REPO_ROOT / rel
			self.assertTrue(path.exists(), f"Expected module missing: {rel}")
			all_violations.extend(find_literal_mode_violations(path.read_text(encoding="utf-8"), rel))
		self.assertEqual(
			all_violations,
			[],
			"Found literal Payment Method name comparisons in money paths — use payment_tender_type():\n"
			+ "\n".join(all_violations),
		)

	def test_row_correction_frontend_has_no_literal_mode_comparisons(self):
		path = REPO_ROOT / "frontend/src/components/RowCorrection.tsx"
		src = path.read_text(encoding="utf-8")
		matches = FRONTEND_LITERAL_MODE_RE.findall(src)
		self.assertEqual(
			matches,
			[],
			f"RowCorrection.tsx must use tender_type instead of literal mode checks: {matches}",
		)

	def test_guard_catches_new_literal_comparisons_and_allows_tender_type_checks(self):
		bad_samples = [
			'if mode == "Cash":\n    pass\n',
			'if pay.mode == "Foreign Cash":\n    pass\n',
			'if mode in ("Cash", "Bank Transfer"):\n    pass\n',
			'CASH_MODES = ("Cash",)\n',
			'sql = "SELECT * FROM `tabCashier Transaction` t WHERE t.mode = \'Cash\'"\n',
		]
		for sample in bad_samples:
			with self.subTest(sample=sample.strip()):
				self.assertTrue(
					find_literal_mode_violations(sample, "<synthetic>"),
					f"Guard failed to catch: {sample!r}",
				)

		good_samples = [
			'tender = payment_tender_type(mode, prop)\nif tender == "Foreign Cash":\n    pass\n',
			'if payment_tender_type(pay.mode, doc.property) in ("Cash", "Foreign Cash"):\n    pass\n',
			'dest_tender = payment_tender_type(destination, doc.property)\nif dest_tender == "Bank Transfer":\n    pass\n',
		]
		for sample in good_samples:
			with self.subTest(sample=sample.strip()):
				self.assertEqual(
					find_literal_mode_violations(sample, "<synthetic>"),
					[],
					f"Guard produced false positive on: {sample!r}",
				)
