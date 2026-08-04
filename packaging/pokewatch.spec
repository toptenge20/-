# PyInstaller 설정 — 파이썬 없이도 실행되는 앱으로 묶는다.
#
#   pip install pyinstaller
#   pyinstaller packaging/pokewatch.spec
#
# 결과물은 dist/ 아래에 생긴다. 맥에서는 dist/포켓몬카드시세.app,
# 윈도우에서는 dist/포켓몬카드시세.exe.
#
# 크로스 컴파일은 안 된다. 맥용 앱은 맥에서, 윈도우용 exe 는 윈도우에서 만들어야 한다.

import sys
from pathlib import Path

APP_NAME = "포켓몬카드시세"
ROOT = Path(SPECPATH).resolve().parent

# 아이콘은 OS 마다 형식이 다르다. tools/make_icons.py 로 만든다.
_icns = ROOT / "packaging" / "icon.icns"
_ico = ROOT / "packaging" / "icon.ico"
if sys.platform == "darwin" and _icns.exists():
    ICON = str(_icns)
elif sys.platform.startswith("win") and _ico.exists():
    ICON = str(_ico)
else:
    ICON = None

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # 대시보드 화면과 카드 이름 사전은 코드가 아니라 데이터라서 직접 넣어 줘야 한다.
    datas=[
        (str(ROOT / "pokewatch" / "web"), "pokewatch/web"),
        (str(ROOT / "pokewatch" / "data"), "pokewatch/data"),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # pywebview 가 있으면 전용 창으로, 없으면 브라우저로 연다. 없어도 빌드는 성공해야 한다.
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    strip=False,
    upx=False,
    # 콘솔 창을 띄워 진행 상황과 오류를 볼 수 있게 한다.
    # 창만 깔끔하게 띄우고 싶으면 False 로 바꾼다.
    console=True,
    icon=ICON,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name=f"{APP_NAME}.app",
        icon=ICON,
        bundle_identifier="local.pokewatch.app",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": "포켓몬 카드 시세 보드",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.utilities",
        },
    )
