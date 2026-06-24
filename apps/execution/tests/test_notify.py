"""Tests for notify.py — Telegram message formatting and gating."""

from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution.notify import _esc as _escape
from schurfer_execution.notify import is_configured, notify_close, notify_open


def _cfg(*, token: str | None = "tok", chat: str | None = "123") -> MagicMock:  # noqa: S107
    cfg = MagicMock()
    cfg.telegram_bot_token = token
    cfg.telegram_chat_id = chat
    return cfg


# --- _escape ---


def test_escape_leaves_plain_text_unchanged() -> None:
    assert _escape("hello world") == "hello world"


def test_escape_backslashes_special_chars() -> None:
    # MarkdownV2 specials: _ * [ ] ( ) ~ ` # + = | { } . ! \ -
    result = _escape("price: 1.5 (profit!)")
    assert "\\." in result
    assert "\\(" in result
    assert "\\)" in result
    assert "\\!" in result


def test_escape_handles_empty_string() -> None:
    assert _escape("") == ""


# --- is_configured ---


def test_is_configured_true_when_both_set() -> None:
    assert is_configured(_cfg()) is True


def test_is_configured_false_when_token_missing() -> None:
    assert is_configured(_cfg(token=None)) is False


def test_is_configured_false_when_chat_missing() -> None:
    assert is_configured(_cfg(chat=None)) is False


def test_is_configured_false_when_both_missing() -> None:
    assert is_configured(_cfg(token=None, chat=None)) is False


# --- notify_open ---


async def test_notify_open_sends_message() -> None:
    with patch("schurfer_execution.notify.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_client_cls.return_value = mock_client

        await notify_open(
            "tok",
            "123",
            base="BEAT",
            exchange="bybit",
            size_usd=50.0,
            leverage=3,
            price=0.00239,
            score=8,
            paper=False,
        )

    mock_client.post.assert_called_once()
    payload = mock_client.post.call_args.kwargs["json"]
    assert payload["chat_id"] == "123"
    assert "BEAT" in payload["text"]
    assert "bybit" in payload["text"]


async def test_notify_open_includes_paper_tag() -> None:
    with patch("schurfer_execution.notify.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_client_cls.return_value = mock_client

        await notify_open(
            "tok",
            "123",
            base="BEAT",
            exchange="bybit",
            size_usd=50.0,
            leverage=3,
            price=0.00239,
            score=8,
            paper=True,
        )

    text = mock_client.post.call_args.kwargs["json"]["text"]
    assert "PAPER" in text


# --- notify_close ---


async def test_notify_close_sends_message_with_pnl() -> None:
    with patch("schurfer_execution.notify.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_client_cls.return_value = mock_client

        await notify_close(
            "tok",
            "123",
            base="BEAT",
            exchange="bybit",
            entry_price=0.0030,
            exit_price=0.0025,
            pnl_pct=16.7,
            reason="take_profit pnl=16.7%",
            paper=False,
        )

    text = mock_client.post.call_args.kwargs["json"]["text"]
    assert "BEAT" in text
    assert "bybit" in text
