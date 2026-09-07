"""Versioned asset-class classification for scanned exchange instruments.

The broad multi-exchange scanner admits any active `/USDT:USDT` ticker and
records `market_type=swap` with no asset class, so LBank 24H stock futures such
as DJT, LYTE and PURR entered pump cohorts as extreme crypto pumps when their
fresh 24-hour baseline initialized (2026-08-25 production audit, ENG-018). The
Bybit and Binance momentum collectors already classify and fail closed on
non-crypto instruments; this is the same contract for the scanner's wider
universe.

What the venues actually expose, measured over public `load_markets` on
2026-09-07 across their USDT linear universes:

| venue   | instruments | class field                                          |
| ------- | ----------: | ---------------------------------------------------- |
| bybit   |         755 | `symbolType`: "", innovation, stock, commodity, ETF   |
| xt      |        1054 | `tags`: null, COMMODITY, FOREX, INDEX, METAL          |
| mexc    |        1063 | `type`: 1 / 2, semantics undocumented                 |
| gate    |         986 | `type`: always "direct"                               |
| bingx   |        1126 | none                                                  |
| bitmart |        1187 | none                                                  |
| lbank   |         822 | none                                                  |

So a single shared field does not exist. Only bybit and xt can be classified
from venue metadata at all; on LBank, DJT is structurally indistinguishable
from BTC apart from incidental leverage and price-limit differences, which are
correlates, not declarations, and must not be used as evidence.

Everything else is therefore `unknown`, and `unknown` is recorded rather than
guessed. Two different unknowns are distinguished by `source`: the venue
exposes no class field at all, or it exposes one whose value this version does
not know how to map. Both stay observable in research universes and both fail
closed for crypto strategies, which is what the ENG-018 contract requires.

The curated table below is the interim second evidence source for venues with
no class field. Every entry carries its own provenance and confidence, and a
`reported` entry is never presented as venue-verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CLASSIFIER_VERSION = "asset_class_v1"

CRYPTO = "crypto"
TOKENIZED_EQUITY = "tokenized_equity"
COMMODITY = "commodity"
INDEX = "index"
FOREX = "forex"
LEVERAGED_PRODUCT = "leveraged_product"
UNKNOWN = "unknown"

#: Every class this version can produce. A consumer that wants only crypto must
#: check for CRYPTO explicitly rather than excluding a list it knows today.
KNOWN_CLASSES = frozenset(
    {CRYPTO, TOKENIZED_EQUITY, COMMODITY, INDEX, FOREX, LEVERAGED_PRODUCT, UNKNOWN}
)

#: Evidence is built from venue-controlled values, so its length is bounded
#: here rather than being left to whatever a venue sends. The column is TEXT
#: (migration 0045), so this is not what prevents a write failure -- it stops
#: a pathological payload from bloating every row and the logs with it. The
#: marker makes a truncated value obviously truncated rather than quietly
#: wrong when someone later re-decides a classification from it.
MAX_EVIDENCE_LENGTH = 512
_TRUNCATION_MARKER = "...[truncated]"

SOURCE_VENUE_FIELD_ABSENT = "venue_field_absent"
SOURCE_VENUE_VALUE_UNMAPPED = "venue_value_unmapped"

CONFIDENCE_VENUE = "venue"
CONFIDENCE_CURATED_REPORTED = "curated_reported"
CONFIDENCE_CURATED_VERIFIED = "curated_verified"


@dataclass(frozen=True)
class AssetClassification:
    """The class, and exactly what it was derived from.

    `evidence` is the raw venue value or curated key that produced the class,
    kept verbatim so a later classifier version can re-decide without
    re-fetching, and so a wrong mapping is auditable rather than invisible.
    """

    asset_class: str
    source: str
    evidence: str | None
    confidence: str | None
    version: str = CLASSIFIER_VERSION


def _bounded_evidence(evidence: str) -> str:
    if len(evidence) <= MAX_EVIDENCE_LENGTH:
        return evidence
    keep = MAX_EVIDENCE_LENGTH - len(_TRUNCATION_MARKER)
    return evidence[:keep] + _TRUNCATION_MARKER


@dataclass(frozen=True)
class CuratedEntry:
    asset_class: str
    provenance: str
    confidence: str


# Interim evidence for venues that expose no class field. Keyed by
# (exchange, base asset), because the same base can be a different product on
# different venues -- PURR is a crypto token elsewhere while LBank's PURR
# contract prices near 12.59 USDT, which is one reason these entries are
# venue-scoped and marked reported rather than verified.
#
# An entry may only claim CONFIDENCE_CURATED_VERIFIED once its provenance is an
# official venue listing announcement or contract specification naming the
# instrument's class. Until then it stays `curated_reported`: enough to keep the
# instrument out of a crypto cohort, not enough to assert what it is.
CURATED_CLASSES: dict[tuple[str, str], CuratedEntry] = {
    ("lbank", "DJT"): CuratedEntry(
        asset_class=TOKENIZED_EQUITY,
        provenance=(
            "2026-08-25 production audit: appeared as an extreme crypto pump when its "
            "fresh 24h baseline initialized; LBank exposes no class field. Not yet "
            "confirmed against an official LBank listing announcement."
        ),
        confidence=CONFIDENCE_CURATED_REPORTED,
    ),
    ("lbank", "LYTE"): CuratedEntry(
        asset_class=TOKENIZED_EQUITY,
        provenance=(
            "2026-08-25 production audit, same cohort as DJT; LBank exposes no class "
            "field. Not yet confirmed against an official LBank listing announcement."
        ),
        confidence=CONFIDENCE_CURATED_REPORTED,
    ),
    ("lbank", "PURR"): CuratedEntry(
        asset_class=TOKENIZED_EQUITY,
        provenance=(
            "2026-08-25 production audit, same cohort as DJT; LBank exposes no class "
            "field. The base symbol collides with an unrelated crypto token traded "
            "elsewhere, so this entry is venue-scoped and unverified."
        ),
        confidence=CONFIDENCE_CURATED_REPORTED,
    ),
}

# Bybit's own instrument field. "" is a standard crypto perpetual and
# `innovation` is Bybit's newer-listing crypto tier: this is the same mapping
# apps/collector/internal/bybit/bybit.go already applies to the momentum
# universe, kept identical on purpose. `ETF` is observed in live data but its
# meaning for a USDT linear perpetual is not verified, so it maps to unknown
# with the raw value recorded rather than to a guessed class.
_BYBIT_SYMBOL_TYPE: dict[str, str] = {
    "": CRYPTO,
    "innovation": CRYPTO,
    "stock": TOKENIZED_EQUITY,
    "commodity": COMMODITY,
}

# XT tags the non-crypto products and leaves crypto untagged, the same shape as
# Bybit's empty symbolType. METAL is folded into commodity; a tag list carrying
# several tags is resolved by this precedence, most specific first.
_XT_TAGS: tuple[tuple[str, str], ...] = (
    ("FOREX", FOREX),
    ("INDEX", INDEX),
    ("METAL", COMMODITY),
    ("COMMODITY", COMMODITY),
)


def _bybit(info: dict[str, Any]) -> AssetClassification | None:
    if "symbolType" not in info:
        return None
    raw = str(info.get("symbolType") or "").strip()
    evidence = _bounded_evidence(f"bybit.symbolType={raw}")
    mapped = _BYBIT_SYMBOL_TYPE.get(raw.lower() if raw else "")
    if mapped is None:
        return AssetClassification(
            asset_class=UNKNOWN,
            source=SOURCE_VENUE_VALUE_UNMAPPED,
            evidence=evidence,
            confidence=CONFIDENCE_VENUE,
        )
    return AssetClassification(
        asset_class=mapped,
        source="venue.bybit.symbolType",
        evidence=evidence,
        confidence=CONFIDENCE_VENUE,
    )


def _xt(info: dict[str, Any]) -> AssetClassification | None:
    if "tags" not in info:
        return None
    raw = info.get("tags")
    tags = [str(tag).strip().upper() for tag in raw] if isinstance(raw, list) else []
    evidence = _bounded_evidence(f"xt.tags={sorted(tags)}" if tags else "xt.tags=[]")
    for tag, mapped in _XT_TAGS:
        if tag in tags:
            return AssetClassification(
                asset_class=mapped,
                source="venue.xt.tags",
                evidence=evidence,
                confidence=CONFIDENCE_VENUE,
            )
    if tags:
        return AssetClassification(
            asset_class=UNKNOWN,
            source=SOURCE_VENUE_VALUE_UNMAPPED,
            evidence=evidence,
            confidence=CONFIDENCE_VENUE,
        )
    return AssetClassification(
        asset_class=CRYPTO,
        source="venue.xt.tags",
        evidence=evidence,
        confidence=CONFIDENCE_VENUE,
    )


_VENUE_ADAPTERS = {"bybit": _bybit, "xt": _xt}


def classify(exchange: str, base_asset: str, market: dict[str, Any] | None) -> AssetClassification:
    """Classify one scanned instrument.

    Venue evidence wins over the curated table: the venue is authoritative about
    its own product, and the curated table exists only for venues that say
    nothing. A venue value this version cannot map does NOT fall through to the
    curated table either -- an unmapped value is a gap in this classifier that
    must be visible, not something a hand-maintained list quietly papers over.
    """
    market = market or {}
    info = market.get("info")
    info = info if isinstance(info, dict) else {}

    adapter = _VENUE_ADAPTERS.get(exchange)
    if adapter is not None:
        classified = adapter(info)
        if classified is not None:
            return classified

    curated = CURATED_CLASSES.get((exchange, base_asset.upper()))
    if curated is not None:
        return AssetClassification(
            asset_class=curated.asset_class,
            source="curated",
            evidence=f"{exchange}:{base_asset.upper()}",
            confidence=curated.confidence,
        )

    return AssetClassification(
        asset_class=UNKNOWN,
        source=SOURCE_VENUE_FIELD_ABSENT,
        evidence=None,
        confidence=None,
    )


def is_crypto(classification: AssetClassification) -> bool:
    """The only admission test a crypto strategy or cohort should use.

    Positive, not a denylist: an instrument counts as crypto when something
    actually said so. Unknown fails closed here while staying fully observable
    in research universes, which is the ENG-018 contract.
    """
    return classification.asset_class == CRYPTO
