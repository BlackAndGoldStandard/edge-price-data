"""Turn SEC EDGAR company facts into small per-company CSVs for EdgeStockService.

    SEC_CONTACT_EMAIL=you@example.com python tools/sec_facts.py run
    SEC_CONTACT_EMAIL=you@example.com python tools/sec_facts.py run --only META AAPL --out /tmp/sec

For each ticker in sec/universe.txt it looks up the company's CIK, checks
the SEC's list of its filings, and only when there's a 10-Q/10-K (or
amendment) newer than last time downloads its XBRL company facts and keeps
what the app's metrics need. Output, all under sec/ (the format the app's
issue #27 reads - change both together):

    companies.csv          cik,ticker,name,sic,sic_description,last_accn,last_filed
    facts/<CIK10>.csv      tag,unit,start,end,value,form,fy,fp,accn,filed
    manifest.json          {"generated_at": ..., "companies": {CIK10: {ticker, last_accn, rows}}}

SEC fair-access rules (https://www.sec.gov/os/accessing-edgar-data): a
User-Agent naming the app and a contact email, and under 10 requests a
second. A 403 or 429 means the SEC has blocked or throttled us: stop and
report it, never work around it. SEC filings are public domain.

Standard library only, so the workflow needs no dependencies.
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_DIR = Path(__file__).resolve().parent.parent / "sec"

# 5 requests a second: half the SEC's limit.
REQUEST_GAP_S = 0.2
EARLIEST_END = "2019-01-01"
FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A"}

# The line items the app's metrics are built from (EdgeStockService issue
# #28). Several tags per item because companies report the same thing
# under different names; the app picks per company.
US_GAAP_TAGS = {
    # Income statement
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "EarningsPerShareDiluted",
    "CommonStockDividendsPerShareDeclared",
    # Cash flow statement (year-to-date in 10-Qs)
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "FinanceLeasePrincipalPayments",
    "ShareBasedCompensation",
    # Balance sheet (point in time)
    "CashAndCashEquivalentsAtCarryingValue",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "LongTermDebtCurrent",
    "ShortTermBorrowings",
    "CommercialPaper",
    "LongTermDebtNoncurrent",
    "LongTermDebt",
    "StockholdersEquity",
    # Share counts. Companies with several share classes (META, BRK) report
    # shares outstanding per class, which company facts leave out, so the
    # weighted averages (every company, every quarter) are the fallback.
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
}
DEI_TAGS = {"EntityCommonStockSharesOutstanding"}

COMPANY_FIELDS = ["cik", "ticker", "name", "sic", "sic_description", "last_accn", "last_filed"]
FACT_FIELDS = ["tag", "unit", "start", "end", "value", "form", "fy", "fp", "accn", "filed"]


class Blocked(Exception):
    """The SEC answered 403/429: stop the whole run, don't retry around it."""


class Client:
    """Rate-limited SEC requests with the required User-Agent."""

    def __init__(self, email: str):
        self.user_agent = f"EdgeStockService {email}"
        self._last = 0.0

    def get_json(self, url: str):
        wait = self._last + REQUEST_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise Blocked(f"SEC returned {exc.code} for {url}") from exc
            raise


def cik10(cik) -> str:
    return str(int(cik)).zfill(10)


def sec_ticker(ticker: str) -> str:
    """The SEC writes share classes with a dash (BRK-B); disclosures use a dot."""
    return ticker.strip().upper().replace(".", "-")


def read_universe(path: Path) -> list[str]:
    tickers = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line.upper() not in tickers:
            tickers.append(line.upper())
    return tickers


def latest_report(submissions: dict) -> tuple[str, str] | tuple[None, None]:
    """(accession number, filing date) of the newest 10-Q/10-K or amendment.
    `filings.recent` is newest first."""
    recent = submissions.get("filings", {}).get("recent", {})
    for accn, form, filed in zip(recent.get("accessionNumber", []), recent.get("form", []), recent.get("filingDate", [])):
        if form in FORMS:
            return accn, filed
    return None, None


def extract_facts(company_facts: dict) -> list[dict]:
    """The rows to keep: wanted tags, 10-Q/10-K forms, periods ending 2019 or
    later, and for each (tag, unit, start, end) only the latest-filed value,
    so an amendment replaces what it amends."""
    latest: dict[tuple, dict] = {}
    for taxonomy, wanted, prefix in (("us-gaap", US_GAAP_TAGS, ""), ("dei", DEI_TAGS, "dei:")):
        for tag, fact in company_facts.get("facts", {}).get(taxonomy, {}).items():
            if tag not in wanted:
                continue
            for unit, values in fact.get("units", {}).items():
                for v in values:
                    if v.get("form") not in FORMS or v.get("end", "") < EARLIEST_END:
                        continue
                    row = {
                        "tag": prefix + tag,
                        "unit": unit,
                        "start": v.get("start", ""),
                        "end": v["end"],
                        "value": v["val"],
                        "form": v["form"],
                        "fy": v.get("fy") or "",
                        "fp": v.get("fp") or "",
                        "accn": v["accn"],
                        "filed": v["filed"],
                    }
                    key = (row["tag"], unit, row["start"], row["end"])
                    kept = latest.get(key)
                    if kept is None or (row["filed"], row["accn"]) > (kept["filed"], kept["accn"]):
                        latest[key] = row
    return sorted(latest.values(), key=lambda r: (r["tag"], r["end"], r["start"], r["unit"]))


def read_companies(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {row["cik"]: row for row in csv.DictReader(f)}


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def count_rows(path: Path) -> int:
    with path.open() as f:
        return max(sum(1 for _ in f) - 1, 0)


def run(email: str, out: Path, only: list[str] | None) -> int:
    client = Client(email)
    facts_dir = out / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    universe = read_universe(SEC_DIR / "universe.txt")
    if only:
        universe = [t.upper() for t in only]
    previous = read_companies(out / "companies.csv")

    by_ticker = {e["ticker"]: e for e in client.get_json(TICKERS_URL).values()}
    companies: dict[str, dict] = {}
    errors: list[str] = []
    try:
        for ticker in universe:
            entry = by_ticker.get(sec_ticker(ticker))
            if entry is None:
                errors.append(f"{ticker}: not in the SEC's ticker list")
                continue
            cik = cik10(entry["cik_str"])
            if cik in companies:  # e.g. GOOG and GOOGL are one company
                print(f"{ticker}: same company as {companies[cik]['ticker']}, skipped")
                continue
            try:
                submissions = client.get_json(SUBMISSIONS_URL.format(cik=cik))
                accn, filed = latest_report(submissions)
                company = {
                    "cik": cik,
                    "ticker": ticker,
                    "name": submissions.get("name") or entry["title"],
                    "sic": submissions.get("sic", ""),
                    "sic_description": submissions.get("sicDescription", ""),
                    "last_accn": accn or "",
                    "last_filed": filed or "",
                }
                facts_path = facts_dir / f"{cik}.csv"
                old = previous.get(cik)
                if old and old["last_accn"] == company["last_accn"] and facts_path.exists():
                    print(f"{ticker}: no new 10-Q/10-K since {company['last_filed']}, skipped")
                else:
                    rows = extract_facts(client.get_json(FACTS_URL.format(cik=cik)))
                    write_csv(facts_path, FACT_FIELDS, rows)
                    print(f"{ticker}: {len(rows)} facts through {company['last_filed']}")
                companies[cik] = company
            except Blocked:
                raise
            except Exception as exc:  # noqa: BLE001 - one company never stops the rest
                errors.append(f"{ticker}: {exc}")
                if cik in previous:  # keep last good data rather than dropping it
                    companies[cik] = previous[cik]
    except Blocked as exc:
        errors.append(f"STOPPED: {exc}")
        # Keep everything not yet reached as it was.
        for cik, old in previous.items():
            companies.setdefault(cik, old)

    if not only:
        # Companies no longer in the universe: remove their files.
        for path in facts_dir.glob("*.csv"):
            if path.stem not in companies:
                path.unlink()
    else:
        for cik, old in previous.items():
            companies.setdefault(cik, old)

    ordered = sorted(companies.values(), key=lambda c: c["ticker"])
    write_csv(out / "companies.csv", COMPANY_FIELDS, ordered)
    listed = {
        c["cik"]: {
            "ticker": c["ticker"],
            "last_accn": c["last_accn"],
            "rows": count_rows(facts_dir / f"{c['cik']}.csv") if (facts_dir / f"{c['cik']}.csv").exists() else 0,
        }
        for c in ordered
    }
    # Only touch the manifest when what it lists changed, so a run with
    # nothing new commits nothing (a fresh timestamp alone isn't a change).
    manifest_path = out / "manifest.json"
    old_listed = json.loads(manifest_path.read_text()).get("companies") if manifest_path.exists() else None
    if listed != old_listed:
        manifest = {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "companies": listed}
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")

    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="update sec/ for every ticker in sec/universe.txt")
    p.add_argument("--only", nargs="+", metavar="TICKER", help="just these tickers (for testing)")
    p.add_argument("--out", type=Path, default=SEC_DIR, help="output folder (default: sec/)")
    args = parser.parse_args()

    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if "@" not in email:
        print("Set SEC_CONTACT_EMAIL: the SEC requires a contact email in the User-Agent.", file=sys.stderr)
        return 2
    return run(email, args.out, args.only)


if __name__ == "__main__":
    sys.exit(main())
