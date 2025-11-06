name: Backtest 44x (since 2023-01-01)

on:
  workflow_dispatch:

jobs:
  backtest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - run: pip install --quiet requests
      - name: Run analyzer (unbuffered)
        run: python -u .github/analyze_44x_trades.py
