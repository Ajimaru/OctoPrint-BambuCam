"""Tests for octoprint_bambucam.paths helpers."""

from octoprint_bambucam.paths import bambu_sort_key, sanitize_filename


class TestBambuSortKey:
    """bambu_sort_key builds a chronological key from Bambu video names."""

    def test_timestamp_digits_extracted(self):
        """The name's timestamp becomes a plain sortable digit string."""
        assert (
            bambu_sort_key("video_2026-05-18_04-23-50.avi") == "20260518042350"
        )

    def test_no_timestamp_sorts_first(self):
        """Names without a timestamp yield '' (sort before real ones)."""
        assert bambu_sort_key("index") == ""
        assert bambu_sort_key("") == ""

    def test_orders_chronologically(self):
        """Keys of two captures compare in capture order."""
        older = bambu_sort_key("video_2026-05-18_02-54-50.avi")
        newer = bambu_sort_key("video_2026-05-18_04-55-59.avi")
        assert older < newer


class TestSanitizeFilename:
    """sanitize_filename wraps OctoPrint's helper without raising."""

    def test_plain_name_passes_through(self):
        """A safe name comes back unchanged."""
        assert sanitize_filename("benchy.mp4") == "benchy.mp4"

    def test_path_separator_rejected_as_empty(self):
        """OctoPrint raises on separators; the wrapper returns ''."""
        assert sanitize_filename("a/b.mp4") == ""
