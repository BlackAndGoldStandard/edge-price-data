"""Turn IEX HIST (TOPS) files into small daily closing-price CSVs.

    python tools/iex_closes.py extract 2026-10-05        # -> closes/2026-10-05.csv
    python tools/iex_closes.py plan --limit 5            # JSON list of dates to extract
    python tools/iex_closes.py index                     # -> closes/index.json

A day's TOPS file is ~10 GB compressed, so `extract` streams it straight
from IEX - download, gunzip, and pcapng parsing all happen in one pass,
nothing is written to disk but the output CSV - and stops reading once the
regular session is over.

"Close" is each symbol's last regular-session (9:30am-4:00pm ET) trade on
IEX: not the official consolidated close (see README). Standard library
only, so the workflow needs no dependencies.

Data provided for free by IEX. By accessing or using IEX Historical Data,
you agree to the IEX Historical Data Terms of Use
(https://www.iex.io/legal/hist-data-terms).
"""
import argparse
import datetime as dt
import gzip
import io
import json
import struct
import sys
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HIST_URL = "https://iextrading.com/api/1.0/hist"
USER_AGENT = "edge-price-data (+https://github.com/BlackAndGoldStandard/edge-price-data)"
NEW_YORK = ZoneInfo("America/New_York")
CLOSES_DIR = Path(__file__).resolve().parent.parent / "closes"

# IEX keeps HIST files for the trailing twelve months.
HISTORY_DAYS = 366
# Keep reading this long past the 4:00pm close before stopping, so trades
# executed just before the bell but sent a little later aren't missed.
CLOSE_GRACE_NS = 10 * 60 * 1_000_000_000

# pcapng block types (https://www.ietf.org/archive/id/draft-tuexen-opsawg-pcapng-05.html)
ENHANCED_PACKET_BLOCK = 6
ETHERTYPE_IPV4 = 0x0800

# IEX-TP header: 40 bytes, messages follow, each prefixed by a u16 length.
IEXTP_HEADER = struct.Struct("<BBHIIHHqqq")  # version .. send_time
IEXTP_HEADER_LEN = 40

# TOPS 1.6 message types and trade sale-condition flags.
MSG_TRADE_REPORT = 0x54  # 'T'
MSG_TRADE_BREAK = 0x42  # 'B'
FLAG_EXTENDED_HOURS = 0x40
FLAG_ODD_LOT = 0x20
TRADE = struct.Struct("<q8sIqq")  # timestamp, symbol, size, price, trade id (after type+flags)


class NoFileForDate(Exception):
    """IEX has no TOPS file for that date (not a trading day, or not posted yet)."""


def _get_json(url: str, attempts: int = 4):
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - retry any network hiccup
            if attempt == attempts:
                raise
            print(f"  {url}: {exc}; retrying", file=sys.stderr)
            time.sleep(10 * attempt)


def tops_link(date: dt.date) -> str:
    files = _get_json(f"{HIST_URL}?date={date:%Y%m%d}") or []
    tops = [f for f in files if f.get("feed") == "TOPS"]
    if not tops:
        raise NoFileForDate(f"no TOPS file listed for {date}")
    # Newest protocol version first, in case IEX ever lists more than one.
    tops.sort(key=lambda f: tuple(int(p) for p in f["version"].split(".")), reverse=True)
    if tops[0]["version"] != "1.6":
        print(f"  warning: TOPS version {tops[0]['version']}, parser written for 1.6", file=sys.stderr)
    return tops[0]["link"]


def _session_bounds_ns(date: dt.date) -> tuple[int, int]:
    def ns(hour, minute):
        return int(dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=NEW_YORK).timestamp()) * 1_000_000_000

    return ns(9, 30), ns(16, 0)


def _fmt_price(price: int) -> str:
    # TOPS prices are fixed-point with 4 implied decimals; format exactly.
    whole, frac = divmod(price, 10_000)
    return f"{whole}.{frac:04d}".rstrip("0").rstrip(".") if frac else str(whole)


def extract_closes(date: dt.date, log_every_s: float = 60.0) -> dict[str, tuple[int, int]]:
    """{symbol: (price, trade_timestamp_ns)} for each symbol's last
    regular-session, last-sale-eligible trade on IEX that day."""
    open_ns, close_ns = _session_bounds_ns(date)
    stop_ns = close_ns + CLOSE_GRACE_NS
    last: dict[bytes, tuple[int, int, int]] = {}  # symbol -> (ts, price, trade_id)

    req = urllib.request.Request(tops_link(date), headers={"User-Agent": USER_AGENT})
    started = next_log = time.time()
    packets = 0
    with urllib.request.urlopen(req, timeout=120) as resp:
        stream = io.BufferedReader(gzip.GzipFile(fileobj=resp), buffer_size=1 << 20)
        read = stream.read
        unpack_from = struct.unpack_from
        trade_from = TRADE.unpack_from
        while True:
            head = read(8)
            if len(head) < 8:
                break
            block_type, block_len = unpack_from("<II", head)
            body = read(block_len - 8)
            if len(body) < block_len - 8:
                break  # truncated file
            if block_type != ENHANCED_PACKET_BLOCK:
                continue
            (cap_len,) = unpack_from("<I", body, 12)
            pkt = memoryview(body)[20 : 20 + cap_len]
            if len(pkt) < 14 or unpack_from(">H", pkt, 12)[0] != ETHERTYPE_IPV4:
                continue
            p = 14 + (pkt[14] & 0x0F) * 4 + 8  # Ethernet + IPv4 (IHL) + UDP
            if len(pkt) < p + IEXTP_HEADER_LEN:
                continue
            *_, msg_count, _stream_offset, _first_seq, send_time = IEXTP_HEADER.unpack_from(pkt, p)
            packets += 1
            if send_time > stop_ns:
                break  # regular session (plus grace) is over; skip after-hours

            off = p + IEXTP_HEADER_LEN
            for _ in range(msg_count):
                (msg_len,) = unpack_from("<H", pkt, off)
                mtype = pkt[off + 2]
                if mtype == MSG_TRADE_REPORT:
                    flags = pkt[off + 3]
                    ts, sym, _size, price, trade_id = trade_from(pkt, off + 4)
                    if open_ns <= ts < close_ns and not flags & (FLAG_EXTENDED_HOURS | FLAG_ODD_LOT):
                        prev = last.get(sym)
                        if prev is None or ts >= prev[0]:
                            last[sym] = (ts, price, trade_id)
                elif mtype == MSG_TRADE_BREAK:
                    _ts, sym, _size, _price, trade_id = trade_from(pkt, off + 4)
                    if sym in last and last[sym][2] == trade_id:
                        # The day's last trade was broken. The previous one
                        # isn't kept, so leave a gap rather than a wrong close.
                        del last[sym]
                off += 2 + msg_len

            if time.time() >= next_log:
                next_log = time.time() + log_every_s
                print(f"  {date}: {packets:,} packets, {len(last):,} symbols, "
                      f"{time.time() - started:,.0f}s", file=sys.stderr)

    print(f"  {date}: done - {len(last):,} symbols in {time.time() - started:,.0f}s", file=sys.stderr)
    return {sym.rstrip(b" ").decode(): (price, ts) for sym, (ts, price, _id) in last.items()}


def write_csv(date: dt.date, closes: dict[str, tuple[int, int]], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{date.isoformat()}.csv"
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        f.write("symbol,close,last_trade_utc\n")
        for symbol in sorted(closes):
            price, ts = closes[symbol]
            when = dt.datetime.fromtimestamp(ts / 1e9, dt.timezone.utc).isoformat(timespec="seconds")
            f.write(f"{symbol},{_fmt_price(price)},{when.replace('+00:00', 'Z')}\n")
    tmp.replace(path)  # never leave a half-written file behind
    return path


def missing_dates(closes_dir: Path, limit: int, history_days: int = HISTORY_DAYS) -> list[str]:
    """Dates IEX has a TOPS file for, within its retention window, that
    have no closes file yet - newest first, so a capped run (or a slow
    backfill) fills in the most useful days first."""
    index = _get_json(HIST_URL) or {}
    cutoff = dt.date.today() - dt.timedelta(days=history_days)
    have = {p.stem for p in closes_dir.glob("*.csv")}
    dates = []
    for key, files in index.items():
        day = dt.datetime.strptime(key, "%Y%m%d").date()
        if day >= cutoff and any(f.get("feed") == "TOPS" for f in files) and day.isoformat() not in have:
            dates.append(day.isoformat())
    return sorted(dates, reverse=True)[:limit]


def write_index(closes_dir: Path) -> Path:
    """closes/index.json - the available dates, so a consumer can find the
    files without listing the directory through the GitHub API."""
    closes_dir.mkdir(parents=True, exist_ok=True)
    dates = sorted(p.stem for p in closes_dir.glob("*.csv"))
    path = closes_dir / "index.json"
    path.write_text(json.dumps({
        "dates": dates,
        "attribution": "Data provided for free by IEX. By accessing or using IEX Historical "
                       "Data, you agree to the IEX Historical Data Terms of Use.",
        "terms": "https://www.iex.io/legal/hist-data-terms",
    }, indent=1) + "\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract", help="extract one day's closes")
    ex.add_argument("date", type=dt.date.fromisoformat)
    ex.add_argument("--out-dir", type=Path, default=CLOSES_DIR)
    pl = sub.add_parser("plan", help="print a JSON list of dates still to extract")
    pl.add_argument("--limit", type=int, default=5)
    pl.add_argument("--closes-dir", type=Path, default=CLOSES_DIR)
    ix = sub.add_parser("index", help="rewrite closes/index.json")
    ix.add_argument("--closes-dir", type=Path, default=CLOSES_DIR)
    args = parser.parse_args(argv)

    if args.command == "extract":
        try:
            closes = extract_closes(args.date)
        except NoFileForDate as exc:
            print(exc, file=sys.stderr)
            return 2
        if not closes:
            print(f"no regular-session trades found for {args.date}", file=sys.stderr)
            return 1
        print(write_csv(args.date, closes, args.out_dir))
    elif args.command == "plan":
        print(json.dumps(missing_dates(args.closes_dir, args.limit)))
    elif args.command == "index":
        print(write_index(args.closes_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
