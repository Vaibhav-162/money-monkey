from pathlib import Path


def test_verify_outcomes_has_no_github_schedule() -> None:
    text = Path(".github/workflows/verify_outcomes.yml").read_text(encoding="utf-8")
    assert "schedule:" not in text
    assert 'cron: "15 4 * * 1-5"' not in text
    assert "repository_dispatch:" in text
    assert "types: [trigger-verify-outcomes]" in text
    assert "workflow_dispatch:" in text
    assert "dry_run:" not in text
