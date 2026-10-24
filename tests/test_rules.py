from __future__ import annotations

from auditor.findings import CATEGORIES, RULE_ID_RE, SEVERITIES
from auditor.prompts import SYSTEM_PROMPT
from auditor.rules import RULES, catalogue_text, is_known_or_custom


def test_catalogue_is_consistent():
    assert len(RULES) >= 40
    for rule_id, rule in RULES.items():
        assert RULE_ID_RE.match(rule_id)
        assert rule_id.startswith(CATEGORIES[rule.category] + "-")
        assert rule.severity in SEVERITIES
        assert rule.title and rule.check
    assert {r.category for r in RULES.values()} == set(CATEGORIES)  # every category covered


def test_known_or_custom_ids():
    assert is_known_or_custom("SEC-001")
    assert is_known_or_custom("CI-901")
    assert not is_known_or_custom("SEC-050")


def test_catalogue_is_in_system_prompt():
    text = catalogue_text()
    assert "SEC-006 [critical] pull_request_target runs untrusted PR code" in text
    assert text in SYSTEM_PROMPT
    # constant prompt: no timestamps or run-specific data (keeps it cacheable)
    assert "{" not in SYSTEM_PROMPT.split("## Rule catalogue")[0].replace("${{", "")
