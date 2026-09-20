name: Weekday Morning Stock Briefing

on:
  schedule:
    # GitHub cron은 UTC. 일~목 22:50 UTC = 월~금 07:50 KST.
    - cron: "50 22 * * 0-4"
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: morning-stock-briefing
  cancel-in-progress: false

jobs:
  send-briefing:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      TZ: Asia/Seoul
      TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
      TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      KRX_ID: ${{ secrets.KRX_ID }}
      KRX_PW: ${{ secrets.KRX_PW }}
      STRICT_DATA_VALIDATION: "true"
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Send briefing
        run: python main.py
