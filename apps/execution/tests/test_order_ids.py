import pytest
from schurfer_execution import order_ids


def test_entry_id_is_32_hex_and_derived_ids_fit_the_bybit_limit() -> None:
    entry = order_ids.new_entry_client_order_id()

    assert order_ids.is_entry_client_order_id(entry)
    longest = order_ids.close_client_order_id(entry, 999)
    assert len(longest) <= order_ids.ORDER_LINK_ID_MAX_LENGTH
    assert order_ids.stop_client_order_id(entry, 1) == f"{entry}s1"
    assert order_ids.close_client_order_id(entry, 2) == f"{entry}c2"
    assert order_ids.stop_client_order_id(entry, 1).startswith(order_ids.stop_prefix(entry))
    assert order_ids.close_client_order_id(entry, 1).startswith(order_ids.close_prefix(entry))


@pytest.mark.parametrize("entry", ["", "f" * 31, "F" * 32, "a1b2c3d4-e5f6-7a8b-9c0d-e1f2a3b4c5d6"])
def test_ids_derive_only_from_a_32_hex_entry(entry: str) -> None:
    with pytest.raises(ValueError, match="32 lowercase hex"):
        order_ids.close_client_order_id(entry, 1)


@pytest.mark.parametrize("sequence", [0, 1000])
def test_sequence_is_bounded(sequence: int) -> None:
    with pytest.raises(ValueError, match="sequence"):
        order_ids.stop_client_order_id("a" * 32, sequence)
