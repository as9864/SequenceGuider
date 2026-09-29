"""sequenceguider — tango sequence coach CLI."""

import shutil
import sys
from pathlib import Path

import typer

# Korean output on Windows consoles (cp949) would otherwise crash on '—' etc.
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

app = typer.Typer(no_args_is_help=True, add_completion=False, help="탱고 시퀀스를 영상에서 나눠 피구라별 주의점을 알려줍니다.")


def _load(sequence: str, library: Path | None):
    from sequenceguider.figures import DEFAULT_LIBRARY, FigureLibrary, UnknownFigureError, parse_sequence

    lib = FigureLibrary.load(library or DEFAULT_LIBRARY)
    try:
        return lib, parse_sequence(sequence, lib)
    except UnknownFigureError as e:
        typer.echo(f"오류: {e}\n`sequenceguider figures`로 알아듣는 이름 목록을 볼 수 있어요.", err=True)
        raise typer.Exit(2)
    except ValueError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(2)


def _check_role(role: str) -> None:
    if role not in ("all", "leader", "follower"):
        typer.echo("오류: --role은 all | leader | follower", err=True)
        raise typer.Exit(2)


@app.command()
def figures(library: Path = typer.Option(None, help="다른 figures.yaml 사용")) -> None:
    """알아듣는 피구라 이름 목록."""
    from sequenceguider.figures import DEFAULT_LIBRARY, FigureLibrary

    lib = FigureLibrary.load(library or DEFAULT_LIBRARY)
    for fig in lib.figures.values():
        typer.echo(f"{fig.name_ko} ({fig.name_es}) — 스텝 {fig.steps}개 기준")
        typer.echo(f"    입력 예: {', '.join(fig.aliases[:5])}")


@app.command()
def guide(
    sequence: str = typer.Argument(..., help='예: "살리다, 오초 아뜨라스 x3, 푸에라 데 에헤"'),
    role: str = typer.Option("all", help="all | leader | follower"),
    library: Path = typer.Option(None),
) -> None:
    """영상 없이 시퀀스 순서별 주의점만 출력."""
    from sequenceguider.report import text_guide

    _check_role(role)
    _, items = _load(sequence, library)
    typer.echo(text_guide(items, role))


@app.command()
def analyze(
    video: Path = typer.Argument(..., exists=True, dir_okay=False, help="연습 영상 (mp4/mov)"),
    sequence: str = typer.Option(..., "--sequence", "-s",
                                 help='순서대로 쉼표 구분. 반복 "x3", 시작 시간 고정 "@1:23"'),
    out: Path = typer.Option(None, help="출력 폴더 (기본: <영상이름>_sequenceguider/)"),
    view: str = typer.Option("side", help="카메라 위치: side(측면, 권장) | front(정면)"),
    role: str = typer.Option("all", help="주의점 관점: all | leader | follower"),
    person: str = typer.Option("largest", help="여러 명이 보일 때: largest | left | right"),
    model: str = typer.Option("full", help="포즈 모델: lite | full | heavy"),
    overlay: bool = typer.Option(True, help="스켈레톤·자막을 입힌 영상을 만들어 리포트에 넣기"),
    library: Path = typer.Option(None, help="다른 figures.yaml 사용"),
    reuse_pose: bool = typer.Option(True, help="이전에 뽑은 포즈 캐시 재사용"),
) -> None:
    """영상 + 시퀀스 → 피구라별 구간·주의점·자세 체크 HTML 리포트."""
    from sequenceguider.analyze import analyze as run_analysis
    from sequenceguider.pose.base import PoseTrack
    from sequenceguider.report import fmt_t, write_report

    _check_role(role)
    if view not in ("side", "front"):
        typer.echo("오류: --view는 side | front", err=True)
        raise typer.Exit(2)
    if person not in ("largest", "left", "right"):
        typer.echo("오류: --person은 largest | left | right", err=True)
        raise typer.Exit(2)
    _, items = _load(sequence, library)
    out = out or video.with_name(f"{video.stem}_sequenceguider")
    out.mkdir(parents=True, exist_ok=True)

    cache = out / f"pose_{model}_{person}.npz"
    if reuse_pose and cache.exists() and cache.stat().st_mtime >= video.stat().st_mtime:
        track = PoseTrack.load(cache)
        typer.echo(f"포즈 캐시 사용: {cache.name}")
    else:
        from sequenceguider.pose.mediapipe_backend import extract_pose

        typer.echo(f"포즈 추출 중 (MediaPipe {model}, 첫 실행 시 모델 다운로드)...")
        with typer.progressbar(length=1000, label="포즈") as bar:
            state = {"p": 0}

            def progress(i: int, total: int) -> None:
                p = int(1000 * i / total) if total else 0
                if p > state["p"]:
                    bar.update(p - state["p"])
                    state["p"] = p

            track = extract_pose(video, variant=model, person=person, progress=progress)
        track.save(cache)

    try:
        analysis = run_analysis(track, items, view=view, role=role)
    except ValueError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(1)

    if overlay:
        from sequenceguider.render import render_overlay

        with typer.progressbar(length=1000, label="영상") as bar:
            state = {"p": 0}

            def progress2(i: int, total: int) -> None:
                p = int(1000 * i / total) if total else 0
                if p > state["p"]:
                    bar.update(p - state["p"])
                    state["p"] = p

            render_overlay(video, analysis, out / "overlay.mp4", progress=progress2)
        video_src = "overlay.mp4"
    else:
        dest = out / video.name
        if not dest.exists():
            shutil.copy2(video, dest)
        video_src = video.name

    report = write_report(analysis, video, out, video_src=video_src, title=f"{video.stem} — 시퀀스 가이드")

    typer.echo(f"\n사람 인식 {analysis.person_coverage:.0%} 프레임, 스텝 {len(analysis.steps)}개 감지")
    for fa in analysis.figures:
        seg = fa.segment
        flag = "  ⚠ " + ", ".join(c.label for c in fa.warnings) if fa.warnings else ""
        conf = "" if seg.confidence == "high" else f"  (분할 {seg.confidence})"
        typer.echo(f"{seg.index + 1}. {seg.item.label:<16} {fmt_t(seg.start_t)}–{fmt_t(seg.end_t)}{conf}{flag}")
    typer.echo(f"\n리포트: {report}")


if __name__ == "__main__":
    app()
