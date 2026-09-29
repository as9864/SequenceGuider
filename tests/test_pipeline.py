import json

import av
import numpy as np
import pytest

from sequenceguider.align import _split_counts, align_sequence
from sequenceguider.analyze import analyze
from sequenceguider.checks import CHECKS
from sequenceguider.figures import FigureLibrary, UnknownFigureError, normalize, parse_sequence, parse_time
from sequenceguider.pose.base import PoseTrack
from sequenceguider.render import render_overlay
from sequenceguider.report import text_guide, write_report
from sequenceguider.steps import detect_steps
from tests.synthetic import walk

LIB = FigureLibrary.load()


class TestFigures:
    def test_every_figure_is_well_formed(self):
        for fig in LIB.figures.values():
            assert fig.cautions, fig.key
            assert fig.steps >= 1
            for spec in fig.checks:
                assert spec.id in CHECKS, (fig.key, spec.id)

    @pytest.mark.parametrize("name, key", [
        ("뿌에라 에헤", "fuera_de_eje"), ("Fuera de Eje", "fuera_de_eje"), ("푸에라데에헤", "fuera_de_eje"),
        ("Ocho Atrás", "ocho_atras"), ("오초 아뜨라스", "ocho_atras"), ("molinete", "giro"), ("사카다", "sacada"),
    ])
    def test_aliases(self, name, key):
        assert LIB.resolve(name).key == key

    def test_unknown_suggests(self):
        with pytest.raises(UnknownFigureError) as e:
            LIB.resolve("오쵸 아트라스")
        assert "오초 아뜨라스" in e.value.suggestions

    def test_normalize(self):
        assert normalize(" Ocho-Atrás ") == normalize("ochoatras")

    def test_parse_sequence(self):
        items = parse_sequence("살리다, 오초 아뜨라스 x3 → 뿌에라 에헤 @1:23.5\n볼레오", LIB)
        assert [i.figure.key for i in items] == ["salida_cruzada", "ocho_atras", "fuera_de_eje", "boleo"]
        assert items[1].count == 3 and items[1].expected_steps == 6
        assert items[2].anchor_s == pytest.approx(83.5)
        assert parse_time("75") == 75.0
        assert parse_time("1:02:03") == 3723.0
        assert parse_sequence("볼레오 @1:02:03", LIB)[0].anchor_s == 3723.0

    def test_anchor_and_count_either_order(self):
        a = parse_sequence("오초 x2 @0:10", LIB)[0]
        b = parse_sequence("오초 @0:10 x2", LIB)[0]
        assert (a.count, a.anchor_s) == (b.count, b.anchor_s) == (2, 10.0)

    def test_anchors_must_increase(self):
        with pytest.raises(ValueError):
            parse_sequence("살리다 @0:20, 볼레오 @0:10", LIB)

    def test_role_filter(self):
        fig = LIB.resolve("오초 아뜨라스")
        assert all(c.role in ("all", "leader") for c in fig.cautions_for("leader"))
        assert len(fig.cautions_for("all")) == len(fig.cautions)

    def test_text_guide_mentions_user_example(self):
        text = text_guide(parse_sequence("뿌에라 에헤", LIB))
        assert "상체가 빠지지 않게" in text and "골반이 뒤로" in text


class TestSteps:
    def test_counts_steps(self):
        steps = detect_steps(walk(8))
        assert len(steps) == 8
        assert [s.foot for s in steps] == ["l", "r"] * 4
        assert steps[0].start_t == pytest.approx(1.0, abs=0.1)

    def test_standing_still_has_no_steps(self):
        assert detect_steps(walk(step_times=[])) == []

    def test_jitter_is_not_a_step(self):
        tr = walk(step_times=[])
        rng = np.random.default_rng(0)
        tr.xy += rng.normal(0, 1.5, tr.xy.shape)  # ~1.5px keypoint noise
        assert detect_steps(tr) == []

    def test_facing_left(self):
        assert len(detect_steps(walk(6, facing=-1))) == 6


class TestAlign:
    def test_split_counts(self):
        assert _split_counts(10, [7, 2, 1]) == [7, 2, 1]
        assert sum(_split_counts(9, [7, 6, 2])) == 9
        assert min(_split_counts(3, [10, 1, 1])) == 1  # nobody left empty

    def test_proportional_split(self):
        tr = walk(11)  # salida(7) + ocho x2 (4)
        items = parse_sequence("살리다, 오초 아뜨라스 x2", LIB)
        segs = align_sequence(items, detect_steps(tr), float(tr.t[-1]))
        assert [len(s.steps) for s in segs] == [7, 4]
        assert all(s.confidence == "high" for s in segs)
        assert segs[0].end_t == segs[1].start_t
        assert segs[1].start_t == pytest.approx((1.0 + 6 * 0.8 + 0.35 + 1.0 + 7 * 0.8) / 2, abs=0.15)

    def test_anchor_overrides(self):
        tr = walk(10)
        steps = detect_steps(tr)
        items = parse_sequence("까미나따, 볼레오 @6.6", LIB)
        segs = align_sequence(items, steps, float(tr.t[-1]))
        assert segs[1].start_t == pytest.approx(6.6)
        assert segs[1].anchored
        assert all(s.mid_t >= 6.6 for s in segs[1].steps)

    @pytest.mark.parametrize("n, expected", [(7, "high"), (3, "medium"), (1, "low")])
    def test_mismatch_lowers_confidence(self, n, expected):
        tr = walk(n)  # salida expects 7
        segs = align_sequence(parse_sequence("살리다", LIB), detect_steps(tr), float(tr.t[-1]))
        assert segs[0].confidence == expected

    def test_no_steps_splits_by_time(self):
        items = parse_sequence("까미나따, 볼레오", LIB)  # 4 : 1
        segs = align_sequence(items, [], 10.0)
        assert segs[0].end_t == pytest.approx(8.0)


class TestChecks:
    def _result(self, track, seq, view="side"):
        return analyze(track, parse_sequence(seq, LIB), view=view)

    def test_upright_walk_passes(self):
        a = self._result(walk(4, lean_deg=2.0), "까미나따")
        by_id = {c.id: c for c in a.figures[0].checks}
        assert by_id["torso_lean"].status == "ok"
        assert by_id["torso_lean"].value == pytest.approx(2.0, abs=0.3)
        assert by_id["axis_vertical"].status in ("ok", "warn")

    @pytest.mark.parametrize("facing", [1, -1])
    def test_forward_lean_flagged(self, facing):
        a = self._result(walk(4, lean_deg=14.0, facing=facing), "까미나따")
        c = next(c for c in a.figures[0].checks if c.id == "torso_lean")
        assert c.status == "warn" and c.direction == "high"
        assert c.value == pytest.approx(14.0, abs=0.5)
        assert "앞으로" in c.message

    def test_fuera_de_eje_pelvis_forward(self):
        """The user's example: in fuera de eje the pelvis must stay back."""
        good = self._result(walk(2, lean_deg=10.0, pelvis_ahead=-0.05), "뿌에라 에헤")
        bad = self._result(walk(2, lean_deg=0.0, pelvis_ahead=0.25), "뿌에라 에헤")
        g = {c.id: c for c in good.figures[0].checks}
        b = {c.id: c for c in bad.figures[0].checks}
        assert g["pelvis_offset"].status == "ok"
        assert b["pelvis_offset"].status == "warn" and b["pelvis_offset"].direction == "high"
        assert "골반" in b["pelvis_offset"].message
        # the kink the forward pelvis creates also breaks the body line
        assert b["body_line"].status == "warn"

    @pytest.mark.parametrize("facing", [1, -1])
    def test_intended_off_axis_lean_is_not_a_fault(self, facing):
        """A straight body tilted as one plank is exactly what volcada/fuera de eje ask for."""
        for seq in ("뿌에라 에헤", "볼까다"):
            a = self._result(walk(2, tilt_deg=15.0, facing=facing), seq)
            by_id = {c.id: c for c in a.figures[0].checks}
            assert by_id["body_line"].status == "ok", seq
            assert by_id["pelvis_offset"].status == "ok", seq
            assert abs(by_id["pelvis_offset"].value) < 0.1  # support foot shifts during steps

    def test_piking_flagged_in_volcada(self):
        """Folding at the hips (seat out) instead of leaning as one line."""
        a = self._result(walk(2, lean_deg=30.0), "볼까다")
        by_id = {c.id: c for c in a.figures[0].checks}
        assert by_id["pelvis_offset"].status == "warn" and by_id["pelvis_offset"].direction == "low"
        assert by_id["body_line"].status == "warn"

    def test_front_view_marks_side_only_checks_na(self):
        a = self._result(walk(4), "까미나따", view="front")
        by_id = {c.id: c for c in a.figures[0].checks}
        assert by_id["torso_lean"].status == "na"
        assert by_id["axis_vertical"].status != "na"

    def test_missing_person_is_unknown(self):
        tr = walk(4)
        tr.conf[:, :] = 0.95
        tr.conf[:, 3:5] = 0.0  # shoulders never visible
        with pytest.raises(ValueError):
            analyze(tr, parse_sequence("까미나따", LIB))

    def test_feet_collect(self):
        a = self._result(walk(4), "까미나따")
        c = next(c for c in a.figures[0].checks if c.id == "feet_collect")
        assert c.status == "ok"  # feet pass each other every step


def _write_video(path, track: PoseTrack):
    w, h = track.image_size
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=int(track.fps))
        stream.width, stream.height, stream.pix_fmt = w, h, "yuv420p"
        for i in range(len(track)):
            img = np.full((h, w, 3), 40, np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            for p in stream.encode(frame):
                container.mux(p)
        for p in stream.encode():
            container.mux(p)


class TestReport:
    def test_end_to_end(self, tmp_path):
        tr = walk(9, lean_deg=12.0)
        video = tmp_path / "practice.mp4"
        _write_video(video, tr)
        a = analyze(tr, parse_sequence("살리다, 오초 아뜨라스", LIB))
        overlay = render_overlay(video, a, tmp_path / "overlay.mp4")
        with av.open(str(overlay)) as c:
            assert c.streams.video[0].codec_context.name == "h264"
            assert sum(1 for _ in c.decode(video=0)) == len(tr)
        page = write_report(a, video, tmp_path, video_src="overlay.mp4", title="테스트")
        html = page.read_text(encoding="utf-8")
        assert "살리다 크루사다" in html and "오초 아뜨라스" in html
        assert "data:image/jpeg;base64," in html
        data = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        assert [f["figure"] for f in data["figures"]] == ["salida_cruzada", "ocho_atras"]
        assert data["steps_detected"] == 9

    def test_pose_cache_roundtrip(self, tmp_path):
        tr = walk(2)
        tr.save(tmp_path / "p.npz")
        back = PoseTrack.load(tmp_path / "p.npz")
        np.testing.assert_allclose(back.xy, tr.xy)
        assert back.image_size == tr.image_size and back.fps == tr.fps
