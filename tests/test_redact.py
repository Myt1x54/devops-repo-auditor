"""Redaction tests.

Fake secrets are assembled at runtime (e.g. ``"ghp_" + ...``) so this file never contains a
literal token that GitHub push protection or secret scanners would flag.
"""

from __future__ import annotations

import pytest

from auditor.redact import is_placeholder, is_secret_key, redact, sanitize, truncate

AWS_ID = "AKIA" + "Q3EXAMPLE7XYZ12A"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYzEXAMPLEKEY"
GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
STRIPE = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"
JWT = (
    "eyJ"
    + "hbGciOiJIUzI1NiJ9"
    + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
RANDOM_40 = "q8Zr2LmX9vB4nT7kP1wY6cD3sF0hJ5gA8eR2uI9o"


def _assert_hidden(secret: str, text: str) -> None:
    out = redact(text)
    assert secret not in out, out
    assert "[REDACTED:" in out


@pytest.mark.parametrize(
    ("secret", "text", "kept"),
    [
        (AWS_ID, f"aws_access_key_id = {AWS_ID}", "AKIA[REDACTED:"),
        (AWS_SECRET, f'AWS_SECRET_ACCESS_KEY = "{AWS_SECRET}"', 'AWS_SECRET_ACCESS_KEY = "['),
        (GH_TOKEN, f"token: {GH_TOKEN}", "token: "),
        (STRIPE, f"stripe.api_key = '{STRIPE}'", "sk_live_"),
        (JWT, f"Authorization: Bearer {JWT}", "Bearer "),
        ("hunter2!", "DB_PASSWORD=hunter2!", "DB_PASSWORD="),
        ("hunter2!", "PGPASSWORD=hunter2!", "PGPASSWORD="),
        ("s3cr3tValue", '{"client_secret": "s3cr3tValue"}', '"client_secret": "'),
        ("s3cr3tValue", "apiKey: s3cr3tValue", "apiKey: "),
        ("s3cr3tValue", "  2| SECRET_KEY = 's3cr3tValue'", "  2| SECRET_KEY = '"),
        ("p4ssw0rd", "DATABASE_URL=postgres://admin:p4ssw0rd@db.internal:5432/app", "admin:"),
        ("s3cr3tValue", "environment: DB_PASSWORD=s3cr3tValue", "environment: DB_PASSWORD="),
        (RANDOM_40, f"value {RANDOM_40} end", "value "),
    ],
)
def test_secrets_are_masked_but_context_kept(secret, text, kept):
    out = redact(text)
    assert secret not in out, out
    assert "[REDACTED:" in out
    assert kept in out


def test_private_key_body_masked_line_numbers_kept():
    body = ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7", "Zm9vYmFyYmF6", "abc"]
    text = "\n".join(
        [
            " 1| import x",
            " 2| -----BEGIN RSA PRIVATE KEY-----",
            f" 3| {body[0]}",
            f" 4| {body[1]}",
            " 5| -----END RSA PRIVATE KEY-----",
            " 6| done",
        ]
    )
    out = redact(text)
    assert body[0] not in out and body[1] not in out
    lines = out.splitlines()
    assert lines[1] == " 2| -----BEGIN RSA PRIVATE KEY-----"
    assert lines[2].startswith(" 3| [REDACTED:private_key]")
    assert lines[4] == " 5| -----END RSA PRIVATE KEY-----"
    assert lines[5] == " 6| done"


@pytest.mark.parametrize(
    "text",
    [
        "password = os.environ['DB_PASSWORD']",
        "API_KEY = os.getenv('API_KEY')",
        "token: ${{ secrets.GITHUB_TOKEN }}",
        "password: ${DB_PASSWORD}",
        "API_KEY=your_api_key_here",
        "SECRET_KEY=changeme",
        "password = settings.DATABASE_PASSWORD",
        "password: str",
        "token_expiry = 3600",
        "password_hash = hash_password(raw)",
        "PASSWORD=",
        "API_KEY=<your-key>",
        "secret: xxxxxxxx",
        "bypass = True",
        "uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2",
        'integrity "sha512-Xo4bB9XKN9WvjNQ8Y6eP2wF0kLhQ3mR7tZ1aB5cD8eF0gH2iJ4kL6mN8oP0qR2sT4uV6"',
        'hash = "sha256:Xo4bB9XKN9WvjNQ8Y6eP2wF0kLhQ3mR7tZ1aB5cD8eF0gH2iJ4"',
        "def test_redaction_of_a_really_long_function_name_here():",
        "class VeryLongClassNameForSomethingElse12:",
        "https://github.com/pallets/itsdangerous",
        "postgres://user:${DB_PASS}@localhost/db",
        "      id-token: write",
        "permissions=[id-token: write]",
        "permissions:\n  contents: read\n  id-token: write",
        '{ url = "https://files.pythonhosted.org/packages/a1/b2/'
        'Q9zXr2LmX9vB4nT7kP1wY6cD3sF0hJ5gA8eR2uI9o/x.whl" }',
    ],
)
def test_non_secrets_unchanged(text):
    assert redact(text) == text


def test_redact_is_idempotent():
    text = f"API_KEY = '{RANDOM_40}'\nAWS={AWS_ID}\nurl=https://u:p4ssw0rd@h/x"
    once = redact(text)
    assert redact(once) == once


def test_redact_empty():
    assert redact("") == ""


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("DB_PASSWORD", True),
        ("apiKey", True),
        ("client_secret", True),
        ("GITHUB_TOKEN", True),
        ("PGPASSWORD", True),
        ("bypass", False),
        ("passenger", False),
        ("tokenizer", False),
        ("author", False),
    ],
)
def test_is_secret_key(key, expected):
    assert is_secret_key(key) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("${X}", True), ("changeme", True), ("1234", True), ("abc", True), ("hunter2!", False)],
)
def test_is_placeholder(value, expected):
    assert is_placeholder(value) is expected


def test_truncate():
    assert truncate("short", 100) == "short"
    text = "\n".join(f"line {i}" for i in range(1000))
    out = truncate(text, 500)
    assert len(out) < 600
    assert "[truncated" in out and "chars" in out
    assert out.split("\n...[truncated")[0].endswith(tuple("0123456789"))  # cut on a line end


def test_sanitize_redacts_before_truncating():
    text = f"API_KEY = '{RANDOM_40}'\n" + "x\n" * 10_000
    out = sanitize(text, limit=200)
    assert RANDOM_40 not in out and "[truncated" in out


def test_redact_is_fast_on_very_long_lines():
    import time

    lines = ["a" * 200_000, "x=" * 50_000, "password" * 20_000, "'" + "b" * 100_000]
    start = time.perf_counter()
    for line in lines:
        redact(line)
    assert time.perf_counter() - start < 2.0
