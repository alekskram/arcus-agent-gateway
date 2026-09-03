"""Static sector map for Robinhood Chain (Arcus) tokenized equities.

`SECTORS` maps 13 sector names -> token symbol lists. The map was
validated against the live /assets snapshot of 2026-09-03: all 194 live
symbols are covered, none extra (source of truth:
/tmp/mec54/sector_map_final.json - regenerate rather than hand-edit).

`sector_of(symbol)` classifies any symbol: the matched sector, 'Other'
for symbols on the residual bucket, and 'Unknown' when the symbol is not
in the map at all (fresh listings) - never a guess.
"""
from __future__ import annotations

SECTORS: dict[str, list[str]] = {
    "Tech": [
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "NFLX",
    "ORCL", "CRM", "ADBE", "INTU", "NOW", "SNOW",
    "DDOG", "MDB", "NET", "PANW", "CRWD", "FTNT",
    "ZS", "WDAY", "SHOP", "TEAM", "PATH", "RBLX",
    "TTWO", "SNAP", "APP", "PLTR", "IBM", "HPE",
    "DELL", "SMCI", "ANET", "CIEN", "CSCO", "JBL",
    "CLS", "FICO", "TER", "WDC", "CRWV", "CTSH",
    "DOCN", "NBIS", "INOD", "PENG", "SOUN", "BB",
    "RDDT", "OUST",
    ],
    "Semiconductors": [
    "NVDA", "AMD", "AVGO", "QCOM", "INTC", "MU",
    "MRVL", "MPWR", "ON", "TSM", "ASML", "AMAT",
    "LRCX", "KLAC", "ALAB", "CRDO", "ONTO", "AXTI",
    "SIMO", "TSEM", "UMC", "AMKR", "MTSI", "MXL",
    "SNDK", "AEHR", "AEIS", "COHR", "AAOI", "POET",
    "QBTS", "QUBT", "IONQ", "RGTI", "FIX", "AMBA",
    "LITE", "NVTS",
    ],
    "Auto/Mobility": [
    "TSLA", "RIVN", "F", "JOBY", "AUR",
    ],
    "Finance/Fintech": [
    "NU", "SOFI", "FUTU", "FIG", "P", "NAVN",
    "CRCL", "FISV", "INFQ",
    ],
    "Crypto/Digital Assets": [
    "MSTR", "GLXY", "BULL", "COIN",
    ],
    "Healthcare": [
    "JNJ", "PFE", "LLY", "UNH", "HIMS", "TEM",
    "MRNA", "IBRX", "ABCL", "CLOV",
    ],
    "Energy/Power": [
    "XOM", "VST", "RUN", "BE", "GEV", "POWL",
    "MOD", "OKLO", "SMR", "NNE", "WULF", "IREN",
    "APLD", "FLNC", "CLSK", "SATS", "CEG", "PR",
    "VRT",
    ],
    "Defense/Aerospace/Industrial": [
    "LMT", "HII", "KTOS", "BA", "GE", "AXON",
    "AVAV", "LHX", "GLW", "HWM", "PWR", "RCAT",
    "SKHY", "UPS", "VICR",
    ],
    "Space": [
    "RKLB", "ASTS", "LUNR", "SPCX", "RDW", "PL",
    ],
    "Consumer/Retail": [
    "COST", "LULU", "ELF", "KSS", "GME", "AMC",
    "DJT", "ZM", "CVNA", "TTD", "BABA", "CCL",
    "CELH", "FLY",
    ],
    "ETF/Index": [
    "SPY", "QQQ", "XLK", "SMH", "SOXX", "SPMO",
    "VTI", "SCHD", "BND", "SHY", "SGOV", "GLD",
    "SLV", "USO", "INDA", "EWY", "EWT", "TE",
    "USAR",
    ],
    "Media/Telecom": [
    "CBRS", "VSAT",
    ],
    "Other": [
    "SLS", "WYFI", "XNDU",
    ],
}

# Reverse index built once at import: SYMBOL -> sector name (first
# sector in SECTORS order wins for any hypothetical duplicate; the
# validated map has none).
_SYMBOL_TO_SECTOR: dict[str, str] = {
    sym: name
    for name, syms in SECTORS.items()
    for sym in syms
}


def sector_of(symbol: str) -> str:
    """Sector name for a tokenSymbol, case-insensitive.

    Returns the mapped sector; 'Other' is the residual bucket for live
    tokens that fit no named sector; 'Unknown' when the symbol is not
    in the map at all. Callers that must distinguish "not categorized"
    from "not a token" should validate with api.asset() first.
    """
    return _SYMBOL_TO_SECTOR.get((symbol or "").strip().upper(), "Unknown")


def sector_symbols(name: str) -> list[str]:
    """Symbols of one sector (case-insensitive name); [] if no such sector."""
    want = (name or "").strip().lower()
    for sec, syms in SECTORS.items():
        if sec.lower() == want:
            return list(syms)
    return []
