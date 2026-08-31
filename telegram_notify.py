#!/usr/bin/env python3
"""
telegram_notify.py — Kirim notifikasi event bot ke Telegram (OPSIONAL).

Baca ENV:
    TELEGRAM_BOT_TOKEN   token dari @BotFather (wajib kalau mau notif)
    TELEGRAM_CHAT_ID     chat_id lo (wajib kalau mau notif)

Kalau salah satu kosong -> semua fungsi jadi NO-OP (bot tetap jalan normal,
cuma gak kirim Telegram). Aman untuk paper mode.

Cara dapat token:
    1. Chat @BotFather di Telegram -> /newbot -> copy token.
    2. Chat @raw_data_bot (atau forward pesan ke bot lo) -> dapat chat_id lo.
    3. Isi ke .env:
        TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
        TELEGRAM_CHAT_ID=158566033
"""
import os
import httpx

# Load .env so TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are picked up even when
# this module is run directly (python telegram_notify.py) without importing config.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_ENABLED = bool(BOT_TOKEN and CHAT_ID)
_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage" if _ENABLED else ""


def notify(text: str, parse_mode: str = "HTML") -> bool:
    """Kirim 1 pesan ke Telegram. Return True kalau terkirim, False kalau no-op/gagal.

    Also emits a `tg` event to the live dashboard telemetry bus (when the
    dashboard is running) so Telegram alerts show up in the UI feed too.
    """
    # Push to dashboard regardless of whether Telegram itself is enabled.
    try:
        from telemetry import tel
        import asyncio
        asyncio.get_event_loop().create_task(
            tel.emit({"type": "tg", "text": text[:4000], "ts": __import__("time").time()})
        )
    except Exception:
        pass
    if not _ENABLED:
        return False
    # Telegram batasi 4096 char; potong kalau kepanjangan
    text = text[:4000]
    try:
        r = httpx.post(
            _API,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode,
                  "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def enabled() -> bool:
    return _ENABLED


if __name__ == "__main__":
    # test lokal
    if enabled():
        ok = notify("🔧 farcaster-yapper / solana-bot notifier test OK")
        print("sent:", ok)
    else:
        print("Telegram NOT configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID kosong) -> no-op")
