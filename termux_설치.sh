#!/data/data/com.termux/files/usr/bin/bash
# 안드로이드 폰 하나로 전부 돌리기 (컴퓨터·클라우드 없이).
#
# Termux 를 설치한 뒤(F-Droid 권장, 플레이스토어 버전은 오래돼서 안 됩니다)
# 이 저장소 폴더에서 실행하세요:
#
#     bash termux_설치.sh
#
# 끝나면 `pokewatch` 한 줄로 앱이 켜지고, 크롬에서 http://localhost:8765 를 열어
# '홈 화면에 추가' 하면 폰 안에서 완결되는 앱이 됩니다. localhost 는 보안 컨텍스트라
# 오프라인 캐시(서비스 워커)까지 정상 동작합니다.

set -e

echo "▶ 필요한 것 설치 중…"
pkg update -y
pkg install -y python git

echo
echo "▶ 배터리 최적화에서 제외하기"
echo "  안드로이드가 백그라운드 앱을 죽이면 수집이 멈춥니다."
echo "  Termux 알림을 길게 눌러 'Acquire wakelock' 을 켜 두세요."

# 폰에서 실행하기 쉽게 짧은 명령을 만든다
REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="$PREFIX/bin/pokewatch"
cat > "$BIN" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# 폰 안에서 시세 앱 실행 (60분마다 자동 수집)
cd "$REPO"
exec python3 app.py --no-window --auto-collect 60 "\$@"
EOF
chmod +x "$BIN"

# 부팅할 때 자동 실행 (Termux:Boot 앱을 설치한 경우에만 동작)
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
cat > "$BOOT_DIR/pokewatch.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
cd "$REPO"
python3 app.py --no-window --auto-collect 60 >> "\$HOME/pokewatch.log" 2>&1 &
EOF
chmod +x "$BOOT_DIR/pokewatch.sh"

echo
echo "▶ 설정"
if [ ! -f "$REPO/config.json" ]; then
  cd "$REPO" && python3 -m pokewatch init
  echo "  config.json 을 만들었습니다. 카페 주소를 채우세요:"
  echo "    nano config.json"
else
  echo "  config.json 이 이미 있습니다."
fi

cat <<'EOF'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
설치 완료

  1. 카페 설정        nano config.json
  2. 첫 수집          python3 -m pokewatch collect
  3. 앱 켜기          pokewatch
  4. 크롬에서 열기    http://localhost:8765
  5. 메뉴 → 홈 화면에 추가

이제 폰만으로 돌아갑니다. 컴퓨터도, 클라우드 계정도 필요 없습니다.
부팅할 때 자동으로 켜지게 하려면 Termux:Boot 앱을 설치하세요.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
