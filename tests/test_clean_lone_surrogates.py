"""
Tests for lone-surrogate sanitisation in MCP server tool handlers (issue #1235).

Covers:
- Unit: _clean() helper edge cases (lone surrogates, valid pairs, empty, etc.)
- Integration: all 5 tool paths that call _clean() receive surrogate payloads

Note on U+FFFD replacement count
----------------------------------
``str.encode('utf-8', 'surrogatepass')`` encodes each lone surrogate into the
3-byte CESU-8 sequence (0xED 0xAx 0xBx).  When that byte sequence is decoded
with ``errors='replace'``, *each individual invalid byte* is swapped for one
U+FFFD, yielding **3** replacement characters per lone surrogate.  Tests must
assert ``"\ufffd" * 3`` (i.e. ``\ufffd\ufffd\ufffd``) for every lone surrogate
that was stripped, not a single ``\ufffd``.

    Note on surrogate pairs in Python source
------------------------------------------
Writing ``"\\ud83d\\ude00"`` in Python source code creates a string with *two*
independent lone-surrogate code points (U+D83D and U+DE00).  Python 3 does not
silently merge them into the astral emoji U+1F600.  Therefore these strings
are treated as lone surrogates by ``_clean()`` and are replaced, just like any
other lone surrogate.  To embed a real emoji use the ``\\U`` escape:
``"\\U0001f600"`` (one code point, no surrogates).
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

from mempalace.mcp_server import _clean


# ── ONNX model cache fix ───────────────────────────────────────────────────
# conftest.py redirects HOME to a temp directory *at module level*, before any
# chromadb import, so that mempalace module-level initialisations don't touch
# the real user profile.  Unfortunately this causes chromadb's ONNXMiniLM_L6_V2
# to compute its DOWNLOAD_PATH (``Path.home() / ".cache" / "chroma" / ...``)
# pointing at the temp dir, triggering a 79 MB model download on every run.
#
# Fix: at module import time (i.e. right now, before any fixture), retrieve the
# *original* USERPROFILE that conftest saved in ``_original_env``, and use it
# to patch DOWNLOAD_PATH back to the real user cache.  The patch is applied at
# module level so it takes effect before the conftest ``collection`` fixture
# creates a ChromaDB client.

def _get_real_chroma_cache() -> Path:
    """Return the real user's chroma ONNX model cache path.

    conftest.py stores the original env in ``_original_env`` before
    redirecting HOME.  We reach into that dict to recover the real
    USERPROFILE / HOME so we can locate the pre-downloaded model.
    Falls back to the current HOME if conftest is not loaded yet.
    """
    conftest = sys.modules.get("conftest")
    if conftest is not None:
        orig_env = getattr(conftest, "_original_env", {})
        real_home = (
            orig_env.get("USERPROFILE")
            or orig_env.get("HOME")
        )
        if real_home:
            return Path(real_home) / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"
    # Fallback: use current env (works when conftest doesn't redirect HOME)
    return Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"


def _patch_onnx_download_path() -> None:
    """Patch ONNXMiniLM_L6_V2.DOWNLOAD_PATH to the real user cache (once).

    Called at module import time so the patch is in place before any ChromaDB
    collection is created inside a fixture.
    """
    try:
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    except ImportError:
        return
    real_cache = _get_real_chroma_cache()
    if real_cache.exists():
        ONNXMiniLM_L6_V2.DOWNLOAD_PATH = real_cache


_patch_onnx_download_path()


# ── Unit tests for _clean() ────────────────────────────────────────────────

class TestCleanLoneSurrogates:
    """Unit tests for the _clean() helper."""

    def test_clean_passthrough_normal(self):
        """Normal ASCII + CJK strings pass through unchanged."""
        assert _clean("hello world") == "hello world"
        assert _clean("你好世界") == "你好世界"
        assert _clean("mixed 中 English 文") == "mixed 中 English 文"

    def test_clean_removes_high_surrogate(self):
        """U+DC00–U+DFFF lone surrogates are replaced.

        Each lone surrogate encodes to 3 CESU-8 bytes; decode('replace') then
        emits one U+FFFD per invalid byte, giving 3 replacement chars.
        """
        # one lone surrogate → 3 × U+FFFD
        assert _clean("hello\udc95world") == "hello\ufffd\ufffd\ufffdworld"
        # three lone surrogates → 9 × U+FFFD
        assert _clean("\udcff\udc00\udcaf") == "\ufffd" * 9

    def test_clean_removes_low_surrogate(self):
        """U+D800–U+DBFF lone surrogates are replaced."""
        assert _clean("test\ud800more") == "test\ufffd\ufffd\ufffdmore"
        assert _clean("\ud800\udbff") == "\ufffd" * 6

    def test_clean_removes_multiple_surrogates(self):
        """Multiple surrogates at different positions are all replaced."""
        result = _clean("a\udca1b\udcffc")
        # each surrogate → 3 U+FFFD
        assert result == "a" + "\ufffd" * 3 + "b" + "\ufffd" * 3 + "c"
        assert "\ufffd" in result

    def test_clean_preserves_real_emoji(self):
        """Real emoji (astral code points via \\U escape) pass through unchanged.

        Note: ``\\ud83d\\ude00`` written in Python source is NOT the emoji — it
        is two lone surrogates.  Use ``\\U0001f600`` for the real grinning face.
        """
        # Grinning face U+1F600
        assert _clean("\U0001f600") == "\U0001f600"
        # Rocket U+1F680
        assert _clean("\U0001f680") == "\U0001f680"
        # Emoji in sentence
        assert _clean("hello \U0001f600 world") == "hello \U0001f600 world"

    def test_clean_emoji_with_trailing_lone_surrogate(self):
        """Real emoji followed by a lone surrogate: emoji preserved, lone replaced."""
        # U+1F600 is one code point (no surrogates); \udc95 is a lone surrogate → 3 U+FFFD
        result = _clean("\U0001f600\udc95")
        assert result == "\U0001f600" + "\ufffd" * 3
        assert "\U0001f600" in result
        assert "\ufffd" in result

    def test_clean_emoji_with_leading_lone_surrogate(self):
        """Real emoji preceded by a lone surrogate: lone replaced, emoji preserved."""
        result = _clean("\udc95\U0001f600")
        assert result == "\ufffd" * 3 + "\U0001f600"
        assert "\U0001f600" in result

    def test_clean_empty_string(self):
        """Empty string is returned unchanged, not crashed."""
        assert _clean("") == ""

    def test_clean_only_surrogates(self):
        """String containing only lone surrogates becomes all U+FFFD (3 each)."""
        assert _clean("\udc95\udcff") == "\ufffd" * 6
        assert _clean("\ud800\udbff\udc00\udfff") == "\ufffd" * 12

    def test_clean_sha256_hashable_after(self):
        """Cleaned string can be hashed with SHA256 (the actual crash path)."""
        dirty = "content\udc95with\ud800surrogate"
        clean = _clean(dirty)
        # Must not raise UnicodeEncodeError
        h = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        assert len(h) == 64

    def test_clean_workbuddy_injected_surrogate(self):
        """
        The specific surrogate WorkBuddy injects during MCP relay.

        MCP clients can emit lone surrogates when relaying binary-in-Unicode
        or corrupted text.  \\udcad is the character observed in production logs.
        Each lone surrogate → 3 U+FFFD after the encode/decode round-trip.
        """
        result = _clean("2026-04-27\udcadworkBuddy relay")
        assert result == "2026-04-27" + "\ufffd" * 3 + "workBuddy relay"
        # SHA256 must not crash (was the original failure mode)
        hashlib.sha256(result.encode()).hexdigest()


# ── Integration tests for tool paths ────────────────────────────────────────

def _patch_mcp_server(monkeypatch, config, kg):
    """Swap production config and KG with test doubles (mirrors test_mcp_server.py)."""
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)


def _get_collection(palace_path, create=False):
    """Helper to open the test ChromaDB collection."""
    import chromadb

    client = chromadb.PersistentClient(path=str(palace_path))
    if create:
        return (
            client,
            client.get_or_create_collection(
                "mempalace_drawers",
                metadata={"hnsw:space": "cosine"},
            ),
        )
    return client, client.get_collection("mempalace_drawers")


class TestLoneSurrogateCleaning:
    """Integration tests: lone surrogates flow through all 5 tool handler paths."""

    def test_add_drawer_with_surrogate_in_content(
        self, monkeypatch, collection, config, kg
    ):
        """
        tool_add_drawer: _clean(content) prevents ChromaDB UnicodeEncodeError.

        The original crash occurred in ChromaDB's upsert() when the content
        string contained lone surrogates injected by the MCP client.
        """
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(
            wing="test",
            room="surrogate",
            content="drawer content with \udc95 surrogate",
        )
        assert result["success"] is True
        assert "drawer_id" in result
        assert result["drawer_id"].startswith("drawer_test_surrogate_")

    def test_add_drawer_with_surrogate_in_metadata(
        self, monkeypatch, collection, config, kg
    ):
        """
        tool_add_drawer: source_file and added_by also go through _clean().

        These metadata fields can carry paths or names that contain
        surrogates if the user's environment generates them.
        """
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(
            wing="test",
            room="meta",
            content="content here",
            source_file="path/to/\udcadfile.txt",
            added_by="user\udc95agent",
        )
        assert result["success"] is True

    def test_check_duplicate_with_surrogate(
        self, monkeypatch, collection, config, kg
    ):
        """
        tool_check_duplicate: _clean(content) before ChromaDB query().

        Without _clean(), col.query() raises UnicodeEncodeError when the
        query string contains lone surrogates.
        """
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace.mcp_server import tool_check_duplicate

        result = tool_check_duplicate(content="exact\udc95match")
        # Must not raise; UnicodeEncodeError would surface as a traceback
        assert isinstance(result, dict)
        assert "is_duplicate" in result or "error" in result or result.get("results") is not None

    def test_search_with_surrogate_in_query(
        self, monkeypatch, collection, config, kg
    ):
        """
        tool_search: _clean(query) before ChromaDB query().

        User search queries containing lone surrogates (e.g. pasted from
        corrupted clipboard) should not crash the search path.
        """
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace.mcp_server import tool_search

        result = tool_search(query="search\udc95term")
        assert isinstance(result, dict)
        assert "success" in result or "error" in result or "results" in result

    def test_update_drawer_with_surrogate(
        self, monkeypatch, collection, config, kg
    ):
        """
        tool_update_drawer: _clean(new_doc) before ChromaDB upsert().

        Updating a drawer's content with surrogate-laden text must persist
        cleanly without ChromaDB crashes.
        """
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace.mcp_server import tool_add_drawer, tool_update_drawer

        add_result = tool_add_drawer(wing="test", room="update", content="original content")
        assert add_result["success"] is True
        drawer_id = add_result["drawer_id"]

        update_result = tool_update_drawer(
            drawer_id=drawer_id,
            content="updated\udc95content",
        )
        assert update_result["success"] is True

    def test_diary_write_with_surrogate(
        self, monkeypatch, collection, config, kg
    ):
        """
        tool_diary_write: _clean(entry) before KG/ChromaDB write.

        Diary entries containing surrogates (e.g. copied from a corrupted
        document) must be accepted without crashing.
        """
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace.mcp_server import tool_diary_write

        result = tool_diary_write(
            agent_name="数数",
            entry="今日工作\udc95完成了，修复了Chromadb crash",
            topic="log",
        )
        assert result["success"] is True
