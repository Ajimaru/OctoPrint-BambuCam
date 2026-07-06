"""Tests for octoprint_bambucam.render_presets."""

from octoprint_bambucam import render_presets


class TestPresets:
    """Preset lookup and filter-string building."""

    def test_get_preset_known(self):
        """A known preset name returns its dict."""
        preset = render_presets.get_preset("medium_1080p")
        assert preset["scale"] == 1920
        assert preset["crf"] == 23

    def test_get_preset_default_fallback(self):
        """An unknown name falls back to the default preset."""
        preset = render_presets.get_preset("nope")
        assert preset == render_presets.PRESETS[render_presets.DEFAULT_PRESET]

    def test_preset_names(self):
        """All four presets are listed in order."""
        names = render_presets.preset_names()
        assert names == [
            "fast_720p",
            "medium_1080p",
            "quality_1080p",
            "original",
        ]

    def test_build_vf_speed_and_scale(self):
        """The vf string encodes setpts (1/N) and scale."""
        vf = render_presets.build_vf(render_presets.get_preset("fast_720p"))
        assert "setpts=0.2*PTS" in vf
        assert "scale=1280:-2" in vf

    def test_build_vf_original_one_x(self):
        """The original preset plays at 1× (setpts=1.0)."""
        vf = render_presets.build_vf(render_presets.get_preset("original"))
        assert "setpts=1.0*PTS" in vf
        assert "scale=1680:-2" in vf
