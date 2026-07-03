"""Unit tests for router.memory_writer — atomic writes, file creation, and append logic.

These tests define the interface that router/memory_writer.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

memory_writer = pytest.importorskip("router.memory_writer", reason="router.memory_writer not yet implemented")

pytestmark = pytest.mark.unit


class TestAtomicWrite:
    """Tests for atomic file writing."""

    def test_write_memory_creates_file(self, tmp_path):
        """write_memory() should create a new file with the given content."""
        target = tmp_path / "new_memory.md"
        memory_writer.write_memory(target, "# New Memory\nSome content.")
        assert target.exists()
        assert target.read_text() == "# New Memory\nSome content."

    def test_write_memory_overwrites_existing(self, tmp_path):
        """write_memory() should overwrite existing file content."""
        target = tmp_path / "existing.md"
        target.write_text("Old content")
        memory_writer.write_memory(target, "New content")
        assert target.read_text() == "New content"

    def test_write_memory_is_atomic(self, tmp_path):
        """Write should be atomic — file should not be partially written on failure.

        This test verifies the interface accepts the path and content.
        Actual atomicity testing requires integration-level testing.
        """
        target = tmp_path / "atomic_test.md"
        memory_writer.write_memory(target, "Complete content")
        assert target.read_text() == "Complete content"


class TestAppendLogic:
    """Tests for appending to memory files."""

    def test_append_memory_adds_content(self, tmp_path):
        """append_memory() should add content to the end of an existing file."""
        target = tmp_path / "append_test.md"
        target.write_text("# Memory\nExisting content.\n")
        memory_writer.append_memory(target, "\n## New Section\nAppended content.")
        content = target.read_text()
        assert "Existing content" in content
        assert "Appended content" in content

    def test_append_to_nonexistent_creates_file(self, tmp_path):
        """append_memory() on a nonexistent file should create it."""
        target = tmp_path / "new_append.md"
        memory_writer.append_memory(target, "# Fresh Content")
        assert target.exists()
        assert "Fresh Content" in target.read_text()


class TestDirectoryCreation:
    """Tests for automatic directory creation."""

    def test_write_creates_parent_directories(self, tmp_path):
        """write_memory() should create parent directories if they don't exist."""
        target = tmp_path / "agents" / "lisa" / "memory.md"
        memory_writer.write_memory(target, "# Lisa Memory")
        assert target.exists()
        assert target.read_text() == "# Lisa Memory"

    def test_append_creates_parent_directories(self, tmp_path):
        """append_memory() should create parent directories if they don't exist."""
        target = tmp_path / "agents" / "new_agent" / "memory.md"
        memory_writer.append_memory(target, "# New Agent Memory")
        assert target.exists()


class TestPermissions:
    """Tests for the 0600/0700 modes enforced for issue #116."""

    def test_write_memory_file_mode_is_0600(self, tmp_path):
        """Written memory files must be 0600 — other host users mustn't read them."""
        import stat

        target = tmp_path / "memory.md"
        memory_writer.write_memory(target, "secret notes")
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_write_memory_parent_dir_mode_is_0700(self, tmp_path):
        """Auto-created parent dirs must be 0700 so the tree isn't browseable."""
        import stat

        target = tmp_path / "agents" / "lisa" / "memory" / "memory.md"
        memory_writer.write_memory(target, "content")
        mode = stat.S_IMODE(target.parent.stat().st_mode)
        assert mode == 0o700, f"expected 0700, got {oct(mode)}"

    def test_append_memory_file_mode_is_0600(self, tmp_path):
        """Files created via append_memory must also be 0600."""
        import stat

        target = tmp_path / "memory.md"
        memory_writer.append_memory(target, "content")
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600

    @pytest.mark.asyncio
    async def test_persist_memory_files_are_0600(self, tmp_path):
        """Every file path persist_memory produces must end up 0600."""
        import datetime
        import stat

        agent_base = tmp_path / "agents"
        updates = {
            "decisions": [{"date": "2026-04-14", "topic": "T", "content": "C"}],
            "preferences": [{"date": "2026-04-14", "content": "P"}],
            "people": [{"name": "Bram", "context": "founder"}],
            "projects": [{"name": "Auth", "update": "shipped"}],
            "agent_memory": "note",
            "daily_log": "log",
        }
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        today = datetime.date.today().isoformat()
        files = [
            agent_base / "lisa" / "memory" / "decisions" / "2026-04-14.md",
            agent_base / "lisa" / "memory" / "preferences" / "preferences.md",
            agent_base / "lisa" / "memory" / "people" / "bram.md",
            agent_base / "lisa" / "memory" / "projects" / "auth.md",
            agent_base / "lisa" / "memory" / "memory.md",
            agent_base / "lisa" / "memory" / "daily" / f"{today}.md",
        ]
        for f in files:
            assert f.exists(), f"{f} missing"
            mode = stat.S_IMODE(f.stat().st_mode)
            assert mode == 0o600, f"{f} mode {oct(mode)} != 0o600"


class TestWriteMemoryErrorHandling:
    """Tests for error handling during atomic writes."""

    def test_write_memory_cleans_up_on_error(self, tmp_path, monkeypatch):
        """If os.rename fails, temp file should be cleaned up."""
        import os

        target = tmp_path / "fail_test.md"

        def failing_rename(src, dst):
            raise OSError("rename failed")

        monkeypatch.setattr(os, "rename", failing_rename)

        with pytest.raises(OSError, match="rename failed"):
            memory_writer.write_memory(target, "content")

        # Temp files should be cleaned up
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


class TestPersistMemory:
    """Tests for the persist_memory function."""

    @pytest.mark.asyncio
    async def test_persist_decisions(self, tmp_path):
        """Should persist decision entries to dated files."""
        agent_base = tmp_path / "agents"

        updates = {
            "decisions": [
                {"date": "2024-01-20", "topic": "Auth approach", "content": "Use OAuth2"},
            ],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        decision_file = agent_base / "lisa" / "memory" / "decisions" / "2024-01-20.md"
        assert decision_file.exists()
        content = decision_file.read_text()
        assert "Auth approach" in content
        assert "OAuth2" in content

    @pytest.mark.asyncio
    async def test_persist_preferences(self, tmp_path):
        """Should persist preference entries."""
        agent_base = tmp_path / "agents"

        updates = {
            "preferences": [
                {"date": "2024-01-20", "content": "Prefers short summaries"},
            ],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        pref_file = agent_base / "lisa" / "memory" / "preferences" / "preferences.md"
        assert pref_file.exists()
        assert "short summaries" in pref_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_people(self, tmp_path):
        """Should persist people entries to name-based files."""
        agent_base = tmp_path / "agents"

        updates = {
            "people": [
                {"name": "John Doe", "context": "Backend engineer"},
            ],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        person_file = agent_base / "lisa" / "memory" / "people" / "john-doe.md"
        assert person_file.exists()
        assert "Backend engineer" in person_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_projects(self, tmp_path):
        """Should persist project updates to name-based files."""
        agent_base = tmp_path / "agents"

        updates = {
            "projects": [
                {"name": "Auth Module", "update": "Added rate limiting"},
            ],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        project_file = agent_base / "lisa" / "memory" / "projects" / "auth-module.md"
        assert project_file.exists()
        assert "rate limiting" in project_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_agent_memory(self, tmp_path):
        """Should append to agent's memory.md."""
        agent_base = tmp_path / "agents"

        updates = {"agent_memory": "Learned about the auth system."}
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        agent_memory_file = agent_base / "lisa" / "memory" / "memory.md"
        assert agent_memory_file.exists()
        assert "auth system" in agent_memory_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_daily_log(self, tmp_path):
        """Should append to daily log file."""
        import datetime

        agent_base = tmp_path / "agents"

        updates = {"daily_log": "Reviewed 3 PRs today."}
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        today = datetime.date.today().isoformat()
        log_file = agent_base / "lisa" / "memory" / "daily" / f"{today}.md"
        assert log_file.exists()
        assert "3 PRs" in log_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_empty_updates(self, tmp_path):
        """Empty updates dict should persist nothing."""
        agent_base = tmp_path / "agents"
        count = await memory_writer.persist_memory("lisa", {}, str(agent_base))
        assert count == 0

    @pytest.mark.asyncio
    async def test_persist_multiple_categories(self, tmp_path):
        """Should handle multiple categories in one call."""
        agent_base = tmp_path / "agents"

        updates = {
            "decisions": [{"date": "2024-01-20", "topic": "DB", "content": "Use Postgres"}],
            "agent_memory": "Decided on Postgres.",
            "daily_log": "DB decision made.",
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 3

    @pytest.mark.asyncio
    async def test_persist_uses_today_as_default_date(self, tmp_path):
        """Decisions without a date should use today's date."""
        import datetime

        agent_base = tmp_path / "agents"

        updates = {"decisions": [{"topic": "Test", "content": "Something"}]}
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        today = datetime.date.today().isoformat()
        assert (agent_base / "lisa" / "memory" / "decisions" / f"{today}.md").exists()

    @pytest.mark.asyncio
    async def test_persist_empty_agent_memory_skipped(self, tmp_path):
        """Empty agent_memory string should not count as persisted."""
        agent_base = tmp_path / "agents"
        count = await memory_writer.persist_memory("lisa", {"agent_memory": ""}, str(agent_base))
        assert count == 0

    @pytest.mark.asyncio
    async def test_persist_empty_daily_log_skipped(self, tmp_path):
        """Empty daily_log string should not count as persisted."""
        agent_base = tmp_path / "agents"
        count = await memory_writer.persist_memory("lisa", {"daily_log": ""}, str(agent_base))
        assert count == 0


class TestDecisionDateNormalization:
    """Regression tests for issue #488 — non-ISO decision dates must still produce ISO filenames."""

    def _decisions_dir(self, agent_base, agent="lisa"):
        return agent_base / agent / "memory" / "decisions"

    def _iso_stem(self, path):
        """Assert and return the ISO-parsed stem, raising AssertionError if invalid."""
        import datetime

        try:
            return datetime.date.fromisoformat(path.stem)
        except ValueError:
            raise AssertionError(f"File stem {path.stem!r} is not a valid ISO date") from None

    @pytest.mark.asyncio
    async def test_long_form_date_normalised(self, tmp_path):
        """'June 19, 2026' must produce 2026-06-19.md, readable by the curator."""
        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "June 19, 2026", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        files = list(self._decisions_dir(agent_base).glob("*.md"))
        assert len(files) == 1
        self._iso_stem(files[0])
        assert files[0].stem == "2026-06-19"

    @pytest.mark.asyncio
    async def test_slash_separated_date_normalised(self, tmp_path):
        """'2026/06/19' must produce 2026-06-19.md and not escape decisions/ via path sep."""
        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "2026/06/19", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        decisions_dir = self._decisions_dir(agent_base)
        files = list(decisions_dir.glob("*.md"))
        assert len(files) == 1, f"Expected 1 file in decisions/, got: {[f.name for f in files]}"
        self._iso_stem(files[0])
        assert files[0].stem == "2026-06-19"
        # File must sit directly inside decisions/, not a subdirectory
        assert files[0].parent == decisions_dir

    @pytest.mark.asyncio
    async def test_datetime_string_normalised(self, tmp_path):
        """A date+time string like '2026-06-19T14:30:00' must produce 2026-06-19.md."""
        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "2026-06-19T14:30:00", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        files = list(self._decisions_dir(agent_base).glob("*.md"))
        assert len(files) == 1
        self._iso_stem(files[0])
        assert files[0].stem == "2026-06-19"

    @pytest.mark.asyncio
    async def test_empty_date_falls_back_to_today(self, tmp_path):
        """An empty date string must fall back to today and produce a valid ISO filename."""
        import datetime

        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        files = list(self._decisions_dir(agent_base).glob("*.md"))
        assert len(files) == 1
        parsed = self._iso_stem(files[0])
        assert parsed == datetime.date.today()

    @pytest.mark.asyncio
    async def test_unparseable_date_falls_back_to_today(self, tmp_path):
        """A date string that cannot be parsed at all must fall back to today."""
        import datetime

        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "not-a-date", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        files = list(self._decisions_dir(agent_base).glob("*.md"))
        assert len(files) == 1
        parsed = self._iso_stem(files[0])
        assert parsed == datetime.date.today()

    @pytest.mark.asyncio
    async def test_normalised_file_is_curator_visible(self, tmp_path):
        """Every written decision file stem must parse via datetime.date.fromisoformat."""
        import datetime

        agent_base = tmp_path / "agents"
        bad_dates = ["June 19, 2026", "2026/06/19", "2026-06-19T12:00:00", "", "??"]
        updates = {"decisions": [{"date": d, "topic": "T", "content": "C"} for d in bad_dates]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        for f in self._decisions_dir(agent_base).glob("*.md"):
            # This is the exact check in memory_curator._read_new_dated_files; it must not raise.
            datetime.date.fromisoformat(f.stem)


class TestPersistMemoryNonDictItems:
    """Regression tests for issue #523 — list-of-strings must not crash persist_memory."""

    @pytest.mark.asyncio
    async def test_decisions_list_of_strings_no_exception(self, tmp_path):
        """decisions as list of strings must not raise AttributeError; well-formed fields persist."""
        agent_base = tmp_path / "agents"
        updates = {
            "decisions": ["we decided to ship X", "another decision"],
            "agent_memory": "some note",
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        # Strings skipped (0 decisions), agent_memory persisted (1)
        assert count == 1
        memory_md = agent_base / "lisa" / "memory" / "memory.md"
        assert memory_md.exists()
        assert "some note" in memory_md.read_text()

    @pytest.mark.asyncio
    async def test_later_malformed_field_does_not_drop_earlier(self, tmp_path):
        """A malformed later field (people=strings) must not abort earlier fields (decisions)."""
        agent_base = tmp_path / "agents"
        updates = {
            "decisions": [{"date": "2024-01-20", "topic": "Auth", "content": "Use OAuth2"}],
            "people": ["Alice", "Bob"],  # list of strings — must be skipped, not crash
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        # decisions persisted (1), people strings skipped (0)
        assert count == 1
        decision_file = agent_base / "lisa" / "memory" / "decisions" / "2024-01-20.md"
        assert decision_file.exists()
        assert "OAuth2" in decision_file.read_text()

    @pytest.mark.asyncio
    async def test_all_list_fields_as_strings_zero_count_no_exception(self, tmp_path):
        """All four list fields as lists of strings produce count=0 and no exception."""
        agent_base = tmp_path / "agents"
        updates = {
            "decisions": ["decision string"],
            "preferences": ["pref string"],
            "people": ["person string"],
            "projects": ["project string"],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 0

    @pytest.mark.asyncio
    async def test_mixed_dict_and_string_in_decisions(self, tmp_path):
        """decisions list with both dicts and strings: dicts persist, strings are skipped."""
        agent_base = tmp_path / "agents"
        updates = {
            "decisions": [
                "bare string decision",
                {"date": "2024-01-20", "topic": "Auth", "content": "Use OAuth2"},
            ],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        decision_file = agent_base / "lisa" / "memory" / "decisions" / "2024-01-20.md"
        assert decision_file.exists()
        assert "OAuth2" in decision_file.read_text()

    @pytest.mark.asyncio
    async def test_early_field_malformed_does_not_drop_agent_memory_and_daily_log(self, tmp_path):
        """Malformed decisions (strings) must not abort agent_memory and daily_log writes."""
        agent_base = tmp_path / "agents"
        updates = {
            "decisions": ["string not a dict"],
            "agent_memory": "important note",
            "daily_log": "log entry",
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        # decisions strings skipped (0), agent_memory (1), daily_log (1)
        assert count == 2
        memory_md = agent_base / "lisa" / "memory" / "memory.md"
        assert memory_md.exists()
        assert "important note" in memory_md.read_text()


class TestConcurrentSafety:
    """Regression tests for issue #516 — concurrent append_memory must not drop entries."""

    def test_concurrent_appends_no_data_loss(self, tmp_path):
        """N concurrent threads each calling append_memory must all survive (no last-writer-wins drop)."""
        import threading

        target = tmp_path / "concurrent.md"
        N = 30
        errors = []

        def do_append(i):
            try:
                memory_writer.append_memory(target, f"entry-{i}\n")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_append, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threads raised: {errors}"
        content = target.read_text()
        missing = [i for i in range(N) if f"entry-{i}" not in content]
        assert not missing, f"Entries lost by concurrent appends: {missing}"

    def test_concurrent_append_and_write_are_mutually_exclusive(self, tmp_path):
        """A concurrent write_memory must not clobber an in-progress append_memory."""
        import threading

        target = tmp_path / "race.md"
        target.write_text("base\n")

        barrier = threading.Barrier(2)

        def appender():
            barrier.wait()
            memory_writer.append_memory(target, "appended\n")

        def writer():
            barrier.wait()
            memory_writer.write_memory(target, "overwrite\n")

        t1 = threading.Thread(target=appender)
        t2 = threading.Thread(target=writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        content = target.read_text()
        # One of the two must have won cleanly — the file must contain either:
        #   (a) "overwrite\n"  (writer ran last)
        #   (b) "base\nappended\n" + possibly "overwrite" depending on ordering
        # The important guarantee: the file must be non-empty and valid (no torn write).
        assert content, "File must not be empty after concurrent append+write"
        assert "\x00" not in content, "No NUL bytes — torn write detected"


class TestNameSanitization:
    """Regression tests for issue #519 — LLM-supplied names used as filenames must be sanitized."""

    def test_sanitize_name_slash(self):
        """A slash in the name must be replaced, not treated as a path separator."""
        result = memory_writer._sanitize_name("bramveen1/ai-dev-team")
        assert "/" not in result
        assert "\\" not in result
        assert result == "bramveen1-ai-dev-team"

    def test_sanitize_name_dotdot(self):
        """A '..' name must not survive sanitization as a path traversal segment."""
        result = memory_writer._sanitize_name("../memory")
        assert ".." not in result
        assert "/" not in result

    def test_sanitize_name_empty_falls_back_to_unknown(self):
        """An empty name must fall back to 'unknown'."""
        assert memory_writer._sanitize_name("") == "unknown"

    def test_sanitize_name_all_separators_falls_back_to_unknown(self):
        """A name composed entirely of path separators must fall back to 'unknown'."""
        assert memory_writer._sanitize_name("///") == "unknown"
        assert memory_writer._sanitize_name("..") == "unknown"

    def test_sanitize_name_spaces_become_hyphens(self):
        """Spaces should be replaced with hyphens (existing behaviour preserved)."""
        assert memory_writer._sanitize_name("John Doe") == "john-doe"

    @pytest.mark.asyncio
    async def test_persist_path_traversal_person_name(self, tmp_path):
        """A person named '../memory' must not write outside people/ or touch memory.md."""
        agent_base = tmp_path / "agents"
        memory_md = agent_base / "lisa" / "memory" / "memory.md"

        updates = {"people": [{"name": "../memory", "context": "attacker"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        # memory/memory.md must not have been created or touched
        assert not memory_md.exists(), "memory.md must not be written by a people entry"

        # All files must sit directly under people/ (no nested subdirectory)
        people_dir = agent_base / "lisa" / "memory" / "people"
        for f in people_dir.rglob("*"):
            if f.is_file():
                assert f.parent == people_dir, f"File escaped people/: {f}"

    @pytest.mark.asyncio
    async def test_persist_slash_in_project_name_stays_flat(self, tmp_path):
        """A project named 'bramveen1/ai-dev-team' must be a single flat .md file under projects/."""
        agent_base = tmp_path / "agents"
        updates = {"projects": [{"name": "bramveen1/ai-dev-team", "update": "shipped"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        projects_dir = agent_base / "lisa" / "memory" / "projects"
        # Must be exactly one file, directly in projects/ (not a nested path)
        files = list(projects_dir.rglob("*.md"))
        assert len(files) == 1, f"Expected 1 file, got: {[str(f) for f in files]}"
        assert files[0].parent == projects_dir, f"File not in projects/ flat: {files[0]}"
        # The curator glob projects/*.md must find it
        glob_files = list(projects_dir.glob("*.md"))
        assert len(glob_files) == 1

    @pytest.mark.asyncio
    async def test_persist_traversal_and_slash_combined(self, tmp_path):
        """Regression: both a traversal person name and a slash project name in one call."""
        agent_base = tmp_path / "agents"
        memory_md = agent_base / "lisa" / "memory" / "memory.md"

        updates = {
            "people": [{"name": "../memory", "context": "attacker"}],
            "projects": [{"name": "bramveen1/ai-dev-team", "update": "work"}],
        }
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        # memory.md must not exist (not clobbered by people entry)
        assert not memory_md.exists()

        # Project file must be a single flat file under projects/
        projects_dir = agent_base / "lisa" / "memory" / "projects"
        glob_files = list(projects_dir.glob("*.md"))
        assert len(glob_files) == 1

        # No file anywhere outside its intended leaf directory
        people_dir = agent_base / "lisa" / "memory" / "people"
        for f in people_dir.rglob("*"):
            if f.is_file():
                assert f.parent == people_dir
        for f in projects_dir.rglob("*"):
            if f.is_file():
                assert f.parent == projects_dir


class TestSystemsCategory:
    """persist_memory systems/ category (#640)."""

    @pytest.mark.asyncio
    async def test_systems_update_written_to_systems_dir(self, tmp_path):
        updates = {"systems": [{"name": "Merge Queue", "update": "Serialises PR merges per repo."}]}
        count = await memory_writer.persist_memory("lisa", updates, agent_base=str(tmp_path))
        assert count == 1
        target = tmp_path / "lisa" / "memory" / "systems" / "merge-queue.md"
        assert target.exists()
        assert "Serialises PR merges" in target.read_text()

    @pytest.mark.asyncio
    async def test_non_dict_system_item_skipped(self, tmp_path):
        updates = {"systems": ["not a dict", {"name": "router", "update": "ok"}]}
        count = await memory_writer.persist_memory("lisa", updates, agent_base=str(tmp_path))
        assert count == 1


class TestCanonicalIdentity:
    """persist_memory merges name variants into the canonical file (#640)."""

    @pytest.fixture
    def alias_map_file(self, tmp_path, monkeypatch):
        import json

        path = tmp_path / "memory-aliases.json"
        path.write_text(
            json.dumps(
                {
                    "people": {"bram": ["Bram Veenhof", "bramveen1"]},
                    "projects": {"ai-dev-team": ["ai dev team router"]},
                }
            )
        )
        monkeypatch.setenv("MEMORY_ALIAS_MAP_PATH", str(path))
        return path

    @pytest.mark.asyncio
    async def test_person_variant_merges_into_canonical_file(self, tmp_path, alias_map_file):
        updates = {"people": [{"name": "Bram Veenhof", "context": "likes short PRs"}]}
        await memory_writer.persist_memory("lisa", updates, agent_base=str(tmp_path))
        people = tmp_path / "lisa" / "memory" / "people"
        assert (people / "bram.md").exists()
        assert not (people / "bram-veenhof.md").exists()

    @pytest.mark.asyncio
    async def test_two_variants_land_in_one_file(self, tmp_path, alias_map_file):
        await memory_writer.persist_memory(
            "lisa", {"people": [{"name": "Bram Veenhof", "context": "first"}]}, agent_base=str(tmp_path)
        )
        await memory_writer.persist_memory(
            "lisa", {"people": [{"name": "bramveen1", "context": "second"}]}, agent_base=str(tmp_path)
        )
        content = (tmp_path / "lisa" / "memory" / "people" / "bram.md").read_text()
        assert "first" in content and "second" in content

    @pytest.mark.asyncio
    async def test_unknown_person_gets_own_file(self, tmp_path, alias_map_file):
        updates = {"people": [{"name": "Jane Doe", "context": "new contact"}]}
        await memory_writer.persist_memory("lisa", updates, agent_base=str(tmp_path))
        assert (tmp_path / "lisa" / "memory" / "people" / "jane-doe.md").exists()

    @pytest.mark.asyncio
    async def test_no_alias_map_behaves_like_sanitization(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_ALIAS_MAP_PATH", str(tmp_path / "missing.json"))
        updates = {"people": [{"name": "Bram Veenhof", "context": "x"}]}
        await memory_writer.persist_memory("lisa", updates, agent_base=str(tmp_path))
        assert (tmp_path / "lisa" / "memory" / "people" / "bram-veenhof.md").exists()


class TestIndexRebuildOnPersist:
    """persist_memory regenerates the manifest index (#640)."""

    @pytest.mark.asyncio
    async def test_manifest_written_after_persist(self, tmp_path):
        updates = {"projects": [{"name": "ai-dev-team", "update": "shipped retrieval"}]}
        await memory_writer.persist_memory("lisa", updates, agent_base=str(tmp_path))
        import json

        manifest = json.loads((tmp_path / "lisa" / "memory" / "manifest.json").read_text())
        assert any(e["path"] == "projects/ai-dev-team.md" for e in manifest["files"])

    @pytest.mark.asyncio
    async def test_empty_updates_do_not_create_manifest(self, tmp_path):
        await memory_writer.persist_memory("lisa", {}, agent_base=str(tmp_path))
        assert not (tmp_path / "lisa" / "memory" / "manifest.json").exists()
