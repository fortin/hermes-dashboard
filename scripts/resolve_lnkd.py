#!/usr/bin/env python3
"""Replace lnkd.in short links in a Markdown note with their destinations.

  python3 scripts/resolve_lnkd.py "/path/to/note.md"

Reads the note, follows each unique https://lnkd.in/<code> link, and writes
the file back when at least one destination was found. Links inside fenced
code blocks are left alone. A timeout, HTTP error, or page that never yields
a destination keeps the original short link.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 512_000
SHORT_PREFIX = "https://lnkd.in/"
TRAILING_PUNCTUATION = ".,;:!?"
REDIRECT_PATH_MARKERS = (
    "/safety",
    "/redir",
    "/redirect",
    "/authwall",
    "/checkpoint",
    "/uas/login",
)

# final_url, html_body, error_reason (empty string on success)
FetchResult = tuple[str, str, str]


def _host_name(host: str) -> str:
    name = host.lower().rstrip(".")
    if name.startswith("www."):
        name = name[4:]
    return name


def is_shortener_host(host: str) -> bool:
    name = _host_name(host)
    return name == "lnkd.in" or name.endswith(".lnkd.in")


def is_linkedin_host(host: str) -> bool:
    name = _host_name(host)
    return is_shortener_host(host) or name == "linkedin.com" or name.endswith(".linkedin.com")


def opening_fence(line: str) -> str | None:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return None
    body = stripped.rstrip("\r\n")
    if body.startswith("```"):
        marker = "`"
    elif body.startswith("~~~"):
        marker = "~"
    else:
        return None
    length = 0
    for char in body:
        if char != marker:
            break
        length += 1
    if length < 3:
        return None
    return marker * length


def closing_fence(line: str, opener: str) -> bool:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return False
    body = stripped.rstrip("\r\n").rstrip(" \t")
    marker = opener[0]
    if not body or any(char != marker for char in body):
        return False
    return len(body) >= len(opener)


def split_fenced_code(text: str) -> list[tuple[str, bool]]:
    """Split text into (chunk, is_code) pieces, preserving every character."""
    lines = text.splitlines(keepends=True)
    parts: list[tuple[str, bool]] = []
    prose: list[str] = []
    code: list[str] = []
    in_code = False
    opener = ""

    def flush(bucket: list[str], is_code: bool) -> None:
        if bucket:
            parts.append(("".join(bucket), is_code))
            bucket.clear()

    for line in lines:
        if not in_code:
            found = opening_fence(line)
            if found:
                flush(prose, False)
                in_code = True
                opener = found
                code.append(line)
            else:
                prose.append(line)
            continue
        code.append(line)
        if closing_fence(line, opener):
            in_code = False
            opener = ""
            flush(code, True)
    flush(prose, False)
    flush(code, True)
    return parts


def _trim_match(raw: str) -> str:
    return raw.rstrip(TRAILING_PUNCTUATION)


def find_short_links(text: str) -> list[tuple[int, int, str, str]]:
    """Return (start, end, matched_url, code) for short links in prose."""
    found: list[tuple[int, int, str, str]] = []
    needle = "https://"
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            break
        rest = text[index:]
        host = "https://www.lnkd.in/"
        prefix = SHORT_PREFIX
        if rest.startswith(host):
            code_at = index + len(host)
        elif rest.startswith(prefix):
            code_at = index + len(prefix)
        else:
            start = index + len(needle)
            continue
        cursor = code_at
        while cursor < len(text) and (text[cursor].isalnum() or text[cursor] in "_-"):
            cursor += 1
        if cursor == code_at:
            start = index + len(needle)
            continue
        if cursor < len(text) and text[cursor] == "?":
            cursor += 1
            while cursor < len(text) and text[cursor] not in " \t\r\n)]>\"'":
                cursor += 1
        raw = _trim_match(text[index:cursor])
        end = index + len(raw)
        code = raw.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        found.append((index, end, raw, code))
        start = end
    return found


MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")


def label_code(label: str) -> str | None:
    """Return the short-link code when the Markdown label is only that URL."""
    text = label.strip().strip("*_").strip()
    matches = find_short_links(text)
    if len(matches) != 1:
        return None
    start, end, _raw, code = matches[0]
    if start == 0 and end == len(text):
        return code
    return None


def replacement_spans(text: str) -> list[tuple[int, int, str]]:
    """Spans to rewrite: a whole Markdown link whose label is a short URL, else the URL."""
    spans: list[tuple[int, int, str]] = []
    covered: list[tuple[int, int]] = []
    for match in MD_LINK_RE.finditer(text):
        code = label_code(match.group(1))
        if not code:
            continue
        spans.append((match.start(), match.end(), code))
        covered.append((match.start(), match.end()))
    for start, end, _raw, code in find_short_links(text):
        if any(span_start <= start and end <= span_end for span_start, span_end in covered):
            continue
        spans.append((start, end, code))
    spans.sort()
    return spans


def strip_tracking(url: str) -> str:
    parts = urlparse(url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "trk" and key != "trackingId" and not key.startswith("utm_")
    ]
    return urlunparse(parts._replace(query=urlencode(kept)))


def _http_url(value: str) -> str | None:
    candidate = value.strip()
    for _ in range(2):
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate
        decoded = unquote(candidate)
        if decoded == candidate:
            return None
        candidate = decoded
    return None


def _is_redirect_page(url: str) -> bool:
    parsed = urlparse(url)
    if not is_linkedin_host(parsed.hostname or ""):
        return False
    path = parsed.path.lower()
    if any(marker in path for marker in REDIRECT_PATH_MARKERS):
        return True
    keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
    return "session_redirect" in keys or "sessionredirect" in keys


def query_destination(url: str) -> str | None:
    parsed = urlparse(url)
    path = parsed.path.lower()
    host = (parsed.hostname or "").lower()
    safetyish = any(marker in path for marker in ("/safety", "/redir", "/redirect"))
    on_shortener = is_shortener_host(host)
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        take = lowered in {"session_redirect", "sessionredirect"} or (
            lowered == "url" and (safetyish or on_shortener)
        )
        if not take:
            continue
        dest = _http_url(value)
        if dest:
            return dest
    return None


class _ContinueParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: str | None = None
        self._href: str | None = None
        self._capture = False
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.found or tag != "a":
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        href = values.get("href", "")
        if not href:
            return
        if values.get("data-tracking-control-name") == "external_url_click" and _off_linkedin(href):
            self.found = href
            return
        label = f"{values.get('aria-label', '')} {values.get('title', '')}".lower()
        if "continue" in label and _off_linkedin(href):
            self.found = href
            return
        self._href = href
        self._capture = True
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._capture or self.found:
            return
        text = " ".join("".join(self._text).split())
        href = self._href or ""
        if _off_linkedin(href) and ("continue" in text.lower() or text == href):
            self.found = href
        self._capture = False
        self._href = None


def _off_linkedin(href: str) -> bool:
    if not href.startswith(("http://", "https://")):
        return False
    return not is_linkedin_host(urlparse(href).hostname or "")


def continue_link(html: str) -> str | None:
    parser = _ContinueParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    return parser.found


def destination_from(final_url: str, body: str) -> str | None:
    if not final_url:
        return None
    current = final_url
    html = body
    seen: set[str] = set()
    for _ in range(4):
        if current in seen:
            return None
        seen.add(current)
        parsed = urlparse(current)
        host = parsed.hostname or ""
        if not is_linkedin_host(host):
            return strip_tracking(current)
        external = query_destination(current)
        if external and external != current:
            current = external
            html = ""
            continue
        if html and (_is_redirect_page(current) or is_shortener_host(host)):
            link = continue_link(html)
            if link and link != current:
                current = link
                html = ""
                continue
        if is_shortener_host(host) or _is_redirect_page(current):
            return None
        return strip_tracking(current)
    return None


def _needs_body(final_url: str) -> bool:
    parsed = urlparse(final_url)
    host = parsed.hostname or ""
    if not is_linkedin_host(host):
        return False
    if query_destination(final_url):
        return False
    if is_shortener_host(host) or _is_redirect_page(final_url):
        return True
    return False


def fetch(url: str) -> FetchResult:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            final = response.geturl()
            body = ""
            if _needs_body(final):
                body = response.read(MAX_BODY_BYTES).decode("utf-8", errors="replace")
            return final, body, ""
    except urllib.error.HTTPError as exc:
        return "", "", f"http {exc.code}"
    except TimeoutError:
        return "", "", "timeout"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return "", "", "timeout"
        return "", "", "error"


def resolve_short_link(code: str, fetch_url=None) -> tuple[str | None, str]:
    if fetch_url is None:
        fetch_url = fetch
    short = f"{SHORT_PREFIX}{code}"
    final, body, error = fetch_url(short)
    if error:
        return None, error
    dest = destination_from(final, body)
    if not dest or dest == short:
        return None, "unchanged"
    return dest, ""


def rewrite_note(text: str, fetch_url=None) -> tuple[str, list[tuple[str, str | None, str]]]:
    parts = split_fenced_code(text)
    codes: list[str] = []
    seen: set[str] = set()
    for chunk, is_code in parts:
        if is_code:
            continue
        for _, _, _, code in find_short_links(chunk):
            if code not in seen:
                seen.add(code)
                codes.append(code)

    resolved: dict[str, str] = {}
    report: list[tuple[str, str | None, str]] = []
    for code in codes:
        short = f"{SHORT_PREFIX}{code}"
        dest, reason = resolve_short_link(code, fetch_url)
        if dest:
            resolved[code] = dest
            report.append((short, dest, ""))
        else:
            report.append((short, None, reason or "unchanged"))

    if not resolved:
        return text, report

    rewritten: list[str] = []
    for chunk, is_code in parts:
        if is_code or not find_short_links(chunk):
            rewritten.append(chunk)
            continue
        pieces: list[str] = []
        last = 0
        for start, end, code in replacement_spans(chunk):
            pieces.append(chunk[last:start])
            pieces.append(resolved.get(code, chunk[start:end]))
            last = end
        pieces.append(chunk[last:])
        rewritten.append("".join(pieces))
    return "".join(rewritten), report


def write_note(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.resolve-lnkd.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replace lnkd.in links in a Markdown note with their destinations."
    )
    parser.add_argument("note", type=Path, help="Path to the Markdown note")
    args = parser.parse_args(argv)
    path: Path = args.note
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 1

    original = path.read_text(encoding="utf-8")
    updated, report = rewrite_note(original)
    replaced = 0
    for short, dest, reason in report:
        print(short)
        if dest:
            replaced += 1
            print(f"  -> {dest}")
        else:
            print(f"  unchanged ({reason})")

    if updated == original:
        print("No changes written.")
        return 0

    write_note(path, updated)
    print(f"Wrote {path} ({replaced} replaced)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
