#!/usr/bin/env python3
"""Tests for scripts/resolve_lnkd.py. No network."""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import resolve_lnkd


def _fetch(final: str, body: str = "", error: str = ""):
    def fetch(_url: str) -> resolve_lnkd.FetchResult:
        return final, body, error

    return fetch


class FindShortLinksTests(unittest.TestCase):
    def test_stops_at_markdown_and_punctuation(self):
        text = "See [Ada](https://lnkd.in/abc). And https://lnkd.in/def,"
        found = resolve_lnkd.find_short_links(text)
        self.assertEqual(
            [(raw, code) for _, _, raw, code in found],
            [("https://lnkd.in/abc", "abc"), ("https://lnkd.in/def", "def")],
        )

    def test_keeps_query_on_the_short_link_but_not_trailing_period(self):
        text = "https://www.lnkd.in/abc?trk=1."
        found = resolve_lnkd.find_short_links(text)
        self.assertEqual(found[0][2], "https://www.lnkd.in/abc?trk=1")
        self.assertEqual(found[0][3], "abc")

    def test_skips_fenced_code(self):
        text = "```\nhttps://lnkd.in/abc\n```\nhttps://lnkd.in/def\n"
        parts = resolve_lnkd.split_fenced_code(text)
        prose = "".join(chunk for chunk, is_code in parts if not is_code)
        code = "".join(chunk for chunk, is_code in parts if is_code)
        self.assertEqual(resolve_lnkd.find_short_links(prose)[0][3], "def")
        self.assertEqual(resolve_lnkd.find_short_links(code)[0][3], "abc")


class DestinationTests(unittest.TestCase):
    def test_external_redirect_strips_tracking_and_keeps_other_params(self):
        dest = resolve_lnkd.destination_from(
            "https://example.com/a?utm_source=li&id=1&trk=x&trackingId=y",
            "",
        )
        self.assertEqual(dest, "https://example.com/a?id=1")

    def test_safety_page_uses_url_param(self):
        dest = resolve_lnkd.destination_from(
            "https://www.linkedin.com/safety/go?url=https%3A%2F%2Fexample.com%2Fpath&trk=flagship",
            "",
        )
        self.assertEqual(dest, "https://example.com/path")

    def test_authwall_session_redirect_lands_on_profile(self):
        dest = resolve_lnkd.destination_from(
            "https://www.linkedin.com/authwall?session_redirect="
            "https%3A%2F%2Fwww.linkedin.com%2Fin%2Fada%3Ftrk%3Dpublic",
            "",
        )
        self.assertEqual(dest, "https://www.linkedin.com/in/ada")

    def test_interstitial_external_url_click(self):
        html = (
            '<a class="artdeco-button" data-tracking-control-name="external_url_click" '
            'data-tracking-will-navigate href="https://www.youtube.com/watch?v=ii1jcLg-eIQ">'
            " https://www.youtube.com/watch?v=ii1jcLg-eIQ </a>"
            '<a data-tracking-control-name="learn_more_click" '
            'href="https://www.linkedin.com/help/linkedin/answer/a1341680"> Learn more </a>'
        )
        dest = resolve_lnkd.destination_from("https://lnkd.in/d_DF5Au7", html)
        self.assertEqual(dest, "https://www.youtube.com/watch?v=ii1jcLg-eIQ")

    def test_continue_link_ignores_logo_and_nested_text(self):
        html = (
            '<a href="https://www.linkedin.com/">LinkedIn</a>'
            '<a href="https://example.com/doc"><span>Continue</span></a>'
        )
        dest = resolve_lnkd.destination_from("https://lnkd.in/abc", html)
        self.assertEqual(dest, "https://example.com/doc")

    def test_profile_url_is_kept_after_stripping_trk(self):
        dest = resolve_lnkd.destination_from(
            "https://www.linkedin.com/in/ada?trk=public_profile",
            "",
        )
        self.assertEqual(dest, "https://www.linkedin.com/in/ada")


class RewriteTests(unittest.TestCase):
    def test_replaces_prose_once_per_code_and_leaves_fences(self):
        text = (
            "```\nhttps://lnkd.in/abc\n```\n"
            "See [Ada](https://lnkd.in/abc).\n"
            "Again https://www.lnkd.in/abc?trk=1 and https://lnkd.in/missing\n"
        )
        calls: list[str] = []

        def fetch(url: str) -> resolve_lnkd.FetchResult:
            calls.append(url)
            code = url.rsplit("/", 1)[-1]
            if code == "missing":
                return "", "", "timeout"
            return "https://example.com/ada?utm_source=li", "", ""

        updated, report = resolve_lnkd.rewrite_note(text, fetch)
        self.assertEqual(calls, ["https://lnkd.in/abc", "https://lnkd.in/missing"])
        self.assertIn("```\nhttps://lnkd.in/abc\n```\n", updated)
        self.assertIn("[Ada](https://example.com/ada).", updated)
        self.assertIn("Again https://example.com/ada and https://lnkd.in/missing\n", updated)
        self.assertEqual(
            report,
            [
                ("https://lnkd.in/abc", "https://example.com/ada", ""),
                ("https://lnkd.in/missing", None, "timeout"),
            ],
        )

    def test_bold_short_link_replaces_the_whole_markdown_link(self):
        href = (
            "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Flnkd%2Ein%2FenX9qSag"
            "&urlhash=EECZ&mt=rglF8Nih6PPlpRfrVSoOmwbKBsr7DPWKcveb369IV36izSLvofoTpBQU"
            "&isSdui=true"
        )
        text = f"Copied [**https://lnkd.in/enX9qSag**]({href}) from a post.\n"
        updated, report = resolve_lnkd.rewrite_note(
            text,
            _fetch("https://example.com/post?utm_source=li"),
        )
        self.assertEqual(updated, "Copied https://example.com/post from a post.\n")
        self.assertEqual(report, [("https://lnkd.in/enX9qSag", "https://example.com/post", "")])

    def test_unresolved_bold_short_link_stays_intact(self):
        text = "[**https://lnkd.in/enX9qSag**](https://www.linkedin.com/safety/go/?url=https%3A%2F%2Flnkd%2Ein%2FenX9qSag)\n"
        updated, _report = resolve_lnkd.rewrite_note(text, _fetch("", "", "timeout"))
        self.assertEqual(updated, text)

    def test_failed_lookup_does_not_change_text(self):
        text = "https://lnkd.in/abc\n"
        updated, report = resolve_lnkd.rewrite_note(text, _fetch("", "", "http 999"))
        self.assertEqual(updated, text)
        self.assertEqual(report, [("https://lnkd.in/abc", None, "http 999")])


class FetchTests(unittest.TestCase):
    def test_http_999_is_reported(self):
        error = urllib.error.HTTPError(
            "https://lnkd.in/abc",
            999,
            "denied",
            hdrs=None,
            fp=None,
        )
        with patch("resolve_lnkd.urllib.request.urlopen", side_effect=error):
            final, body, reason = resolve_lnkd.fetch("https://lnkd.in/abc")
        self.assertEqual((final, body, reason), ("", "", "http 999"))


class MainTests(unittest.TestCase):
    def test_writes_note_only_when_a_destination_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            path.write_text("See https://lnkd.in/abc.\n", encoding="utf-8")

            def fetch(_url: str) -> resolve_lnkd.FetchResult:
                return "https://example.com/ada", "", ""

            with patch("resolve_lnkd.fetch", fetch):
                code = resolve_lnkd.main([str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "See https://example.com/ada.\n")
            self.assertFalse((Path(tmp) / ".note.md.resolve-lnkd.tmp").exists())

    def test_leaves_file_untouched_when_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            original = "https://lnkd.in/abc\n"
            path.write_text(original, encoding="utf-8")

            def fetch(_url: str) -> resolve_lnkd.FetchResult:
                return "", "", "timeout"

            with patch("resolve_lnkd.fetch", fetch):
                code = resolve_lnkd.main([str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
