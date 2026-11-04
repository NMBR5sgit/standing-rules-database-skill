#!/usr/bin/env python3
"""Snapshot test (A3): the SKILL.md Tier-1 section must stay terse.

Prevents drift back to verbose paragraph form, which every user pays for on
every turn of every job.
"""
import pathlib
import unittest

SKILL_MD = pathlib.Path(__file__).resolve().parent.parent / "SKILL.md"


class SkillDocTest(unittest.TestCase):

    def _tier1_section(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        lines = text.splitlines()
        start = end = None
        for i, line in enumerate(lines):
            if line.startswith("## Tier 1 — HARD rules"):
                start = i
            elif start is not None and line.startswith("## ") and i > start:
                end = i
                break
        if start is None:
            self.fail("no '## Tier 1 — HARD rules' section in SKILL.md")
        return lines[start:end if end is not None else len(lines)]

    def test_no_line_exceeds_200_chars(self):
        for line in self._tier1_section():
            self.assertLessEqual(len(line), 200,
                                 f"line too long ({len(line)} chars): {line[:80]}...")

    def test_terse_shape_present(self):
        section = "\n".join(self._tier1_section())
        self.assertIn("[SR-T1x] <one imperative sentence>", section)

    def test_examples_present(self):
        section = "\n".join(self._tier1_section())
        self.assertIn("Public identity = legal business name", section)

    def test_old_verbose_phrasing_removed(self):
        section = "\n".join(self._tier1_section())
        self.assertNotIn("e.g. a required output-format rule", section)


if __name__ == "__main__":
    unittest.main()
