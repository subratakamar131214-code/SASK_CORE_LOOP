"""Secret/PII redaction and the fail-closed repository scanner."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from src.compliance.secretscan import (
    SecretFinding,
    assert_clean,
    scan_file,
    scan_text,
    scan_tree,
)
from src.core.errors import ComplianceError, SanitizationError
from src.core.redact import (
    assert_no_secrets,
    contains_secret,
    is_suspicious_assignment_value,
    redact_json,
    redact_text,
    sanitize_public_payload,
    secret_hits,
)

from helpers import (
    auth_header,
    fake_aws_access_key,
    fake_github_token,
    fake_google_api_key,
    fake_google_oauth_token,
    fake_groq_key,
    fake_jwt,
    fake_openai_key,
    fake_private_key,
    fake_slack_token,
    quoted_assignment,
    unquoted_assignment,
    url_secret,
    url_userinfo,
)

TOKEN_CASES = [
    (fake_github_token, "GITHUB_TOKEN"),
    (fake_groq_key, "GROQ_KEY"),
    (fake_openai_key, "API_KEY"),
    (fake_google_api_key, "GOOGLE_API_KEY"),
    (fake_google_oauth_token, "GOOGLE_OAUTH"),
    (fake_aws_access_key, "AWS_KEY"),
    (fake_slack_token, "SLACK_TOKEN"),
    (fake_jwt, "JWT"),
    (fake_private_key, "PRIVATE_KEY"),
]

PII_CASES = [
    ("ops@example.com", "EMAIL"),
    ("123-45-6789", "SSN"),
    ("4111 1111 1111 1111", "CARD"),
    ("10.0.0.7", "PRIVATE_IP"),
    ("+15551234567", "PHONE"),
    ("C:\\Users\\alice\\notes.txt", "USER"),
    ("/home/alice/notes.txt", "USER"),
]


@pytest.mark.parametrize(("builder", "kind"), TOKEN_CASES)
def test_provider_credentials_are_redacted(
    builder: Callable[[], str], kind: str
) -> None:
    raw = builder()
    result = redact_text(raw)
    assert f"[REDACTED:{kind}]" in result.text
    assert raw not in result.text
    assert result.counts


@pytest.mark.parametrize(("text", "kind"), PII_CASES)
def test_pii_is_redacted(text: str, kind: str) -> None:
    result = redact_text(text)
    assert f"[REDACTED:{kind}]" in result.text
    assert result.counts


def test_authorization_headers_are_redacted() -> None:
    result = redact_text(auth_header())
    assert "[REDACTED:AUTH]" in result.text
    assert "abc123def456" not in result.text


def test_key_value_assignments_are_redacted() -> None:
    unquoted = redact_text(unquoted_assignment())
    assert "[REDACTED:SECRET]" in unquoted.text
    assert unquoted.counts["secret_assignment"] == 1

    quoted = redact_text(quoted_assignment())
    assert '[REDACTED:SECRET]' in quoted.text
    assert quoted.counts["quoted_secret_assignment"] == 1


def test_url_secrets_and_userinfo_are_redacted() -> None:
    secret = redact_text(url_secret())
    assert "[REDACTED:SECRET]" in secret.text
    assert "abc123def456" not in secret.text

    userinfo = redact_text(url_userinfo())
    assert "[REDACTED:USERINFO]@" in userinfo.text


def test_placeholders_and_template_references_survive() -> None:
    examples = (
        "API_" + "KEY=" + "YOUR_" + "KEY_" + "HERE",
        "TOKEN=" + "${{ secrets.GITHUB_TOKEN }}",
        'pass' + 'word = "' + "CHANGE" + "ME" + '"',
    )
    for example in examples:
        assert redact_text(example).text == example


def test_redaction_is_idempotent() -> None:
    once = redact_text(unquoted_assignment() + " contact ops@example.com").text
    twice = redact_text(once).text
    assert once == twice


def test_contains_secret_and_assert_no_secrets() -> None:
    assert contains_secret(fake_groq_key())
    assert not contains_secret("plain descriptive prose")
    with pytest.raises(SanitizationError):
        assert_no_secrets("ops@example.com")
    assert_no_secrets("contact [REDACTED:EMAIL]")


def test_json_redaction_walks_every_level() -> None:
    payload = {
        "password": "hunter" + "2",
        "note": "write to ops@example.com",
        "token": "YOUR_TOKEN_HERE",
        "nested": {"api_key": "abc" + "123def456"},
        "count": 3,
    }
    sanitized, counts = redact_json(payload)
    assert isinstance(sanitized, dict)
    assert sanitized["password"] == "[REDACTED:SECRET_FIELD]"
    assert sanitized["token"] == "YOUR_TOKEN_HERE"
    assert sanitized["count"] == 3
    assert "[REDACTED:EMAIL]" in sanitized["note"]
    nested = sanitized["nested"]
    assert isinstance(nested, dict)
    assert nested["api_key"] == "[REDACTED:SECRET_FIELD]"
    assert counts["secret_field"] == 2
    assert counts["email"] == 1


def test_public_payload_sanitizer_returns_clean_values() -> None:
    sanitized, _ = sanitize_public_payload(
        {"body": "reach me at ops@example.com", "amount_usd": 0.0}
    )
    assert isinstance(sanitized, dict)
    assert "ops@example.com" not in sanitized["body"]
    assert sanitized["amount_usd"] == 0.0


def test_scanner_ignores_clean_text_and_plain_pii() -> None:
    assert scan_text("ordinary documentation text") == []
    assert scan_text("contact ops@example.com") == []
    assert scan_text("the parser reads config/system.yaml") == []


def test_scanner_detects_provider_tokens() -> None:
    findings = scan_text(fake_github_token(), path="example.py")
    assert any(finding.kind == "github_token" for finding in findings)
    assert all(finding.path == "example.py" for finding in findings)


def test_scanner_detects_suspicious_assignments() -> None:
    text = "".join(("REVISION_", "TOKEN=", "abc", "123def456"))
    findings = scan_text(text)
    assert any(finding.kind == "secret_assignment" for finding in findings)


def test_scanner_detects_high_entropy_values() -> None:
    candidate = "abcdefghijklmnopqrstuvwxyz" + "ABCDEF" + "0123456789"
    findings = scan_text("auth " + candidate)
    assert any(finding.kind == "high_entropy" for finding in findings)


def test_scanner_reads_files_and_trees(tmp_path: Path) -> None:
    clean = tmp_path / "clean.txt"
    clean.write_text("nothing to see here\n", encoding="utf-8")
    dirty = tmp_path / "dirty.txt"
    dirty.write_text("creds " + fake_groq_key() + "\n", encoding="utf-8")

    assert scan_file(clean) == []
    assert scan_file(dirty)
    findings = scan_tree(tmp_path)
    assert [finding.path for finding in findings] == ["dirty.txt"]

    with pytest.raises(ComplianceError):
        assert_clean(findings)

    assert_clean([])


def test_scan_findings_never_include_the_secret_value() -> None:
    raw = fake_private_key()
    hits = secret_hits(raw)
    assert hits
    findings = scan_text(raw)
    serialized = " ".join(f"{item.kind}:{item.line}" for item in findings)
    assert "MIIB" not in serialized
    assert raw not in serialized


@pytest.mark.parametrize(
    "value",
    [
        "payment_credentials",
        "request.max_tokens",
        "REQUEST_TOKEN",
        "YOUR_KEY_HERE",
        "true",
        "${{ secrets.X }}",
        "https://example.com/path",
        "abcdef",
    ],
)
def test_assignment_filter_accepts_code_and_references(value: str) -> None:
    assert not is_suspicious_assignment_value(value)


def test_assignment_filter_flags_credential_values() -> None:
    assert is_suspicious_assignment_value("abc123def456")
    assert is_suspicious_assignment_value(fake_groq_key())
    assert is_suspicious_assignment_value("A1b2C3d4E5!")
    assert not is_suspicious_assignment_value("CHANGEME")


def test_secret_finding_serialization_has_no_payload() -> None:
    finding = SecretFinding(path="a.txt", line=2, kind="github_token")
    assert finding.to_json() == {"path": "a.txt", "line": 2, "kind": "github_token"}
