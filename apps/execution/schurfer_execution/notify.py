from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

_API = "https://api.telegram.org/bot{token}/sendMessage"
_SPECIAL = re.compile(r"[_*\[\]()~`>#+=|{}.!\\-]")


def _esc(s: str) -> str:
    return _SPECIAL.sub(lambda m: "\\" + m.group(), s)


def _fmt_pnl(pnl_pct: float) -> str:
    sign = "+" if pnl_pct >= 0 else ""
    return f"{sign}{pnl_pct:.2f}%"


async def _send(token: str, chat_id: str, text: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _API.format(token=token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
            )
            if resp.status_code != 200:
                log.warning("notify.send.failed", status=resp.status_code)
    except Exception as exc:
        log.warning("notify.send.error", err=str(exc))


async def notify_open(
    token: str,
    chat_id: str,
    *,
    base: str,
    exchange: str,
    size_usd: float,
    leverage: int,
    price: float,
    score: int,
    paper: bool,
) -> None:
    tag = "📄 PAPER" if paper else "⚡ TRADE"
    text = (
        f"*{_esc(tag)}: SHORT {_esc(base)}*\n"
        f"Exchange: {_esc(exchange)}\n"
        f"Entry: `{_esc(str(price))}`\n"
        f"Size: ${_esc(str(size_usd))} x{_esc(str(leverage))}\n"
        f"Score: {_esc(str(score))}"
    )
    await _send(token, chat_id, text)


async def notify_close(
    token: str,
    chat_id: str,
    *,
    base: str,
    exchange: str,
    entry_price: float,
    exit_price: float,
    pnl_pct: float,
    reason: str,
    paper: bool,
) -> None:
    tag = "📄 PAPER" if paper else "🏁 CLOSED"
    emoji = "✅" if pnl_pct >= 0 else "🔴"
    text = (
        f"*{_esc(tag)}: {base} {emoji}*\n"
        f"Exchange: {_esc(exchange)}\n"
        f"Entry→Exit: `{_esc(str(entry_price))}` → `{_esc(str(exit_price))}`\n"
        f"PnL: *{_esc(_fmt_pnl(pnl_pct))}*\n"
        f"Reason: {_esc(reason)}"
    )
    await _send(token, chat_id, text)


def credentials(cfg: Any) -> tuple[str, str] | None:
    token = getattr(cfg, "telegram_bot_token", None)
    chat_id = getattr(cfg, "telegram_chat_id", None)
    if token and chat_id:
        return str(token), str(chat_id)
    return None


def is_configured(cfg: Any) -> bool:
    return credentials(cfg) is not None
