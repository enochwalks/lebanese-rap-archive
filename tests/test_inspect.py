"""The HTML edit report.

Tests stay dependency-free: they check the page is self-contained and that the
data it renders from is the real project, rather than driving a browser.
"""

import json
import re
from fractions import Fraction

import pytest

from conftest import fake_audio_media, fake_video_media
from edit_engine.inspect import build_report_html, write_report
from edit_engine.model import Project, Sequence, make_clip
from edit_engine.programs.beat_edit import build_music_video

RATE = Fraction(30)


def embedded(html):
    match = re.search(r"const EMBEDDED = (\{.*?\});\n", html, re.S)
    assert match, "report has no embedded project data"
    return json.loads(match.group(1))


@pytest.fixture
def built(project):
    sequence = project.add_sequence(Sequence.create(name="MV", rate=RATE))
    song = fake_audio_media(project, "song.wav", seconds=30)
    visuals = [fake_video_media(project, f"clip{i}.mp4", seconds=20) for i in range(3)]
    build_music_video(project, song.path, [v.path for v in visuals],
                      sequence=sequence, seed=4)
    return project, sequence


class TestReport:
    def test_page_is_self_contained(self, built):
        project, sequence = built
        html = build_report_html(project, sequence)
        assert "<!doctype html>" in html.lower()
        # a strict-CSP page and an offline machine must both render it
        assert "http://" not in html and "https://" not in html
        assert "<script src" not in html and "<link" not in html

    def test_embedded_data_is_the_real_project(self, built):
        project, sequence = built
        data = embedded(build_report_html(project, sequence))
        assert data["name"] == project.name
        clips = data["sequences"][0]["video_tracks"][0]["clips"]
        assert len(clips) == len(sequence.video_tracks[0].clips)

    def test_every_shot_carries_its_reasoning(self, built):
        project, sequence = built
        data = embedded(build_report_html(project, sequence))
        for clip in data["sequences"][0]["video_tracks"][0]["clips"]:
            decision = clip["metadata"]["decision"]
            assert decision["effect_reason"]
            assert decision["source_reason"]
            assert "cut_on_beat" in decision

    def test_analysis_is_recorded_for_the_report_to_show(self, built):
        project, sequence = built
        analysis = sequence.metadata["analysis"]
        assert analysis["program"] == "beat_edit"
        assert analysis["seed"] == 4
        assert "cut_mode" in analysis and "beats" in analysis

    def test_requested_sequence_is_rendered_first(self, project):
        first = project.add_sequence(Sequence.create(name="A", rate=RATE))
        second = project.add_sequence(Sequence.create(name="B", rate=RATE))
        data = embedded(build_report_html(project, second))
        assert data["sequences"][0]["name"] == "B"

    def test_title_cannot_inject_markup(self, built):
        project, sequence = built
        html = build_report_html(project, sequence, title="<script>alert(1)</script>")
        assert "<title><script>" not in html

    def test_write_report_creates_missing_directories(self, built, tmp_path):
        project, sequence = built
        path = write_report(project, sequence, tmp_path / "deep" / "nested" / "r.html")
        assert path.exists() and path.read_text(encoding="utf-8").startswith("<!doctype")


class TestNoClassCollisions:
    """Regression: `<span class="pill beat">` also matched the `.beat` timeline
    tick rule (position:absolute), which threw the pill to the top of the page."""

    def test_pill_modifiers_do_not_reuse_positioned_class_names(self, built):
        project, sequence = built
        html = build_report_html(project, sequence)
        positioned = re.findall(r"\.([a-z-]+)\s*\{[^}]*position:absolute", html)
        pill_modifiers = set(re.findall(r'class="pill ([a-z-]+)"', html))
        assert not pill_modifiers & set(positioned), (
            f"pill modifier collides with a positioned class: "
            f"{pill_modifiers & set(positioned)}")
