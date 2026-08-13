# telegram-bot

Status: inactive legacy scaffold.

The active Telegram sender is the Go service in [`apps/notifier`](../notifier/), and
the target durable delivery design is
[`docs/contracts/notification-delivery-v1.md`](../../docs/contracts/notification-delivery-v1.md).
Do not implement new notification behavior in this directory.

Python service that sends alerts to Telegram and accepts
approve/skip actions on suggested trades.

Historical note: this planned service was never activated.
