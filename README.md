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
| `sec/` | Quarterly and annual financials from SEC filings for the companies in `sec/universe.txt` (see "SEC financials" below) |
| `tools/` | The extractors: `iex_closes.py` (closes) and `sec_facts.py` (SEC financials) |
| `.github/workflows/` | The scheduled jobs: `closes.yml` each weekday, `sec.yml` daily |

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

## SEC financials (`sec/`)

Financial statement figures (revenue, income, cash flow, cash, debt,
equity, share counts) from each company's 10-Q and 10-K filings since
2019, taken from the SEC's XBRL company facts. SEC filings are public
domain. The `sec.yml` job runs daily and only re-downloads a company when
it has filed a new 10-Q/10-K (or amendment), so most days change nothing.

| Path | Contents |
|---|---|
| `sec/universe.txt` | Tickers to cover, one per line (hand-maintained) |
| `sec/companies.csv` | `cik,ticker,name,sic,sic_description,last_accn,last_filed`: one row per company, with its SIC industry code and latest 10-Q/10-K |
| `sec/facts/<CIK>.csv` | `tag,unit,start,end,value,form,fy,fp,accn,filed`: one row per reported figure (CIK zero-padded to 10 digits) |
| `sec/manifest.json` | Each company's latest filing and row count, so readers download only what changed |

- `tag` is the XBRL tag; cover-page tags have a `dei:` prefix. `start` is
  empty for point-in-time figures (balance sheet, share counts).
- Cash-flow figures in 10-Qs are **year-to-date**, as filed.
- When a period was reported more than once (amendments, or prior-year
  comparatives in a later filing), only the latest-filed value is kept.
- Companies with several share classes (e.g. META, BRK) don't report total
  shares outstanding in this data; use the weighted-average share counts.
- Changing this format means changing EdgeStockService's reader too
  (its issue #27).

**Setup:** the job needs a `SEC_CONTACT_EMAIL` repository secret, because
the SEC requires a contact email in every request's User-Agent. It keeps
under the SEC's 10 requests/second limit and stops if the SEC answers 403
or 429. To run it locally:
`SEC_CONTACT_EMAIL=you@example.com python tools/sec_facts.py run --only META --out /tmp/sec`

## Using this data

You may use and redistribute these files, provided you keep the IEX
attribution above, as the IEX Historical Data Terms of Use require.
