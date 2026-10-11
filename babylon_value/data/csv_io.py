"""Load and save a dataset as CSV files in one directory.

Required:
  securities.csv     ticker,name,sector,exchange,security_type,country
  fundamentals.csv   ticker plus every FinancialPeriod field (dates as YYYY-MM-DD).
                     One row per report version; a restatement is a second row for
                     the same fiscal_year_end with a later available_date.
  prices.csv         ticker,date,close,volume,dividend

Optional:
  splits.csv         ticker,ex_date,ratio            (2.0 = 2-for-1, 0.1 = 1-for-10)
  shares.csv         ticker,as_of,available_date,shares   (e.g. 10-Q cover counts)
  quality_flags.csv  ticker,start,end,reason         (empty end = still open)
  dataset.json       metadata: purpose, price basis, sources, build date

An empty fundamental or price cell is loaded as NaN - never as zero - so the
integrity gates reject the security instead of trusting a guess. An empty
dividend cell means no dividend.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import fields
from datetime import date
from pathlib import Path

from ..models import FinancialPeriod, SecurityProfile
from .provider import InMemoryProvider, PriceBar, QualityFlag, ShareCount, Split

SECURITY_COLUMNS = ("ticker", "name", "sector", "exchange", "security_type", "country")
PRICE_COLUMNS = ("ticker", "date", "close", "volume", "dividend")
FUNDAMENTAL_COLUMNS = ("ticker",) + tuple(f.name for f in fields(FinancialPeriod))
SPLIT_COLUMNS = ("ticker", "ex_date", "ratio")
SHARE_COLUMNS = ("ticker", "as_of", "available_date", "shares")
FLAG_COLUMNS = ("ticker", "start", "end", "reason")
_DATE_FIELDS = ("fiscal_year_end", "available_date")


def load_csv_directory(directory: str | Path) -> InMemoryProvider:
    directory = Path(directory)
    profiles = {}
    for _, row in _read(directory / "securities.csv", SECURITY_COLUMNS):
        profiles[row["ticker"]] = SecurityProfile(**{k: row[k].strip() for k in SECURITY_COLUMNS})

    fundamentals: dict[str, list[FinancialPeriod]] = {}
    for line, row in _read(directory / "fundamentals.csv", FUNDAMENTAL_COLUMNS):
        values = {}
        for name in FUNDAMENTAL_COLUMNS[1:]:
            text = row[name]
            values[name] = _date(text, name, line) if name in _DATE_FIELDS else _number(text, name, line)
        fundamentals.setdefault(row["ticker"], []).append(FinancialPeriod(**values))

    prices: dict[str, list[PriceBar]] = {}
    for line, row in _read(directory / "prices.csv", PRICE_COLUMNS):
        dividend = row["dividend"].strip()
        prices.setdefault(row["ticker"], []).append(PriceBar(
            date=_date(row["date"], "date", line),
            close=_number(row["close"], "close", line),
            volume=_number(row["volume"], "volume", line),
            dividend=_number(dividend, "dividend", line) if dividend else 0.0,
        ))

    splits: dict[str, list[Split]] = {}
    for line, row in _read_optional(directory / "splits.csv", SPLIT_COLUMNS):
        ratio = _number(row["ratio"], "ratio", line)
        if not ratio > 0:
            raise ValueError(f"splits.csv line {line}: ratio must be a positive number")
        splits.setdefault(row["ticker"], []).append(Split(_date(row["ex_date"], "ex_date", line), ratio))

    share_counts: dict[str, list[ShareCount]] = {}
    for line, row in _read_optional(directory / "shares.csv", SHARE_COLUMNS):
        share_counts.setdefault(row["ticker"], []).append(ShareCount(
            _date(row["as_of"], "as_of", line), _date(row["available_date"], "available_date", line),
            _number(row["shares"], "shares", line)))

    flags: dict[str, list[QualityFlag]] = {}
    for line, row in _read_optional(directory / "quality_flags.csv", FLAG_COLUMNS):
        end = row["end"].strip()
        flags.setdefault(row["ticker"], []).append(QualityFlag(
            _date(row["start"], "start", line), _date(end, "end", line) if end else None, row["reason"]))

    metadata_path = directory / "dataset.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    return InMemoryProvider(profiles, fundamentals, prices, splits=splits, share_counts=share_counts,
                            quality_flags=flags, metadata=metadata)


def export_csv_directory(provider: InMemoryProvider, directory: str | Path) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "securities.csv", SECURITY_COLUMNS,
           ([getattr(provider.profiles[t], c) for c in SECURITY_COLUMNS] for t in sorted(provider.profiles)))
    _write(directory / "fundamentals.csv", FUNDAMENTAL_COLUMNS,
           ([t] + [_format(getattr(p, name)) for name in FUNDAMENTAL_COLUMNS[1:]]
            for t in sorted(provider.fundamentals) for p in provider.fundamentals[t]))
    _write(directory / "prices.csv", PRICE_COLUMNS,
           ([t, b.date.isoformat(), _format(b.close), _format(b.volume), _format(b.dividend)]
            for t in sorted(provider.bars) for b in provider.bars[t]))
    _write(directory / "splits.csv", SPLIT_COLUMNS,
           ([t, s.ex_date.isoformat(), _format(s.ratio)]
            for t in sorted(provider.split_events) for s in provider.split_events[t]))
    _write(directory / "shares.csv", SHARE_COLUMNS,
           ([t, c.as_of.isoformat(), c.available_date.isoformat(), _format(c.shares)]
            for t in sorted(provider.share_counts) for c in provider.share_counts[t]))
    _write(directory / "quality_flags.csv", FLAG_COLUMNS,
           ([t, f.start.isoformat(), f.end.isoformat() if f.end else "", f.reason]
            for t in sorted(provider.quality_flags) for f in provider.quality_flags[t]))
    (directory / "dataset.json").write_text(json.dumps(provider.metadata, indent=2, sort_keys=True, default=str) + "\n",
                                            encoding="utf-8")


def _write(path: Path, header, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _read(path: Path, required: tuple[str, ...]):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name} is missing column(s): {', '.join(missing)}")
        yield from enumerate(reader, start=2)


def _read_optional(path: Path, required: tuple[str, ...]):
    if path.exists():
        yield from _read(path, required)


def _number(text: str, name: str, line: int) -> float:
    text = (text or "").strip()
    if not text:
        return math.nan
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"line {line}: {name} value {text!r} is not a number") from None


def _date(text: str, name: str, line: int) -> date:
    try:
        return date.fromisoformat((text or "").strip())
    except ValueError:
        raise ValueError(f"line {line}: {name} value {text!r} is not a YYYY-MM-DD date") from None


def _format(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return ""
    return repr(float(value)) if isinstance(value, float) else str(value)
