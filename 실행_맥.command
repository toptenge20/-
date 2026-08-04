#!/bin/bash
# 맥에서 더블클릭으로 실행합니다.
# 처음 한 번은 마우스 오른쪽 클릭 → '열기' 를 눌러야 할 수 있습니다.
cd "$(dirname "$0")"
exec python3 app.py "$@"
