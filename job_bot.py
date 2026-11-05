name: 라이팅 구인 알림 봇

on:
  schedule:
    - cron: "*/30 * * * *"   # 30분마다 확인
  workflow_dispatch:          # 직접 실행 버튼
    inputs:
      report:
        description: "점검 결과도 휴대폰으로 받기"
        type: boolean
        default: true

permissions:
  contents: write

concurrency:
  group: job-bot
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install requests beautifulsoup4

      - name: 공고 확인
        env:
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
          REPORT_MODE: ${{ github.event_name == 'workflow_dispatch' && inputs.report }}
        run: python job_bot.py

      - name: 기록 저장
        run: |
          git config user.name "job-bot"
          git config user.email "job-bot@users.noreply.github.com"
          git add seen.json
          git diff --cached --quiet || (git commit -m "기록 업데이트" && git push)

      - name: 자동 실행 유지
        # 50일 넘게 기록이 없으면 작은 신호 파일을 남겨서
        # GitHub이 60일 규칙으로 자동 실행을 끄지 않게 함
        run: |
          last=$(git log -1 --format=%ct)
          now=$(date +%s)
          days=$(( (now - last) / 86400 ))
          echo "마지막 기록 후 ${days}일 지남"
          if [ "$days" -ge 50 ]; then
            date -u +"%Y-%m-%d %H:%M UTC" > keepalive.txt
            git add keepalive.txt
            git commit -m "자동 실행 유지" && git push
          fi
