name: Intraday KOSPI KOSDAQ Monitor

on:
  schedule:
    # 일~목 23:58 UTC = 월~금 08:58 KST. 09:00까지 코드가 대기합니다.
    - cron: "58 23 * * 0-4"
    # 월~금 04:28 UTC = 월~금 13:28 KST. 13:30까지 코드가 대기합니다.
    - cron: "28 4 * * 1-5"
  workflow_dispatch:
    inputs:
      session:
        description: "수동 실행할 감시 구간"
        required: true
        default: morning
        type: choice
        options:
          - morning
          - afternoon

permissions:
  contents: read

concurrency:
  group: intraday-market-monitor
  cancel-in-progress: true

env:
  TZ: Asia/Seoul
  KIS_APP_KEY: ${{ secrets.KIS_APP_KEY }}
  KIS_APP_SECRET: ${{ secrets.KIS_APP_SECRET }}
  TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
  TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
  NAVER_CLIENT_ID: ${{ secrets.NAVER_CLIENT_ID }}
  NAVER_CLIENT_SECRET: ${{ secrets.NAVER_CLIENT_SECRET }}
  DART_API_KEY: ${{ secrets.DART_API_KEY }}
  INTRADAY_SCAN_SECONDS: "30"
  INTRADAY_CONFIRM_SCANS: "3"
  INTRADAY_ALERT_COOLDOWN_MINUTES: "15"

jobs:
  morning-session:
    if: >-
      (github.event_name == 'schedule' && github.event.schedule == '58 23 * * 0-4') ||
      (github.event_name == 'workflow_dispatch' && inputs.session == 'morning')
    runs-on: ubuntu-latest
    timeout-minutes: 75
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -r requirements.txt
      - name: Monitor 09:00-10:00 KST
        run: python intraday_monitor.py --until 10:00 --state runtime/intraday_state.json

  afternoon-session:
    if: >-
      (github.event_name == 'schedule' && github.event.schedule == '28 4 * * 1-5') ||
      (github.event_name == 'workflow_dispatch' && inputs.session == 'afternoon')
    runs-on: ubuntu-latest
    timeout-minutes: 105
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -r requirements.txt
      - name: Monitor 13:30-15:00 KST
        run: python intraday_monitor.py --until 15:00 --state runtime/intraday_state.json
