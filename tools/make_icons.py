"""앱 아이콘(PNG)을 만든다. 외부 라이브러리 없이 zlib 으로 PNG 를 직접 쓴다.

    python3 tools/make_icons.py

pokewatch/web/icons/ 아래에 홈화면·설치용 아이콘을 생성한다. 아이콘 디자인을
바꾸고 싶으면 draw_icon() 을 고친 뒤 다시 실행하면 된다.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "pokewatch" / "web" / "icons"

BG_TOP = (24, 27, 38)
BG_BOTTOM = (13, 15, 22)
BALL_RED = (239, 68, 68)
BALL_WHITE = (248, 250, 252)
BALL_LINE = (17, 20, 28)

SS = 3  # 계단 현상을 줄이기 위한 슈퍼샘플링 배수


def write_png(path: Path, width: int, height: int, rgba_rows: list[bytes]) -> None:
    raw = b"".join(b"\x00" + row for row in rgba_rows)  # 필터 타입 0

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def _sample(x: float, y: float, size: int, scale: float, rounded: bool):
    """아이콘 한 점의 색. scale 은 몬스터볼이 차지하는 비율(마스커블용 여백 확보)."""
    cx = cy = size / 2
    # 배경 (세로 그라데이션) — 마스커블은 모서리까지 꽉 채운다
    t = y / size
    bg = tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))

    if rounded:
        r = size * 0.22
        dx = max(abs(x - cx) - (size / 2 - r), 0)
        dy = max(abs(y - cy) - (size / 2 - r), 0)
        if math.hypot(dx, dy) > r:
            return (0, 0, 0, 0)

    ball_r = size * scale / 2
    d = math.hypot(x - cx, y - cy)
    if d > ball_r:
        return (*bg, 255)

    line_w = ball_r * 0.13
    center_r = ball_r * 0.27

    if d <= center_r:
        return (*BALL_WHITE, 255) if d <= center_r - line_w else (*BALL_LINE, 255)
    if abs(y - cy) <= line_w:
        return (*BALL_LINE, 255)
    if d >= ball_r - line_w * 0.8:
        return (*BALL_LINE, 255)
    return (*(BALL_RED if y < cy else BALL_WHITE), 255)


def draw_icon(size: int, scale: float = 0.74, rounded: bool = True) -> list[bytes]:
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    s = _sample(px + (sx + 0.5) / SS, py + (sy + 0.5) / SS, size, scale, rounded)
                    r += s[0] * s[3]; g += s[1] * s[3]; b += s[2] * s[3]; a += s[3]
            n = SS * SS
            if a:
                row += bytes((round(r / a), round(g / a), round(b / a), round(a / n)))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return rows


ICONS = [
    # (파일명, 크기, 볼 비율, 둥근 모서리)
    ("icon-192.png", 192, 0.74, True),
    ("icon-512.png", 512, 0.74, True),
    # 마스커블: 안드로이드가 임의 모양으로 잘라내므로 안전 영역(60%) 안에 그린다
    ("icon-maskable-512.png", 512, 0.56, False),
    # iOS 홈화면: 시스템이 알아서 둥글게 자르므로 사각형으로 둔다
    ("apple-touch-icon.png", 180, 0.74, False),
    ("favicon-32.png", 32, 0.80, True),
]


def png_bytes(size: int, scale: float = 0.74, rounded: bool = True) -> bytes:
    """PNG 를 파일 대신 바이트로 만든다 (.ico/.icns 안에 넣기 위해)."""
    import io

    buf = io.BytesIO()
    rows = draw_icon(size, scale, rounded)
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    buf.write(b"\x89PNG\r\n\x1a\n")
    buf.write(chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)))
    buf.write(chunk(b"IDAT", zlib.compress(raw, 9)))
    buf.write(chunk(b"IEND", b""))
    return buf.getvalue()


def write_ico(path: Path, sizes=(16, 32, 48, 64, 128, 256)) -> None:
    """윈도우 .exe 아이콘. 각 크기의 PNG 를 그대로 담는다."""
    images = [(s, png_bytes(s, rounded=True)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        # 256 은 ICO 디렉터리에서 0 으로 표기한다
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    path.write_bytes(header + entries + blobs)


def write_icns(path: Path) -> None:
    """맥 .app 아이콘. PNG 를 담을 수 있는 타입만 사용한다."""
    # (OSType, 픽셀 크기)
    types = [
        (b"ic07", 128), (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024),
        (b"ic11", 32), (b"ic12", 64), (b"ic13", 256), (b"ic14", 512),
    ]
    body = b""
    for ostype, size in types:
        data = png_bytes(size, rounded=False)  # 맥이 알아서 둥글게 자른다
        body += ostype + struct.pack(">I", len(data) + 8) + data
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main() -> None:
    for name, size, scale, rounded in ICONS:
        write_png(OUT_DIR / name, size, size, draw_icon(size, scale, rounded))
        print(f"  {name}  {size}x{size}")
    print(f"\n  → {OUT_DIR}")

    pkg = Path(__file__).resolve().parent.parent / "packaging"
    pkg.mkdir(exist_ok=True)
    write_ico(pkg / "icon.ico")
    write_icns(pkg / "icon.icns")
    print(f"  icon.ico / icon.icns\n\n  → {pkg}")


if __name__ == "__main__":
    main()
