"""Check kamra/translations/<lang>.csv against the extracted backend strings.

Fails when a catalogue row cannot work at runtime:

* a source the code asks for has no translation (the UI would fall back to
  English, which is exactly the "half translated" state we must avoid);
* a translation has different `{placeholders}` than its source, so
  `.format()` raises or prints the wrong value;
* a translation is empty or blank;
* the same source appears twice (Frappe takes the first match, so the second
  is dead weight and hides edits);
* a row exists for a string the code no longer asks for (orphan).

Usage:
    python scripts/i18n/check_backend_translations.py              # defaults to fr
    python scripts/i18n/check_backend_translations.py --lang fr
    python scripts/i18n/check_backend_translations.py --lang zh
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STRINGS = HERE / "backend-strings.csv"
TRANSLATIONS_DIR = ROOT / "kamra" / "translations"

PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def read_rows(path: Path) -> list[list[str]]:
	with path.open(encoding="utf-8", newline="") as handle:
		rows = [row for row in csv.reader(handle) if row]
	return rows[1:] if rows and rows[0][0] == "source" else rows


def load_expected_strings() -> tuple[set[str], set[str]]:
	if str(HERE) not in sys.path:
		sys.path.insert(0, str(HERE))
	from extract_backend_strings import extract_strings

	# Frappe also translates Select options and field descriptions when available.
	display_strings, kinds, _ = extract_strings(include_field_help=True)
	required = {key for key, sources in kinds.items() if sources - {"field help"}}
	return required, set(display_strings)


def check_language(lang: str, expected: set[str], display_strings: set[str]) -> int:
	csv_path = TRANSLATIONS_DIR / f"{lang}.csv"
	if not csv_path.exists():
		print(f"{csv_path.relative_to(ROOT)} is missing", file=sys.stderr)
		return 2

	rows = read_rows(csv_path)
	label = f"{lang}.csv"

	problems: list[str] = []
	seen: set[str] = set()
	for number, row in enumerate(rows, 2):
		if len(row) < 2:
			problems.append(f"{label}:{number}: not a source,translation row")
			continue
		source, translation = row[0], row[1]
		if source in seen:
			problems.append(f"{label}:{number}: duplicate source {source!r}")
		seen.add(source)
		if not translation.strip():
			problems.append(f"{label}:{number}: empty translation for {source!r}")
			continue
		if source not in display_strings:
			problems.append(f"{label}:{number}: orphan source {source!r} (no longer in the code)")
		if sorted(PLACEHOLDER.findall(source)) != sorted(PLACEHOLDER.findall(translation)):
			problems.append(
				f"{label}:{number}: placeholder mismatch\n"
				f"    source      {source!r} -> {sorted(PLACEHOLDER.findall(source))}\n"
				f"    translation {translation!r} -> {sorted(PLACEHOLDER.findall(translation))}"
			)

	missing = sorted(expected - seen)
	for source in missing:
		problems.append(f"{label}: missing translation: {source!r}")

	if problems:
		print(f"{label}: {len(problems)} problem(s):")
		for problem in problems:
			print(f"  {problem}")
		return 1

	print(
		f"backend translations ({label}) OK: {len(seen & expected)}/{len(expected)} required strings, "
		f"placeholders match, no orphans or duplicates"
	)
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Validate backend translation catalogues.")
	parser.add_argument(
		"--lang",
		default="fr",
		help="Language code under kamra/translations/<lang>.csv (e.g. fr, zh, or comma-separated list).",
	)
	args = parser.parse_args(argv)
	expected, display_strings = load_expected_strings()
	langs = [part.strip() for part in args.lang.split(",") if part.strip()]
	rc = 0
	for lang in langs:
		code = check_language(lang, expected, display_strings)
		if code != 0:
			rc = code
	return rc


if __name__ == "__main__":
	sys.exit(main())
