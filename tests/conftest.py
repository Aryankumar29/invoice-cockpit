import pytest


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch):
    """A key in the developer's shell must never reach a test: tests are free and deterministic."""
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_MODEL", "COCKPIT_LLM"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _no_live_connectors(monkeypatch):
    """Tests read the built-in master data and never reach Slack, a mailbox or Google."""
    for k in ("SLACK_WEBHOOK_URL", "GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_FILE", "COCKPIT_PO_BOOK",
              "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "OUTLOOK_TENANT_ID", "OUTLOOK_CLIENT_ID",
              "OUTLOOK_CLIENT_SECRET", "OUTLOOK_MAILBOX", "COCKPIT_MAILBOX_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("COCKPIT_PO_SOURCE", "seed")
    monkeypatch.setenv("COCKPIT_DIGEST_TIME", "off")
