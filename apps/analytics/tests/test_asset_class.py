"""ENG-018: the broad scanner admitted any active /USDT:USDT ticker with no
asset class, so LBank 24H stock futures entered pump cohorts as crypto pumps.

The venue payload shapes below are the real ones observed over public
load_markets on 2026-09-07, not invented fixtures.
"""

from __future__ import annotations

import pytest
from schurfer_analytics.asset_class import (
    CLASSIFIER_VERSION,
    COMMODITY,
    CONFIDENCE_CURATED_REPORTED,
    CONFIDENCE_VENUE,
    CRYPTO,
    CURATED_CLASSES,
    FOREX,
    INDEX,
    KNOWN_CLASSES,
    SOURCE_VENUE_FIELD_ABSENT,
    SOURCE_VENUE_VALUE_UNMAPPED,
    TOKENIZED_EQUITY,
    UNKNOWN,
    classify,
    is_crypto,
)


def _market(info: dict[str, object] | None) -> dict[str, object]:
    return {"info": info} if info is not None else {}


class TestBybitVenueEvidence:
    @pytest.mark.parametrize(
        ("symbol_type", "expected"),
        [
            ("", CRYPTO),
            ("innovation", CRYPTO),
            ("stock", TOKENIZED_EQUITY),
            ("commodity", COMMODITY),
        ],
    )
    def test_maps_the_venue_field(self, symbol_type: str, expected: str) -> None:
        result = classify("bybit", "BTC", _market({"symbolType": symbol_type}))
        assert result.asset_class == expected
        assert result.source == "venue.bybit.symbolType"
        assert result.confidence == CONFIDENCE_VENUE
        assert result.evidence == f"bybit.symbolType={symbol_type}"
        assert result.version == CLASSIFIER_VERSION

    def test_unmapped_value_is_unknown_with_the_raw_value_kept(self) -> None:
        """ETF is present in live Bybit data. Its meaning for a USDT linear
        perpetual is not verified, so it must not be guessed into a class."""
        result = classify("bybit", "SOMETHING", _market({"symbolType": "ETF"}))
        assert result.asset_class == UNKNOWN
        assert result.source == SOURCE_VENUE_VALUE_UNMAPPED
        assert result.evidence == "bybit.symbolType=ETF"


class TestXtVenueEvidence:
    @pytest.mark.parametrize(
        ("tags", "expected"),
        [
            (None, CRYPTO),
            ([], CRYPTO),
            (["COMMODITY"], COMMODITY),
            (["METAL"], COMMODITY),
            (["METAL", "COMMODITY"], COMMODITY),
            (["FOREX"], FOREX),
            (["INDEX"], INDEX),
        ],
    )
    def test_maps_the_tag_list(self, tags: list[str] | None, expected: str) -> None:
        result = classify("xt", "BTC", _market({"tags": tags}))
        assert result.asset_class == expected
        assert result.confidence == CONFIDENCE_VENUE

    def test_unrecognized_tag_is_unknown_not_crypto(self) -> None:
        result = classify("xt", "BTC", _market({"tags": ["SOMETHING_NEW"]}))
        assert result.asset_class == UNKNOWN
        assert result.source == SOURCE_VENUE_VALUE_UNMAPPED


class TestVenuesWithoutAnyClassField:
    """bingx, bitmart, gate, mexc and lbank expose nothing usable. A structural
    lookalike is not evidence: on LBank, DJT differs from BTC only by leverage
    and price-limit width, which are correlates of the product, not a
    declaration of its class."""

    @pytest.mark.parametrize("exchange", ["bingx", "bitmart", "gate", "lbank"])
    def test_unknown_and_says_why(self, exchange: str) -> None:
        result = classify(exchange, "SOMECOIN", _market({"baseCurrency": "SOMECOIN"}))
        assert result.asset_class == UNKNOWN
        assert result.source == SOURCE_VENUE_FIELD_ABSENT
        assert result.evidence is None

    def test_lbank_btc_is_not_promoted_to_crypto_by_lookalike_fields(self) -> None:
        lbank_btc = {
            "baseCurrency": "BTC",
            "defaultLeverage": 20.0,
            "maxLeverage": 200,
            "priceLimitUpperValue": 0.05,
        }
        assert classify("lbank", "BTC", _market(lbank_btc)).asset_class == UNKNOWN

    def test_missing_market_is_unknown_not_a_crash(self) -> None:
        assert classify("lbank", "BTC", None).asset_class == UNKNOWN


class TestCuratedTable:
    def test_curated_entry_classifies_a_venue_that_says_nothing(self) -> None:
        lbank_djt = {"baseCurrency": "DJT", "defaultLeverage": 1.0, "maxLeverage": 50}
        result = classify("lbank", "DJT", _market(lbank_djt))
        assert result.asset_class == TOKENIZED_EQUITY
        assert result.source == "curated"
        assert result.confidence == CONFIDENCE_CURATED_REPORTED

    def test_curated_lookup_is_case_insensitive_on_the_base(self) -> None:
        assert classify("lbank", "djt", _market({})).asset_class == TOKENIZED_EQUITY

    def test_curated_entries_are_venue_scoped(self) -> None:
        """PURR is an unrelated crypto token on other venues. A curated entry
        must never leak across exchanges."""
        assert classify("bitmart", "PURR", _market({})).asset_class == UNKNOWN

    def test_every_curated_entry_carries_provenance_and_a_known_class(self) -> None:
        assert CURATED_CLASSES
        for (exchange, base), entry in CURATED_CLASSES.items():
            assert exchange and base == base.upper()
            assert entry.asset_class in KNOWN_CLASSES
            assert len(entry.provenance) > 40, (exchange, base)

    def test_venue_evidence_wins_over_the_curated_table(self) -> None:
        """A venue is authoritative about its own product, and an unmapped
        venue value must stay visible rather than being papered over by a
        hand-maintained list."""
        assert classify("bybit", "DJT", _market({"symbolType": "stock"})).source == (
            "venue.bybit.symbolType"
        )


class TestCryptoAdmission:
    def test_only_a_positive_crypto_classification_admits(self) -> None:
        assert is_crypto(classify("bybit", "BTC", _market({"symbolType": ""})))
        assert not is_crypto(classify("lbank", "BTC", _market({})))
        assert not is_crypto(classify("bybit", "X", _market({"symbolType": "stock"})))
        assert not is_crypto(classify("bybit", "X", _market({"symbolType": "ETF"})))
