"""Secret masking and output truncation (D11).

``redact()`` is applied to every tool result before the LLM sees it, to the trace and to the
final report, so secret values never leave the process. The *fact* that a secret is present
stays visible: the key name, the quotes and a known provider prefix (e.g. ``AKIA``) are kept,
so the agent can still report "hardcoded AWS key in settings.py line 12".
"""

from __future__ import annotations

import math
import re
from collections import Counter

DEFAULT_OUTPUT_LIMIT = 8000  # chars per tool result (D7)

# -- 1. Provider tokens with a recognisable shape: keep a safe prefix, mask the rest. ----------
_PROVIDER_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    # (kind, pattern, number of leading chars to keep)
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"), 4),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), 4),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), 11),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), 6),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), 5),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), 31),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 4),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}\b"), 8),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), 7),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"), 3),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), 4),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"), 3),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 3),
]

# -- 2. Credentials inside URLs and auth headers. ------------------------------------------
_URL_CREDENTIALS = re.compile(
    r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@'\"]+:)(?P<pw>[^\s@/'\"]+)(?P<at>@)"
)
_AUTH_HEADER = re.compile(r"(?P<pre>\b(?:Bearer|Basic|token)\s+)(?P<val>[A-Za-z0-9._~+/=-]{16,})")

# -- 3. key = value assignments where the key name says it is a secret. ---------------------
# Matched in two parts so the scan stays linear on very long lines (minified files, lock
# files): the key+separator regex can only start at a token boundary, and the value is then
# read with an anchored match. Only the key+separator is consumed, so a harmless assignment
# never swallows a secret one that follows it (e.g. ``env: DB_PASSWORD=x``).
_KEY_SEP = re.compile(
    r"""(?<![A-Za-z0-9_.\-])(?P<kq>["']?)(?P<key>[A-Za-z0-9_.-]+)(?P=kq)"""
    r"""(?P<sep>[ \t]*(?::=|=>|[:=])[ \t]*)"""
)
_VALUE = re.compile(r"""(?P<q>["'`]?)(?P<val>[^\s"'`,;]+)(?P=q)""")
_SECRET_WORDS = {
    "password", "passwd", "pwd", "pass", "passphrase", "secret", "secrets",
    "token", "tokens", "apikey", "credential", "credentials", "privatekey",
}  # fmt: skip
_SECRET_PAIRS = {
    ("api", "key"), ("access", "key"), ("private", "key"), ("secret", "key"),
    ("auth", "token"), ("client", "secret"), ("signing", "key"), ("encryption", "key"),
    ("master", "key"), ("account", "key"), ("connection", "string"),
}  # fmt: skip
_NON_SECRET_VALUES = {
    "true", "false", "none", "null", "nil", "undefined", "str", "string", "int", "bool",
    "bytes", "optional", "any", "required", "changeme", "change_me", "change-me", "example",
    "placeholder", "dummy", "redacted", "todo", "tbd", "secret", "password", "env",
    "read", "write", "read-all", "write-all",  # GitHub Actions permission levels
}  # fmt: skip

# -- 4. Private key blocks and long high-entropy tokens. ------------------------------------
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.DOTALL,
)
_B64_RUN = re.compile(r"[A-Za-z0-9+/=]{8,}")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9+/_=-])")
_HASH_PREFIX = re.compile(r"(?:sha1|sha256|sha384|sha512|md5)[-:]", re.IGNORECASE)
_HASH_SUFFIX = re.compile(r"(?:sha1|sha256|sha384|sha512|md5)[-:]$", re.IGNORECASE)


def _split_words(key: str) -> list[str]:
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip("\"'"))  # camelCase -> camel_Case
    return [w for w in re.split(r"[_.\-]+", key.lower()) if w]


def is_secret_key(key: str) -> bool:
    words = _split_words(key)
    if any(w in _SECRET_WORDS for w in words):
        return True
    # Glued names such as PGPASSWORD, dbpassword, githubtoken (but not "bypass").
    if any(w.endswith(("password", "passwd", "secret", "token", "apikey")) for w in words):
        return True
    return any(pair in _SECRET_PAIRS for pair in zip(words, words[1:], strict=False))


def is_placeholder(value: str) -> bool:
    """Values that are clearly not literal secrets: references, templates, dummies."""
    v = value.strip().rstrip(")]}")  # e.g. "write]" when printed inside brackets
    low = v.lower()
    if len(v) < 4 or v.isdigit():
        return True
    if any(tok in v for tok in ("${", "{{", "$(", "<", ">", "(", "[", "%", "os.environ", "getenv")):
        return True
    if v.startswith("$") or v.startswith("!"):
        return True  # $VAR, YAML tags like !vault
    if low in _NON_SECRET_VALUES or low.startswith(("your", "my-", "my_", "replace", "insert")):
        return True
    if len(set(v)) <= 2:
        return True  # "xxxx", "****", "...."
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", v):
        return True  # dotted attribute reference: settings.SECRET_KEY, config.db.password
    return "redacted" in low


def shannon_entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_random(token: str) -> bool:
    if len(token) < 32 or shannon_entropy(token) < 4.3:
        return False  # hex digests (max 4.0 bits/char) are not masked
    classes = sum(bool(re.search(p, token)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    return classes >= 3 and not re.search(r"[a-z]{3,}[A-Z][a-z]{3,}[A-Z][a-z]{3,}", token)


def _mask(kind: str, keep: str = "") -> str:
    return f"{keep}[REDACTED:{kind}]"


def _mask_private_key(match: re.Match[str]) -> str:
    lines = match.group(0).split("\n")
    if len(lines) <= 1:
        return _mask("private_key")
    head, body, tail = lines[0], lines[1:-1], lines[-1]
    if "-----END" not in tail:
        body, tail = lines[1:], ""
    masked = [_B64_RUN.sub(_mask("private_key"), ln) for ln in body]
    return "\n".join([head, *masked] + ([tail] if tail else []))


def _redact_assignments(text: str) -> str:
    """Mask values of ``key = value`` / ``key: value`` pairs whose key names a secret."""
    out: list[str] = []
    pos = 0
    for m in _KEY_SEP.finditer(text):
        if not is_secret_key(m.group("key")):
            continue
        v = _VALUE.match(text, m.end())
        if v is None or is_placeholder(v.group("val")):
            continue
        out.append(text[pos : v.start("val")])
        out.append(_mask("secret_value"))
        pos = v.end("val")
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def redact(text: str) -> str:
    """Mask secret values in ``text``. Idempotent; non-secret content is left unchanged."""
    if not text:
        return text

    text = _PRIVATE_KEY_BLOCK.sub(_mask_private_key, text)

    for kind, pattern, keep in _PROVIDER_PATTERNS:
        text = pattern.sub(lambda m, k=kind, n=keep: _mask(k, m.group(0)[:n]), text)

    text = _URL_CREDENTIALS.sub(
        lambda m: (
            m.group(0)
            if is_placeholder(m.group("pw"))
            else f"{m.group('pre')}{_mask('password')}{m.group('at')}"
        ),
        text,
    )
    text = _AUTH_HEADER.sub(lambda m: f"{m.group('pre')}{_mask('auth_token')}", text)

    text = _redact_assignments(text)

    def _long_token(m: re.Match[str]) -> str:
        token = m.group(0)
        if "REDACTED" in token or not _looks_random(token):
            return token
        line_start = max(text.rfind("\n", 0, m.start()), 0)
        word = re.split(r"[\s\"'`(<]", text[line_start : m.start()])[-1]
        if "://" in word:
            return token  # part of a URL path (download links in lock files, etc.)
        before = text[max(0, m.start() - 7) : m.start()]
        if _HASH_PREFIX.match(token) or _HASH_SUFFIX.search(before):
            return token  # lock-file integrity hashes (sha512-..., sha256:...)
        return _mask("high_entropy_string")

    return _LONG_TOKEN.sub(_long_token, text)


def truncate(text: str, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    """Cut ``text`` to ``limit`` characters with an explicit marker the LLM can act on."""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit * 0.8:
        cut = limit
    dropped = len(text) - cut
    return (
        text[:cut]
        + f"\n...[truncated {dropped} chars; narrow the request, e.g. a line range or path_glob]"
    )


def sanitize(text: str, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    """Redact then truncate: the single gate every tool result passes before the LLM."""
    return truncate(redact(text), limit)
