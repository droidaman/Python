"""Law 4: "Gold slippeth away from the man who invests it in businesses or
purposes with which he is not familiar." The circle of competence."""

from __future__ import annotations

from ..logic import LeafGate, Verdict
from ..principles import Principle


class SecurityTypeGate(LeafGate):
    """Ordinary shares only: no ETFs, preferreds, warrants, SPACs or other wrappers."""

    name = "security_type"
    principle = Principle.LAW_4

    def check(self, snap, ctx):
        kind = snap.profile.security_type
        allowed = kind in ctx.config.competence.allowed_security_types
        return Verdict(allowed, f"{kind!r} is {'an allowed' if allowed else 'not an allowed'} security type")


class ExchangeGate(LeafGate):
    """Major regulated exchanges only: no OTC or pink-sheet listings."""

    name = "exchange"
    principle = Principle.LAW_4

    def check(self, snap, ctx):
        exchange = snap.profile.exchange
        allowed = exchange in ctx.config.competence.allowed_exchanges
        return Verdict(allowed, f"listed on {exchange!r}" + ("" if allowed else ", which is not an allowed exchange"))


class CountryGate(LeafGate):
    """US companies only: US accounting, US courts, US disclosure rules."""

    name = "country"
    principle = Principle.LAW_4

    def check(self, snap, ctx):
        country = snap.profile.country
        allowed = country in ctx.config.competence.allowed_countries
        return Verdict(allowed, f"headquartered in {country!r}" + ("" if allowed else ", outside the allowed countries"))


class SectorGate(LeafGate):
    """Banks, insurers, REITs and utilities cannot be judged with the tools
    used here (EV/EBIT, current ratio, Altman Z), so they are left alone."""

    name = "sector"
    principle = Principle.LAW_4

    def check(self, snap, ctx):
        sector = (snap.profile.sector or "").strip()
        if not sector:
            return Verdict(False, "sector is unclassified")
        if sector in ctx.config.competence.excluded_sectors:
            return Verdict(False, f"{sector} is outside the circle of competence")
        return Verdict(True, f"{sector} is inside the circle of competence")


class EstablishedBusinessGate(LeafGate):
    """A real operating business with meaningful revenue, not a story."""

    name = "established_business"
    principle = Principle.LAW_4

    def check(self, snap, ctx):
        revenue = snap.latest.revenue
        minimum = ctx.config.competence.min_revenue
        return Verdict(revenue >= minimum, f"revenue {revenue:,.0f} vs minimum {minimum:,.0f}", {"revenue": revenue})
