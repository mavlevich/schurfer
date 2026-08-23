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


def _fmt_usd(pnl_usd: float) -> str:
    sign = "+" if pnl_usd >= 0 else "-"
    return f"{sign}${abs(pnl_usd):.2f}"


async def _send(token: str, chat_id: str, text: str) -> bool:
    """Best-effort by design (never raises -- a Telegram outage can't be
    allowed to break a trade open/close) but now reports whether delivery
    actually succeeded, so a caller that needs to know (e.g. an alert that
    must be retried, not silently marked delivered) can act on it. Callers
    that are fully fire-and-forget (notify_open/notify_close) simply don't
    look at the return value."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _API.format(token=token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
            )
            if resp.status_code != 200:
                log.warning("notify.send.failed", status=resp.status_code)
                return False
            return True
    except Exception as exc:
        log.warning("notify.send.error", err=str(exc))
        return False


async def notify_open(
    token: str,
    chat_id: str,
    *,
    side: str = "short",
    strategy: str = "unknown",
    base: str,
    exchange: str,
    size_usd: float,
    leverage: int,
    price: float,
    score: int,
    paper: bool,
) -> None:
    tag = "📄 PAPER" if paper else "⚡ TRADE"
    margin_usd = size_usd / leverage if leverage else None
    size_line = f"Notional: ${_esc(str(size_usd))} x{_esc(str(leverage))}"
    if margin_usd is not None:
        size_line += f" \\(margin ≈ ${_esc(f'{margin_usd:.2f}')}\\)"
    text = (
        f"*{_esc(tag)}: {_esc(side.upper())} {_esc(base)}*\n"
        f"Strategy: {_esc(strategy)}\n"
        f"Exchange: {_esc(exchange)}\n"
        f"Entry: `{_esc(str(price))}`\n"
        f"{size_line}\n"
        f"Score: {_esc(str(score))}"
    )
    await _send(token, chat_id, text)


async def notify_close(
    token: str,
    chat_id: str,
    *,
    base: str,
    exchange: str,
    side: str = "short",
    strategy: str = "unknown",
    entry_price: float,
    exit_price: float,
    pnl_pct: float,
    reason: str,
    paper: bool,
    pnl_kind: str = "gross",
    pnl_usd: float | None = None,
    gross_pnl_pct: float | None = None,
    size_usd: float | None = None,
    margin_usd: float | None = None,
    accounting_status: str | None = None,
    fees_usd: float | None = None,
    funding_usd: float | None = None,
    slippage_usd: float | None = None,
) -> None:
    tag = "📄 PAPER" if paper else "🏁 CLOSED"
    emoji = "✅" if pnl_pct >= 0 else "🔴"
    pnl_label = "Modeled net PnL" if pnl_kind == "modeled_net" else "Gross PnL"
    # pnl_usd is None when the position size behind this close could not be
    # resolved (e.g. an old position opened before size tracking existed, or
    # an evicted Redis key) — show the percent alone rather than fabricate a
    # dollar figure.
    pnl_value = (
        f"*{_esc(_fmt_pnl(pnl_pct))}* \\({_esc(_fmt_usd(pnl_usd))}\\)"
        if pnl_usd is not None
        else f"*{_esc(_fmt_pnl(pnl_pct))}*"
    )
    lines = [
        f"*{_esc(tag)}: {_esc(side.upper())} {base} {emoji}*",
        f"Strategy: {_esc(strategy)}",
        f"Exchange: {_esc(exchange)}",
        f"Entry→Exit: `{_esc(str(entry_price))}` → `{_esc(str(exit_price))}`",
    ]
    if size_usd is not None:
        size_line = f"Notional: ${_esc(str(size_usd))}"
        if margin_usd is not None:
            size_line += f" \\(margin ≈ ${_esc(f'{margin_usd:.2f}')}\\)"
        lines.append(size_line)
    if gross_pnl_pct is not None and pnl_kind == "modeled_net":
        lines.append(f"Gross PnL: {_esc(_fmt_pnl(gross_pnl_pct))}")
    lines.append(f"{_esc(pnl_label)}: {pnl_value}")
    if fees_usd is not None or funding_usd is not None or slippage_usd is not None:
        parts = []
        if fees_usd is not None:
            parts.append(f"fees {_esc(_fmt_usd(-fees_usd))}")
        if funding_usd is not None:
            parts.append(f"funding {_esc(_fmt_usd(-funding_usd))}")
        if slippage_usd is not None:
            parts.append(f"slippage {_esc(_fmt_usd(-slippage_usd))}")
        lines.append(f"Costs: {', '.join(parts)}")
    if accounting_status is not None:
        lines.append(f"Accounting: {_esc(accounting_status)}")
    lines.append(f"Reason: {_esc(reason)}")
    await _send(token, chat_id, "\n".join(lines))


async def notify_alert(token: str, chat_id: str, *, text: str) -> bool:
    """Returns whether Telegram actually accepted the message -- callers
    that must not silently drop a failed alert (see early_momentum.py's
    _maybe_alert) check this instead of assuming delivery just because no
    exception propagated."""
    return await _send(token, chat_id, f"*⚠️ ALERT*\n{_esc(text)}")


def credentials(cfg: Any) -> tuple[str, str] | None:
    token = getattr(cfg, "telegram_bot_token", None)
    chat_id = getattr(cfg, "telegram_chat_id", None)
    if token and chat_id:
        return str(token), str(chat_id)
    return None


def is_configured(cfg: Any) -> bool:
    return credentials(cfg) is not None
