"""The desktop manifest has to satisfy the directory review."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_declares_privacy_policy_and_credentials() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())

    policies = manifest["privacy_policies"]
    assert policies == [
        "https://github.com/navisbio/AACT_clinicaltrials_MCP/blob/main/PRIVACY.md"
    ]
    assert (ROOT / "PRIVACY.md").is_file()
    privacy = (ROOT / "PRIVACY.md").read_text()
    assert "aact-db.ctti-clinicaltrials.org" in privacy
    assert "external database" in privacy.lower()

    user_config = manifest["user_config"]
    assert user_config["db_user"]["required"] is True
    assert "DB_USER" in user_config["db_user"]["description"]
    assert user_config["db_password"]["required"] is True
    assert user_config["db_password"]["sensitive"] is True
    assert "DB_PASSWORD" in user_config["db_password"]["description"]

    env = manifest["server"]["mcp_config"]["env"]
    assert env["DB_USER"] == "${user_config.db_user}"
    assert env["DB_PASSWORD"] == "${user_config.db_password}"

    assert manifest["repository"]["url"] == (
        "https://github.com/navisbio/AACT_clinicaltrials_MCP"
    )
