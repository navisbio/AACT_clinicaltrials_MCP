"""Shared fixtures for integration tests.

These tests hit the live AACT database — they require DB_USER and DB_PASSWORD
to be set in the environment (or in a .env file at the project root).
"""
import json
import os
import pytest
from dotenv import load_dotenv

# Load .env immediately at import time, before any server module is touched.
load_dotenv()

# Skip all integration tests if DB credentials are not configured.
requires_db = pytest.mark.skipif(
    "DB_USER" not in os.environ or "DB_PASSWORD" not in os.environ,
    reason="DB_USER and DB_PASSWORD required (set in .env or environment)",
)


def pytest_collection_modifyitems(items):
    """Auto-apply the requires_db marker to all tests requesting the 'client' fixture."""
    for item in items:
        if "client" in getattr(item, "fixturenames", ()):
            item.add_marker(requires_db)


@pytest.fixture
async def client():
    """Create an in-memory MCP client connected to the AACT server.

    Note: pytest-asyncio tears down async generator fixtures in a different
    task than the one that created them, which triggers a RuntimeError from
    anyio's cancel scope checks. We catch this at teardown since the server
    task is properly cancelled regardless.
    """
    from mcp.client import Client
    from src.server import mcp as server

    try:
        async with Client(server, raise_exceptions=True) as session:
            yield session
    except (RuntimeError, BaseExceptionGroup):
        # Known pytest-asyncio + anyio teardown incompatibility:
        # "Attempted to exit cancel scope in a different task than it was entered in"
        pass


def parse_tool_result(result) -> list[dict]:
    """Parse a CallToolResult into a list of dicts from its text content.

    Skips non-JSON content blocks (e.g. grounding notices).
    """
    texts = [block.text for block in result.content if hasattr(block, "text")]
    parsed = []
    for text in texts:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue  # Skip grounding notices and other non-JSON content
        if isinstance(data, list):
            parsed.extend(data)
        else:
            parsed.append(data)
    return parsed
