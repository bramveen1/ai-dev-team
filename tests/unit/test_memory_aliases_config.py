"""Unit tests for the curated config.example/shared/memory-aliases.json (#716).

Every alias must be receipted (a real login/id/email seen in code or config) —
these tests pin the confirmed bram/sam identities and guard against baking in
the uncertain worker/bot slugs the issue explicitly flags for human review.
"""

import json
from pathlib import Path

import pytest

from router.memory_identity import AliasMap

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
ALIAS_MAP_PATH = REPO_ROOT / "config.example" / "shared" / "memory-aliases.json"

# Single-token uncertain bot/worker slugs #716 flags for Bram to confirm. These
# sanitize to single-hyphen slugs, miss the alias map, and correctly keep their
# own file. NOTE: double-hyphen compound stems (e.g. "sam--bot") are NOT covered
# here — the #715 field-splitter still merges those into the person slug,
# bypassing the alias map. That resolver gap is tracked separately in #730; do
# not add compound stems here until #730 decides the merge rule.
UNCERTAIN_SAM_SLUGS = ("dev-sam", "aidt-sam", "sam-bot", "sam-ai-bot")


@pytest.fixture
def alias_map():
    raw = json.loads(ALIAS_MAP_PATH.read_text(encoding="utf-8"))
    return AliasMap(raw)


class TestBramAliases:
    def test_known_variants_resolve_to_bram(self, alias_map):
        for variant in ("Bram Veenhof", "bramveen1", "bramveenhof@gmail.com", "U0AHCJEHVNJ"):
            assert alias_map.resolve("people", variant) == "bram"


class TestSamAliases:
    def test_confirmed_pr_review_identity_resolves_to_sam(self, alias_map):
        """aidt-tl-sam is the login pr_review's gh api check verifies (config/dispatch.yaml)."""
        assert alias_map.resolve("people", "aidt-tl-sam") == "sam"

    def test_canonical_name_resolves_to_sam(self, alias_map):
        assert alias_map.resolve("people", "Sam") == "sam"

    @pytest.mark.parametrize("slug", UNCERTAIN_SAM_SLUGS)
    def test_uncertain_worker_bot_slugs_not_merged(self, alias_map, slug):
        """Flagged in issue #716 for Bram to confirm — must stay unmerged until then."""
        assert alias_map.resolve("people", slug) != "sam"


class TestGenericSlugsNeverAliasToPerson:
    def test_generic_user_slug_keeps_its_own_file(self, alias_map):
        assert alias_map.resolve("people", "user-12345") == "user-12345"
