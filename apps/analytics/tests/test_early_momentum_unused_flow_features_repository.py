from schurfer_analytics.early_momentum_unused_flow_features_repository import FEATURE_ROWS_SQL


def test_query_partitions_exact_source_series_and_versions() -> None:
    sql = str(FEATURE_ROWS_SQL)
    assert "b.exchange = c.source_exchange" in sql
    assert "b.market_type = c.market_type" in sql
    assert "b.symbol = c.source_native_id" in sql
    assert "b.capture_version = c.capture_version" in sql
    assert "b.universe_version = c.universe_version" in sql


def test_query_anchors_features_on_frozen_episode_bucket_not_trade_exit() -> None:
    sql = str(FEATURE_ROWS_SQL)
    assert "e.features->>'bucket_start'" in sql
    assert "c.decision_bucket - interval '120 minutes'" in sql
    assert "c.decision_bucket" in sql
    assert "bucket_start >= :bars_start" in sql
    assert "bucket_start < :cohort_end" in sql


def test_query_accepts_only_complete_paper_long_contract_economics() -> None:
    sql = str(FEATURE_ROWS_SQL)
    assert "e.contract_sha256 = decode(:contract_sha256_hex, 'hex')" in sql
    assert "t.accounting_status = 'complete'" in sql
    assert "t.accounting_version = :accounting_version" in sql
    assert "t.side = 'long'" in sql
    assert "t.setup_context->>'paper' = 'true'" in sql
