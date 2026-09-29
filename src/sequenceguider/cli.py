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


def _youtube_id(url: str) -> str:
    import re

    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{6,})", url)
    return m.group(1) if m else "video"


SOURCE_NAMES = {"chapters": "영상 챕터", "description": "영상 설명란 타임스탬프", "transcript": "영상 자막"}


def _print_matches(meta, src) -> None:
    from sequenceguider.report import fmt_t

    where = SOURCE_NAMES.get(meta.source, "")
    if meta.source == "transcript":
        where += f" ({src.transcript_lang})" if src.transcript_lang else ""
    typer.echo(f"{where}에서 {len(meta.matches)}개 항목을 읽었습니다:")
    for m in meta.matches:
        mark = f"→ {m.figure.name_ko}" + (f" ×{m.count}" if m.count > 1 else "") if m.figure else "→ (피구라 아님, 건너뜀)"
        typer.echo(f"  {fmt_t(m.chapter.start_s):>7}  {m.chapter.title}  {mark}")
    if meta.source == "transcript":
        typer.echo("  (자막은 말한 시점이라 실제 동작보다 조금 이를 수 있어요. 챕터보다 덜 정확하니 한번 확인하세요)")


@app.command()
def preview(
    url: str = typer.Argument(..., help="유튜브 링크"),
    library: Path = typer.Option(None, help="다른 figures.yaml 사용"),
) -> None:
    """링크의 챕터·설명란·자막에서 읽은 시퀀스를 미리 보기 (다운로드 안 함)."""
    from sequenceguider.figures import DEFAULT_LIBRARY, FigureLibrary
    from sequenceguider.youtube import fetch_info, sequence_from_metadata

    lib = FigureLibrary.load(library or DEFAULT_LIBRARY)
    try:
        src = fetch_info(url)
    except Exception as e:  # yt-dlp raises many types; all mean "couldn't read this link"
        typer.echo(f"오류: 링크를 읽지 못했습니다 — {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{src.title}\n")
    meta = sequence_from_metadata(src, lib)
    if not meta.matches:
        typer.echo("챕터·설명란 타임스탬프가 없고 자막에서도 피구라 이름을 찾지 못했습니다 "
                   "→ analyze 할 때 -s로 순서를 직접 알려주세요.")
        raise typer.Exit(0)
    _print_matches(meta, src)
    if meta.items:
        typer.echo("\n그대로 쓰거나 고쳐서 -s에 넣으세요:")
        typer.echo(f'  -s "{meta.as_sequence_text()}"')


@app.command()
def analyze(
    video: str = typer.Argument(..., help="연습 영상 파일 경로 또는 유튜브 링크"),
    sequence: str = typer.Option(None, "--sequence", "-s",
                                 help='순서대로 쉼표 구분. 반복 "x3", 시작 시간 고정 "@1:23". '
                                      "유튜브 링크는 생략하면 챕터·설명란·자막에서 읽음"),
    out: Path = typer.Option(None, help="출력 폴더 (기본: <영상이름>_sequenceguider/)"),
    view: str = typer.Option("side", help="카메라 위치: side(측면, 권장) | front(정면)"),
    role: str = typer.Option("all", help="주의점 관점: all | leader | follower"),
    person: str = typer.Option("largest", help="여러 명이 보일 때: largest | left | right"),
    model: str = typer.Option("full", help="포즈 모델: lite | full | heavy"),
    overlay: bool = typer.Option(True, help="스켈레톤·자막을 입힌 영상을 만들어 리포트에 넣기"),
    library: Path = typer.Option(None, help="다른 figures.yaml 사용"),
    reuse_pose: bool = typer.Option(True, help="이전에 뽑은 포즈 캐시 재사용"),
) -> None:
    """영상(파일 또는 유튜브 링크) + 시퀀스 → 피구라별 구간·주의점·자세 체크 HTML 리포트."""
    from sequenceguider.analyze import analyze as run_analysis
    from sequenceguider.figures import DEFAULT_LIBRARY, FigureLibrary
    from sequenceguider.pose.base import PoseTrack
    from sequenceguider.report import fmt_t, write_report
    from sequenceguider.youtube import is_url

    _check_role(role)
    if view not in ("side", "front"):
        typer.echo("오류: --view는 side | front", err=True)
        raise typer.Exit(2)
    if person not in ("largest", "left", "right"):
        typer.echo("오류: --person은 largest | left | right", err=True)
        raise typer.Exit(2)

    source_url = None
    sequence_note = None
    if is_url(video):
        from sequenceguider.youtube import fetch_video, sequence_from_metadata

        source_url = video
        out = out or Path(f"youtube_{_youtube_id(video)}_sequenceguider")
        typer.echo("영상 받는 중 (본인 영상이나 허락받은 영상만, 개인 학습용으로 사용하세요)...")
        try:
            src = fetch_video(video, out / "source")
        except Exception as e:  # yt-dlp raises many types; all mean "couldn't get this video"
            typer.echo(f"오류: 영상을 받지 못했습니다 — {e}", err=True)
            raise typer.Exit(1)
        video_path = Path(src.path)
        title = src.title or video_path.stem
        typer.echo(f"{title} ({video_path.name})")
        if sequence:
            _, items = _load(sequence, library)
        else:
            lib = FigureLibrary.load(library or DEFAULT_LIBRARY)
            meta = sequence_from_metadata(src, lib)
            if meta.matches:
                _print_matches(meta, src)
            if not meta.items:
                typer.echo(
                    "오류: 영상 챕터·설명란·자막에서 피구라 순서를 찾지 못했습니다. "
                    '-s "살리다, 오초 아뜨라스 x3, ..."로 순서를 알려주세요.', err=True)
                raise typer.Exit(2)
            items = meta.items
            sequence_note = SOURCE_NAMES[meta.source]
            typer.echo(f'(고치려면: -s "{meta.as_sequence_text()}")')
    else:
        video_path = Path(video)
        if not video_path.is_file():
            typer.echo(f"오류: 파일이 없습니다: {video_path}", err=True)
            raise typer.Exit(2)
        if not sequence:
            typer.echo('오류: 파일 영상은 -s로 순서를 알려주세요. 예: -s "살리다, 오초 아뜨라스 x3"', err=True)
            raise typer.Exit(2)
        _, items = _load(sequence, library)
        title = video_path.stem
        out = out or video_path.with_name(f"{video_path.stem}_sequenceguider")
    video = video_path
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

    report = write_report(analysis, video, out, video_src=video_src, title=f"{title} — 시퀀스 가이드",
                          source_url=source_url, sequence_note=sequence_note)

    typer.echo(f"\n사람 인식 {analysis.person_coverage:.0%} 프레임, 스텝 {len(analysis.steps)}개 감지")
    for fa in analysis.figures:
        seg = fa.segment
        flag = "  ⚠ " + ", ".join(c.label for c in fa.warnings) if fa.warnings else ""
        conf = "" if seg.confidence == "high" else f"  (분할 {seg.confidence})"
        typer.echo(f"{seg.index + 1}. {seg.item.label:<16} {fmt_t(seg.start_t)}–{fmt_t(seg.end_t)}{conf}{flag}")
    typer.echo(f"\n리포트: {report}")


if __name__ == "__main__":
    app()
