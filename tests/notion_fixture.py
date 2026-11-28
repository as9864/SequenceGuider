"""A Notion "Markdown & CSV" export of a database like the user's "Tango Note", built the way Notion builds it:
an outer zip with a Part-1 zip inside, page ids at the end of every name, Korean names without the zip's
UTF-8 flag, a CSV with a BOM, URL-encoded links to images and subpages."""

import io
import zipfile
from pathlib import Path
from urllib.parse import quote

DB = "Tango Note 1a12afc0000080aaaaaaaaaaaaaaaaaa"
IDS = {
    "소똥 개인레슨": "1a12afc0000080b1b1b1b1b1b1b1b1b1",
    "10. 26. 쁘락 오답 체크": "1a12afc0000080b2b2b2b2b2b2b2b2b2",
    "피드백 정리 노트": "1a12afc0000080b3b3b3b3b3b3b3b3b3",
    "커넥션에 대한 개념": "1a12afc0000080b4b4b4b4b4b4b4b4b4",
    "Brenno y fati class": "1a12afc0000080b5b5b5b5b5b5b5b5b5",
    "성공 노트": "1a12afc0000080b6b6b6b6b6b6b6b6b6",
    "깔리토스": "1a12afc0000080b7b7b7b7b7b7b7b7b7",
}
SUB = ("히로 연습 메모", "1a12afc0000080c1c1c1c1c1c1c1c1c1")
SUBSUB = ("발 모양", "1a12afc0000080c2c2c2c2c2c2c2c2c2")
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def name(title: str) -> str:
    return f"{title} {IDS[title]}"


def files(body_extra: str = "") -> dict[str, bytes | str]:
    sotong = name("소똥 개인레슨")
    sub = f"{SUB[0]} {SUB[1]}"
    out: dict[str, bytes | str] = {
        f"{DB}.csv": "﻿이름,사람,Keyword,요약,날짜,영상주소\n"
                     "소똥 개인레슨,,\"다리앤소, 걷기, 사까다, 히로\",,,\n"
                     "10. 26. 쁘락 오답 체크,,,,,\n"
                     "피드백 정리 노트,\"한승엽, [이약사의 세계여행] Minalee\",\"걷기, 디아고날, 커넥션, 자세교정\",,2025년 12월 13일,\n"
                     "커넥션에 대한 개념,,,,,\n"
                     "Brenno y fati class,,,,2025년 11월 7일,https://youtu.be/AAAAAAAAAAA\n"
                     "성공 노트,,\"걷기, 오쵸, 커넥션\",,,\n"
                     "깔리토스,,,,,\n",
        f"{DB}.md": "# Tango Note\n\n" + f"[{DB}.csv]({quote(DB)}.csv)\n",  # the database page itself
        f"{DB}/{sotong}.md": (
            "# 소똥 개인레슨\n\n"
            "Keyword: 다리앤소, 걷기, 사까다, 히로\n\n"
            "## 걷기\n\n"
            "- 골반이 먼저, 발은 나중에\n    - 축 다리 무릎은 **살짝** 굽히기\n- 바닥을 *밀어서* 나간다\n\n"
            f"![image.png]({quote(sotong)}/image.png)\n\n"
            "<aside>\n💡 선생님: 상체는 파트너를 향해 둔다\n</aside>\n\n"
            "1. 오른발 사까다\n2. 히로로 연결\n\n"
            "- [x] 걷기 10분\n- [ ] 사까다 거울 앞에서\n\n"
            "| 피구라 | 포인트 |\n| --- | --- |\n| 히로 | 축 유지 |\n\n"
            "시연 영상: https://www.youtube.com/watch?v=BBBBBBBBBBB\n\n"
            f"[{SUB[0]}]({quote(sotong)}/{quote(sub)}.md)\n\n"
            "<script>alert(1)</script>\n" + body_extra),
        f"{DB}/{sotong}/image.png": PNG,
        f"{DB}/{sotong}/{sub}.md": (
            f"# {SUB[0]}\n\n축을 세우고 돈다.\n\n[{SUBSUB[0]}]({quote(SUB[0])}/{quote(SUBSUB[0] + ' ' + SUBSUB[1])}.md)\n"),
        f"{DB}/{sotong}/{SUB[0]}/{SUBSUB[0]} {SUBSUB[1]}.md": f"# {SUBSUB[0]}\n\n뒤꿈치를 붙인다.\n",
        f"{DB}/{name('10. 26. 쁘락 오답 체크')}.md": "# 10. 26. 쁘락 오답 체크\n\n- 음악 안 듣고 피구라만 함\n",
        f"{DB}/{name('피드백 정리 노트')}.md": (
            "# 피드백 정리 노트\n\n사람: 한승엽, [이약사의 세계여행] Minalee\nKeyword: 걷기, 디아고날, 커넥션, 자세교정\n"
            "날짜: 2025년 12월 13일\n\n<details>\n<summary>걷기 피드백</summary>\n보폭을 줄이기\n</details>\n\n"
            f"관련: [소똥 개인레슨]({quote(name('소똥 개인레슨'))}.md)\n"),
        f"{DB}/{name('커넥션에 대한 개념')}.md": "# 커넥션에 대한 개념\n\n> 가슴으로 듣는다\n",
        f"{DB}/{name('Brenno y fati class')}.md": "# Brenno y fati class\n\n날짜: 2025년 11월 7일\n영상주소: https://youtu.be/AAAAAAAAAAA\n\n볼레오 타이밍\n",
        f"{DB}/{name('성공 노트')}.md": "# 성공 노트\n\nKeyword: 걷기, 오쵸, 커넥션\n\n오늘은 잘 됐다.\n",
        f"{DB}/{name('깔리토스')}.md": "# 깔리토스\n\n꼴가다: 움직임 사이에 부드러운 연결감\n",
    }
    return out


def _no_utf8_flag(info: zipfile.ZipInfo):
    return info.filename.encode("utf-8"), info.flag_bits & ~0x800


def write_zip(path: Path, body_extra: str = "", nested: bool = True) -> Path:
    inner = io.BytesIO()
    orig = zipfile.ZipInfo._encodeFilenameFlags
    zipfile.ZipInfo._encodeFilenameFlags = _no_utf8_flag  # Notion's zips don't set it
    try:
        with zipfile.ZipFile(inner, "w") as zf:
            for n, data in files(body_extra).items():
                zf.writestr(n, data.encode("utf-8") if isinstance(data, str) else data)
    finally:
        zipfile.ZipInfo._encodeFilenameFlags = orig
    if not nested:
        path.write_bytes(inner.getvalue())
        return path
    with zipfile.ZipFile(path, "w") as outer:
        outer.writestr("Export-6f1e2d3c-Part-1.zip", inner.getvalue())
    return path
