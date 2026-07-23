"""Regression tests for #789 — curation JSON whose shape doesn't match the
`{"result": "<text>"}` contract must degrade to `return False`, not raise
`AttributeError`.
"""

from unittest.mock import AsyncMock, patch

import pytest

from router.memory_curator import MARKER_FILENAME, curate_agent_memory

pytestmark = pytest.mark.unit


class TestNonDictCurationJson:
    """Malformed curation output must never crash curate_agent_memory."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stdout", ["42", "[1,2]", '{"result": 42}', "null"])
    async def test_non_dict_curation_json_returns_false(self, tmp_path, stdout):
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("entry")

        with patch("router.memory_curator._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (stdout, "", 0)
            result = await curate_agent_memory("lisa", "lisa", str(agent_base))

        assert result is False
        assert not (memory_dir / MARKER_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_valid_result_still_writes(self, tmp_path):
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("entry")

        with patch("router.memory_curator._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ('{"result": "## Notes\\ncurated content"}', "", 0)
            result = await curate_agent_memory("lisa", "lisa", str(agent_base))

        assert result is True
        assert (memory_dir / MARKER_FILENAME).exists()
        assert "curated content" in (memory_dir / "memory.md").read_text()
