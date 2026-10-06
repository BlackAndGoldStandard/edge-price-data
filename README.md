# edge-price-data

Daily US stock closing prices derived from IEX historical (HIST) data,
published for [EdgeStockService](https://edgestockservice.onrender.com),
which charts the stock moves around disclosed trades by members of
Congress and executive-branch officials.

> **Data provided for free by IEX. By accessing or using IEX Historical
> Data, you agree to the [IEX Historical Data Terms of Use](https://www.iex.io/legal/hist-data-terms).**

## What's here

| Path | Contents |
|---|---|
| `closes/YYYY-MM-DD.csv` | One file per trading day: the closing price of every symbol that traded on IEX that day |
| `tools/` | The extractor that turns an IEX HIST file into a closes file |
| `.github/workflows/` | The scheduled job that runs the extractor each weekday |

Each closes file has a header row and these columns:

| Column | Meaning |
|---|---|
| `symbol` | Ticker as IEX reports it (e.g. `AAPL`, `BRK.B`) |
| `close` | Price of the last regular-session (9:30am-4:00pm ET) trade on IEX that day, in USD |
| `last_trade_utc` | Time of that trade, ISO 8601 UTC |

## How it's produced

A GitHub Actions job runs each weekday morning. It downloads the
previous trading day's IEX TOPS file from the HIST service (published
T+1), streams through it, keeps each symbol's last regular-session
trade, and commits the result as one small CSV. No other data source is
used.

## Limitations

- **IEX-only prices.** `close` is the last regular-session trade *on
  IEX*, not the official consolidated closing price (which is set by the
  listing exchange's 4:00pm closing auction). For liquid stocks it's
  typically within about 0.1%: on 2026-10-05, AAPL was $333.11 here vs a
  $332.89 official close, SPY $774.97 vs $774.83. Thinly traded stocks
  can differ more.
- **Gaps.** A symbol with no regular-session IEX trade that day is
  missing from that day's file. That's common for thinly traded stocks,
  and mutual funds never trade on IEX.
- **History.** IEX keeps HIST files for the trailing twelve months, so
  that's the furthest back this data can go.
- **No volume.** IEX volume is only a small share of total market volume,
  so it isn't published here to avoid it being mistaken for total volume.
- Provided as-is, with no warranty of accuracy or completeness. Not
  investment advice.

## Using this data

You may use and redistribute these files, provided you keep the IEX
attribution above, as the IEX Historical Data Terms of Use require.
