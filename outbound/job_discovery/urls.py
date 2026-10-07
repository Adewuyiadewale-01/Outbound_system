"""URL helpers ported from ``job_discovery/src/url.mjs``.

``canonicalize_url`` reproduces Node's WHATWG ``URL`` normalization exactly for
the transforms the reference implementation performs (fragment removal, host
case, tracking-param removal + conditional re-serialization, trailing-slash
stripping). The behavior was extracted empirically against Node v22 and is
locked by the golden checks in ``tests/test_job_discovery_urls.py``.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlsplit

_TRACKING_KEYS = frozenset(
    {
        "gclid",
        "fbclid",
        "ref",
        "source",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
    }
)

_ATS_ID_PARAMS = ("jobId", "job_id", "gh_jid", "lever-origin", "opening", "requisitionId")

_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21, "ws": 80, "wss": 443}

# Parse-time encoding sets for special schemes (http/https), verified against Node:
# query encodes space, double-quote, <, >, and single-quote; path additionally
# encodes backtick, braces, and caret (but keeps pipe and brackets, and turns
# backslash into "/" before parsing).
_SPECIAL_QUERY_ESCAPES = {" ": "%20", '"': "%22", "<": "%3C", ">": "%3E", "'": "%27"}
_PATH_ESCAPES = {
    " ": "%20",
    '"': "%22",
    "<": "%3C",
    ">": "%3E",
    "`": "%60",
    "{": "%7B",
    "}": "%7D",
    "^": "%5E",
}

_URLENCODED_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789*-._")


def stable_hash(value: str) -> str:
    """sha256 hex, first 24 chars (the reference's identity digest)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def normalized_text(value: str = "") -> str:
    """Lowercase, non-alphanumeric runs collapsed to single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _pct_utf8(character: str) -> str:
    return "".join(f"%{byte:02X}" for byte in character.encode("utf-8"))


def _encode_special_query(raw: str) -> str:
    out: list[str] = []
    for character in raw:
        code = ord(character)
        if code < 0x20 or code > 0x7E:
            out.append(_pct_utf8(character))
        else:
            out.append(_SPECIAL_QUERY_ESCAPES.get(character, character))
    return "".join(out)


def _encode_path(raw: str) -> str:
    out: list[str] = []
    for character in raw:
        code = ord(character)
        if code < 0x20 or code > 0x7E:
            out.append(_pct_utf8(character))
        else:
            out.append(_PATH_ESCAPES.get(character, character))
    return "".join(out)


def _decode_component(text: str) -> str:
    raw = bytearray()
    index = 0
    while index < len(text):
        character = text[index]
        if character == "+":
            raw.append(0x20)
            index += 1
        elif character == "%" and re.fullmatch(r"[0-9A-Fa-f]{2}", text[index + 1 : index + 3]):
            raw.append(int(text[index + 1 : index + 3], 16))
            index += 3
        else:
            raw.extend(character.encode("utf-8"))
            index += 1
    return raw.decode("utf-8", errors="replace")


def _encode_component(text: str) -> str:
    out: list[str] = []
    for character in text:
        if character in _URLENCODED_SAFE:
            out.append(character)
        elif character == " ":
            out.append("+")
        else:
            out.append(_pct_utf8(character))
    return "".join(out)


def _parse_pairs(query: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for sequence in query.split("&"):
        if sequence == "":
            continue
        name, _, value = sequence.partition("=")
        pairs.append((_decode_component(name), _decode_component(value)))
    return pairs


def _is_tracked(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_KEYS or lowered.startswith("utm_")


def _resolve_query(raw_query: str) -> tuple[bool, str]:
    """(present, serialized) mirroring URLSearchParams delete + re-serialization.

    Node re-serializes the whole query only when a deletion occurs; otherwise the
    raw query is kept with parse-time encoding only.
    """
    pairs = _parse_pairs(raw_query)
    if not any(_is_tracked(name) for name, _ in pairs):
        return True, _encode_special_query(raw_query)
    remaining = [(name, value) for name, value in pairs if not _is_tracked(name)]
    if not remaining:
        return False, ""
    serialized = "&".join(
        f"{_encode_component(name)}={_encode_component(value)}" for name, value in remaining
    )
    return True, serialized


def _is_single_dot(segment: str) -> bool:
    return segment.lower() in (".", "%2e")


def _is_double_dot(segment: str) -> bool:
    return segment.lower() in ("..", ".%2e", "%2e.", "%2e%2e")


def _resolve_dot_segments(path: str) -> str:
    if not path.startswith("/"):
        return path
    out: list[str] = []
    for index, segment in enumerate(path.split("/")):
        if index == 0:
            out.append(segment)
            continue
        if _is_single_dot(segment):
            continue
        if _is_double_dot(segment):
            if len(out) > 1:
                out.pop()
            continue
        out.append(segment)
    return "/".join(out)


def _normalize_netloc(netloc: str, scheme: str) -> str:
    userinfo = ""
    hostport = netloc
    if "@" in netloc:
        userinfo, _, hostport = netloc.rpartition("@")
        userinfo += "@"
    is_ipv6 = hostport.startswith("[")
    if is_ipv6:
        closing = hostport.find("]")
        if closing == -1:
            raise ValueError(f"Invalid URL host: {netloc!r}")
        host = hostport[1:closing]
        rest = hostport[closing + 1 :]
        port = rest[1:] if rest.startswith(":") else ""
    elif ":" in hostport:
        host, _, port = hostport.rpartition(":")
    else:
        host, port = hostport, ""
    if not host:
        raise ValueError(f"Invalid URL host: {netloc!r}")
    if any(character in host for character in " \t\n\r"):
        raise ValueError(f"Invalid URL host: {netloc!r}")
    if port and not port.isdigit():
        raise ValueError(f"Invalid URL port: {netloc!r}")
    host = host.lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            pass
    if port:
        port_number = int(port)
        port = "" if _DEFAULT_PORTS.get(scheme) == port_number else str(port_number)
    host_out = f"[{host}]" if is_ipv6 else host
    return f"{userinfo}{host_out}" + (f":{port}" if port else "")


def canonicalize_url(value: str) -> str:
    """Port of ``canonicalizeUrl`` (see module docstring)."""
    without_fragment = value.split("#", 1)[0]
    parts = urlsplit(without_fragment)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"Invalid URL: {value!r}")
    scheme = parts.scheme.lower()
    netloc = _normalize_netloc(parts.netloc, scheme)
    raw_path = (parts.path or "/").replace("\\", "/")
    path = _encode_path(_resolve_dot_segments(raw_path))
    path = re.sub(r"/+$", "", path) or "/"
    if "?" in without_fragment:
        present, query = _resolve_query(parts.query)
        suffix = f"?{query}" if present else ""
    else:
        suffix = ""
    return f"{scheme}://{netloc}{path}{suffix}"


def find_ats_job_id(url: str) -> str | None:
    """Explicit ATS id param, else the last path segment when >= 5 chars."""
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    pairs = _parse_pairs(parts.query)
    for key in _ATS_ID_PARAMS:
        for name, value in pairs:
            if name == key:
                if value:
                    return value
                break
    segments = [segment for segment in parts.path.split("/") if segment]
    tail = segments[-1] if segments else ""
    if tail and len(tail) >= 5:
        return f"{hostname}:{tail}"
    return None


def is_career_landing_page_url(value: str, platform: str = "") -> bool:
    """Ashby ``/{company}`` boards are careers pages, not individual postings."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    hostname = parts.hostname or ""
    segments = [segment for segment in parts.path.split("/") if segment]
    return (
        bool(re.search(r"ashbyhq\.com$", hostname, re.IGNORECASE))
        and (platform == "Ashby" or not platform)
        and len(segments) < 2
    )


def is_job_posting_candidate(candidate: dict) -> bool:
    """Port of ``isJobPostingCandidate``."""
    return not is_career_landing_page_url(
        candidate.get("canonicalUrl", ""), candidate.get("platform", "")
    )


def extract_passthrough_token(value: str) -> str | None:
    """Return the redirect token when ``value`` is a Google passthrough link.

    Recognizes the modern ``google.com/goto?url=<token>`` wrapper and the older
    ``google.com/url?q=<token>`` form. Anything else returns ``None``.
    """
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    hostname = (parts.hostname or "").lower()
    if not hostname.endswith("google.com"):
        return None
    if parts.path == "/goto":
        key = "url"
    elif parts.path == "/url":
        key = "q"
    else:
        return None
    pairs = parse_qs(parts.query, keep_blank_values=True)
    values = pairs.get(key) or []
    token = values[0].strip() if values else ""
    return token or None


def default_passthrough_resolver(value: str) -> str:
    """Resolve a Google passthrough link to its final destination.

    Follows the redirect chain with cookieless, short-timeout requests (the
    endpoint answers 302 -> target without authentication and repeatably).
    Raises on network/HTTP failure so callers can fall back gracefully.
    """
    import httpx

    with httpx.Client(follow_redirects=False, timeout=15.0) as client:
        current = value
        for _ in range(5):
            response = client.get(current)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    break
                current = str(response.next_request.url) if response.next_request else location
                continue
            response.raise_for_status()
            return current
    raise RuntimeError(f"Redirect chain did not terminate for {value!r}")


def resolve_passthrough_url(
    value: str,
    *,
    cache: dict[str, str] | None = None,
    resolver=None,
) -> str:
    """Unwrap Google passthrough links to their real target URL.

    ``cache`` maps a passthrough URL to its already-resolved target so repeated
    tokens cost zero extra requests (one cache per search run). ``resolver``
    defaults to :func:`default_passthrough_resolver` and may be replaced in
    tests. On any resolution failure the original ``value`` is returned
    unchanged -- resolution never drops a result outright.
    """
    token = extract_passthrough_token(value)
    if not token:
        return value
    if cache is not None and value in cache:
        return cache[value]
    resolve = resolver or default_passthrough_resolver
    try:
        target = resolve(value)
    except Exception:  # noqa: BLE001 -- fallback keeps the result flow alive
        return value
    target = target.strip()
    if not target:
        return value
    try:
        target = canonicalize_url(target)
    except ValueError:
        return value
    if cache is not None:
        cache[value] = target
    return target
