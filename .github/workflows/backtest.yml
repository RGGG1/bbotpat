# .github/workflows/run-backtest.yml
name: Run Backtest
on:
  workflow_dispatch:
    inputs:
      start:
        description: "Start date (YYYY-MM-DD)"
        required: false
        default: "2023-01-01"
      end:
        description: "End date (YYYY-MM-DD)"
        required: false
        default: ""
jobs:
  backtest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python -m pip install --upgrade pip requests pandas
      - name: Run
        run: |
          python backtest.py
