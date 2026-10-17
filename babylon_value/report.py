"""Self-contained HTML report for a backtest: headline metrics, growth of $1
against the benchmark, drawdowns, calendar-year returns and every rebalance."""

from __future__ import annotations

import html
import math
from datetime import date

from .backtest import BacktestResult
from .engine import RunStatus
from .orders import Side


def _pct(value, digits: int = 1) -> str:
    return f"{value:.{digits}%}" if isinstance(value, float) and math.isfinite(value) else "n/a"


def _num(value, digits: int = 2) -> str:
    return f"{value:,.{digits}f}" if isinstance(value, float) and math.isfinite(value) else "n/a"


def _index(returns) -> list[float]:
    level, out = 1.0, [1.0]
    for r in returns:
        level *= 1 + (r or 0.0)
        out.append(level)
    return out


def _drawdowns(levels: list[float]) -> list[float]:
    peak, out = levels[0], []
    for level in levels:
        peak = max(peak, level)
        out.append(level / peak - 1)
    return out


def _yearly(dates: list[date], returns) -> dict[int, float]:
    years: dict[int, float] = {}
    for day, r in zip(dates[1:], returns):
        if r is not None:
            years[day.year] = (1 + years.get(day.year, 0.0)) * (1 + r) - 1
    return years


def _line_chart(dates: list[date], series: list[tuple[str, list[float], str]], fmt, height: int = 260) -> str:
    width, left, right, top, bottom = 760, 56, 12, 12, 28
    values = [v for _, points, _ in series for v in points if math.isfinite(v)]
    low, high = min(values), max(values)
    if high == low:
        high = low + 1
    span = len(dates) - 1 or 1

    def x(i):
        return left + (width - left - right) * i / span

    def y(v):
        return top + (height - top - bottom) * (1 - (v - low) / (high - low))

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">']
    for k in range(5):
        v = low + (high - low) * k / 4
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                     f'<text x="{left - 6}" y="{y(v) + 4:.1f}" class="axis" text-anchor="end">{fmt(v)}</text>')
    years = sorted({d.year for d in dates})
    step = max(1, len(years) // 8)
    for year in years[::step]:
        i = next(i for i, d in enumerate(dates) if d.year == year)
        parts.append(f'<text x="{x(i):.1f}" y="{height - 8}" class="axis" text-anchor="middle">{year}</text>')
    for name, points, css in series:
        path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(points)
                        if math.isfinite(v))
        parts.append(f'<path d="{path}" class="{css}"><title>{html.escape(name)}</title></path>')
    parts.append("</svg>")
    legend = "".join(f'<span class="key {css}-key">{html.escape(name)}</span>' for name, _, css in series)
    return f'<div class="legend">{legend}</div>' + "".join(parts)


def render_html(result: BacktestResult, title: str, notes: list[str] | None = None,
                benchmark_name: str = "Benchmark") -> str:
    m = result.metrics
    strategy = _index(result.returns)
    has_benchmark = any(b is not None for b in result.benchmark_returns)
    series = [("Strategy", strategy, "s1")]
    if has_benchmark:
        series.append((benchmark_name, _index(result.benchmark_returns), "s2"))
    growth = _line_chart(result.dates, series, lambda v: f"{v:.2f}x")
    drawdown = _line_chart(result.dates, [("Strategy drawdown", _drawdowns(strategy), "s3")],
                           lambda v: f"{v:.0%}", height=180)

    tiles = [
        ("CAGR (time-weighted)", _pct(m.get("cagr"))),
        (f"{benchmark_name} CAGR", _pct(m.get("benchmark_cagr"))),
        ("Jensen's alpha, annual", _pct(m.get("alpha"))),
        ("Beta", _num(m.get("beta"))),
        ("Sharpe ratio", _num(m.get("sharpe"))),
        ("Max drawdown", _pct(m.get("max_drawdown"))),
        ("Volatility", _pct(m.get("volatility"))),
        ("Average cash", _pct(m.get("avg_cash_fraction"))),
    ]
    tile_html = "".join(f'<div class="tile"><div class="label">{html.escape(k)}</div><div class="value">{v}</div></div>'
                        for k, v in tiles)

    strategy_years = _yearly(result.dates, result.returns)
    bench_years = _yearly(result.dates, result.benchmark_returns) if has_benchmark else {}
    year_rows = "".join(
        f"<tr><td>{year}</td><td>{_pct(r)}</td><td>{_pct(bench_years.get(year))}</td>"
        f"<td>{_pct(r - bench_years[year]) if year in bench_years else 'n/a'}</td></tr>"
        for year, r in sorted(strategy_years.items()))

    rebalance_rows = []
    for report in result.reports:
        buys = sum(1 for f in report.fills if f.side is Side.BUY)
        sells = sum(1 for f in report.fills if f.side is Side.SELL)
        status = "halted" if report.status is RunStatus.HALTED else "completed"
        rebalance_rows.append(
            f"<tr><td>{report.as_of}</td><td>{status}</td><td>{report.universe_size}</td>"
            f"<td>{report.funnel.get('eligible', 0)}</td><td>{buys}</td><td>{sells}</td>"
            f"<td>{_num(report.nav_after, 0)}</td></tr>")

    note_html = "".join(f"<li>{html.escape(n)}</li>" for n in (notes or []))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1d2330; --muted:#5e6675; --line:#d9dce2; --tile:#fff; --s1:#2f6f9f; --s2:#b0763a; --s3:#a14848; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --fg:#e7e9ee; --muted:#9aa2b1; --line:#2c323c; --tile:#1b1f26;
  --s1:#6fa8d6; --s2:#d9a066; --s3:#d97b7b; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:880px; margin:0 auto; }}
h1 {{ font-size:24px; margin:0 0 4px; }} h2 {{ font-size:17px; margin:32px 0 8px; }}
.sub, .axis, .label, .key {{ color:var(--muted); }}
.notes {{ border-left:3px solid var(--s2); padding:8px 16px; margin:16px 0; background:var(--tile); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:8px; }}
.tile {{ background:var(--tile); border:1px solid var(--line); border-radius:6px; padding:10px 12px; }}
.label {{ font-size:12px; }} .value {{ font-size:20px; font-variant-numeric:tabular-nums; }}
.chart {{ width:100%; height:auto; }} .grid {{ stroke:var(--line); }} .axis {{ font-size:11px; fill:var(--muted); }}
path {{ fill:none; stroke-width:2; }} .s1 {{ stroke:var(--s1); }} .s2 {{ stroke:var(--s2); }} .s3 {{ stroke:var(--s3); }}
.legend {{ font-size:13px; }} .key::before {{ content:""; display:inline-block; width:14px; height:3px; margin:0 6px 3px 0; }}
.key {{ margin-right:16px; }} .s1-key::before {{ background:var(--s1); }} .s2-key::before {{ background:var(--s2); }}
.s3-key::before {{ background:var(--s3); }}
.scroll {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; font-size:14px; }}
th, td {{ text-align:right; padding:4px 8px; border-bottom:1px solid var(--line); }} th:first-child, td:first-child {{ text-align:left; }}
</style></head><body><main>
<h1>{html.escape(title)}</h1>
<div class="sub">{result.dates[0]} to {result.dates[-1]} · {len(result.returns)} months · {len(result.reports)} rebalances ·
config {result.config_fingerprint} · final NAV {_num(result.nav[-1], 0)} (contributions {_num(result.contributions, 0)})</div>
{f'<ul class="notes">{note_html}</ul>' if note_html else ''}
<h2>Headline results</h2><div class="tiles">{tile_html}</div>
<h2>Growth of $1 (time-weighted, net of costs)</h2>{growth}
<h2>Drawdown</h2>{drawdown}
<h2>Calendar years</h2><div class="scroll"><table><tr><th>Year</th><th>Strategy</th><th>{html.escape(benchmark_name)}</th>
<th>Difference</th></tr>{year_rows}</table></div>
<h2>Every rebalance</h2><div class="scroll"><table><tr><th>Date</th><th>Status</th><th>Universe</th><th>Eligible</th>
<th>Buys</th><th>Sells</th><th>NAV after</th></tr>{''.join(rebalance_rows)}</table></div>
<p class="sub">Costs paid {_num(result.costs_paid, 0)} · holdings delisted while held: {len(result.delistings)} ·
average holdings {_num(m.get('avg_holdings'), 1)}</p>
</main></body></html>
"""
