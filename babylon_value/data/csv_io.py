"""Load and save market data as three CSV files in one directory.

securities.csv    ticker,name,sector,exchange,security_type,country
fundamentals.csv  ticker plus every FinancialPeriod field (dates as YYYY-MM-DD).
                  One row per report version; a restatement is a second row for
                  the same fiscal_year_end with a later available_date.
prices.csv        ticker,date,close,volume,dividend

An empty fundamental or price cell is loaded as NaN - never as zero - so the
integrity gates reject the security instead of trusting a guess. An empty
dividend cell means no dividend.
"""

from __future__ import annotations

import csv
import math
from dataclasses import fields
from datetime import date
from pathlib import Path

from ..models import FinancialPeriod, SecurityProfile
from .provider import InMemoryProvider, PriceBar

SECURITY_COLUMNS = ("ticker", "name", "sector", "exchange", "security_type", "country")
PRICE_COLUMNS = ("ticker", "date", "close", "volume", "dividend")
FUNDAMENTAL_COLUMNS = ("ticker",) + tuple(f.name for f in fields(FinancialPeriod))
_DATE_FIELDS = ("fiscal_year_end", "available_date")


def load_csv_directory(directory: str | Path) -> InMemoryProvider:
    directory = Path(directory)
    profiles = {}
    for row in _read(directory / "securities.csv", SECURITY_COLUMNS):
        profiles[row["ticker"]] = SecurityProfile(**{k: row[k].strip() for k in SECURITY_COLUMNS})

    fundamentals: dict[str, list[FinancialPeriod]] = {}
    for line, row in enumerate(_read(directory / "fundamentals.csv", FUNDAMENTAL_COLUMNS), start=2):
        values = {}
        for name in FUNDAMENTAL_COLUMNS[1:]:
            text = row[name]
            values[name] = _date(text, name, line) if name in _DATE_FIELDS else _number(text, name, line)
        fundamentals.setdefault(row["ticker"], []).append(FinancialPeriod(**values))

    prices: dict[str, list[PriceBar]] = {}
    for line, row in enumerate(_read(directory / "prices.csv", PRICE_COLUMNS), start=2):
        dividend = row["dividend"].strip()
        prices.setdefault(row["ticker"], []).append(PriceBar(
            date=_date(row["date"], "date", line),
            close=_number(row["close"], "close", line),
            volume=_number(row["volume"], "volume", line),
            dividend=_number(dividend, "dividend", line) if dividend else 0.0,
        ))
    return InMemoryProvider(profiles, fundamentals, prices)


def export_csv_directory(provider: InMemoryProvider, directory: str | Path) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "securities.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SECURITY_COLUMNS)
        for ticker in sorted(provider.profiles):
            profile = provider.profiles[ticker]
            writer.writerow([getattr(profile, column) for column in SECURITY_COLUMNS])
    with (directory / "fundamentals.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(FUNDAMENTAL_COLUMNS)
        for ticker in sorted(provider.fundamentals):
            for period in provider.fundamentals[ticker]:
                writer.writerow([ticker] + [_format(getattr(period, name)) for name in FUNDAMENTAL_COLUMNS[1:]])
    with (directory / "prices.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(PRICE_COLUMNS)
        for ticker in sorted(provider.bars):
            for bar in provider.bars[ticker]:
                writer.writerow([ticker, bar.date.isoformat(), _format(bar.close), _format(bar.volume),
                                 _format(bar.dividend)])


def _read(path: Path, required: tuple[str, ...]):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name} is missing column(s): {', '.join(missing)}")
        yield from reader


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
