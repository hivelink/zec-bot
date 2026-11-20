#!/usr/bin/env python3
"""Bot Telegram pemantau harga Zcash (ZEC) untuk target jual hasil mining.

Fitur:
- Cek harga ZEC berkala (CoinGecko, cadangan Binance)
- Alert bertahap (Tahap 1/2/3) dan stop-loss, dengan cooldown + reset otomatis
- Perintah Telegram: /cek, /status, /reset, /help (dibalas cepat lewat long-polling)

Konfigurasi lewat environment variable (lihat .env.example).
"""
import html
import json
import logging
import os
import signal
import threading
import time

import requests

# ================= KONFIGURASI UTAMA =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # detik
COOLDOWN_SECONDS = int(float(os.getenv("COOLDOWN_HOURS", "6")) * 3600)
STATE_FILE = os.getenv("STATE_FILE", "alert_state.json")
FALLBACK_USD_IDR = float(os.getenv("FALLBACK_USD_IDR", "16850"))  # kurs jika CoinGecko limit
RETREAT_PCT = 0.04  # reset otomatis jika harga menjauh 4% dari titik target

# 4 Parameter Target Jual ZEC (Hasil Mining)
TARGETS = [
    {
        "id": "tahap_1",
        "condition": "above",
        "price": 875.0,
        "title": "TARGET TAHAP 1 TERCAPAI (Tutup OPEX Listrik)",
        "action": "Jual 25%-30% porsi ZEC di zona US$875 - US$945 untuk mengamankan kas operasional rig.",
    },
    {
        "id": "tahap_2",
        "condition": "above",
        "price": 1000.0,
        "title": "TARGET TAHAP 2 TERCAPAI (Kunci Profit Utama)",
        "action": "Jual 35%-40% porsi ZEC di zona US$1.000 - US$1.050 jika momentum BTC ikut menguat.",
    },
    {
        "id": "tahap_3",
        "condition": "above",
        "price": 1280.0,
        "title": "TARGET TAHAP 3 TERCAPAI (Moonbag)",
        "action": "Pertimbangkan menjual sisa 20%-30% porsi ZEC di area US$1.280+.",
    },
    {
        "id": "stop_loss",
        "condition": "below",
        "price": 700.0,
        "title": "PERINGATAN BATAS PENGAMAN (Stop-Loss US$700)",
        "action": "Harga ZEC menembus ke bawah US$700. Evaluasi penutupan harian dan amankan porsi biaya listrik.",
    },
]
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("zec-bot")

_state_lock = threading.Lock()
_stop = threading.Event()


# ---------------------- State ----------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("last_triggered", {})
            data.setdefault("last_update_id", 0)
            return data
        except Exception as e:
            log.warning("State rusak, dibuat ulang: %s", e)
    return {"last_triggered": {}, "last_update_id": 0}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)  # atomic, aman jika proses mati di tengah tulis


# ---------------------- Telegram ----------------------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            log.error("Telegram menolak pesan (%s): %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.error("Gagal kirim pesan Telegram: %s", e)
        return False


# ---------------------- Harga ----------------------
def get_zec_price():
    """Return (usd, idr, sumber) atau (None, None, None)."""
    # Sumber utama: CoinGecko (USD & IDR)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "zcash", "vs_currencies": "usd,idr"},
            timeout=10,
        )
        data = r.json()
        return float(data["zcash"]["usd"]), float(data["zcash"]["idr"]), "CoinGecko"
    except Exception as e:
        log.warning("CoinGecko gagal (%s), coba Binance", e)

    # Sumber cadangan: Binance (ZECUSDT)
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "ZECUSDT"},
            timeout=10,
        )
        usd = float(r.json()["price"])
        return usd, usd * FALLBACK_USD_IDR, "Binance"
    except Exception as e:
        log.error("Gagal mengambil harga ZEC: %s", e)
        return None, None, None


def format_status_message(usd, idr, source):
    return (
        f"<b>Status Harga Zcash (ZEC)</b>\n"
        f"Harga Saat Ini: <b>US${usd:,.2f}</b> (Rp{idr:,.0f})\n"
        f"Sumber Data: {html.escape(source)}\n\n"
        f"<b>Parameter Aktif Anda:</b>\n"
        f"- Tahap 1 (OPEX 25-30%): US$875 - US$945\n"
        f"- Tahap 2 (Profit 35-40%): US$1.000 - US$1.050\n"
        f"- Tahap 3 (Moonbag 20-30%): US$1.280+\n"
        f"- Batas Pengaman: Di bawah US$700"
    )


HELP_TEXT = (
    "<b>Perintah Bot ZEC</b>\n"
    "/cek atau /status - harga ZEC terbaru + parameter aktif\n"
    "/reset - reset cooldown semua alert\n"
    "/help - tampilkan bantuan ini"
)


# ---------------------- Pengecekan target ----------------------
def check_targets(state, usd, idr, now=None):
    now = time.time() if now is None else now
    with _state_lock:
        triggered_map = state.setdefault("last_triggered", {})
        changed = False

        for t in TARGETS:
            tid, target_price = t["id"], t["price"]
            above = t["condition"] == "above"

            hit = usd >= target_price if above else usd <= target_price
            retreated = (
                usd < target_price * (1 - RETREAT_PCT)
                if above
                else usd > target_price * (1 + RETREAT_PCT)
            )

            # Reset otomatis jika harga menjauh dari titik target
            if retreated and tid in triggered_map:
                del triggered_map[tid]
                changed = True

            last_time = triggered_map.get(tid, 0)
            if hit and (now - last_time) >= COOLDOWN_SECONDS:
                alert_msg = (
                    f"<b>{t['title']}</b>\n\n"
                    f"Harga ZEC: <b>US${usd:,.2f}</b> (Rp{idr:,.0f})\n"
                    f"Rencana Eksekusi: {t['action']}\n\n"
                    f"<i>Cek analisa momentum teknikal sebelum eksekusi jual.</i>"
                )
                if send_telegram(alert_msg):
                    triggered_map[tid] = now  # hanya dicatat jika pesan benar-benar terkirim
                    changed = True

        if changed:
            save_state(state)


# ---------------------- Perintah Telegram ----------------------
def handle_message(state, msg):
    if str(msg.get("chat", {}).get("id")) != CHAT_ID:
        return  # abaikan chat selain milik Anda
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return
    cmd = text.split()[0].split("@")[0]  # "/cek@namabot" -> "/cek"

    if cmd in ("/cek", "/status", "cek"):
        usd, idr, src = get_zec_price()
        if usd is not None:
            send_telegram(format_status_message(usd, idr, src))
        else:
            send_telegram("Gagal mengambil data harga ZEC saat ini, coba beberapa saat lagi.")
    elif cmd == "/reset":
        with _state_lock:
            state["last_triggered"] = {}
            save_state(state)
        send_telegram("Status cooldown seluruh parameter ZEC berhasil di-reset.")
    elif cmd in ("/start", "/help"):
        send_telegram(HELP_TEXT)


def command_loop(state):
    """Long-polling di thread terpisah agar perintah dibalas dalam hitungan detik."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    while not _stop.is_set():
        try:
            with _state_lock:
                offset = state.get("last_update_id", 0) + 1
            r = requests.get(url, params={"offset": offset, "timeout": 25}, timeout=35)
            data = r.json()
            if not data.get("ok"):
                log.error("getUpdates error: %s", str(data)[:200])
                _stop.wait(5)
                continue
            for upd in data.get("result", []):
                with _state_lock:
                    state["last_update_id"] = upd["update_id"]
                    save_state(state)
                msg = upd.get("message")
                if msg:
                    handle_message(state, msg)
        except Exception as e:
            log.warning("Polling perintah gagal: %s", e)
            _stop.wait(5)


# ---------------------- Main ----------------------

def self_test():
    """Log diagnosis konektivitas saat start (tidak membocorkan token)."""
    checks = [
        ("Telegram (tanpa token)", "https://api.telegram.org/"),
        ("Telegram getMe", f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"),
        ("CoinGecko", "https://api.coingecko.com/api/v3/ping"),
    ]
    for name, url in checks:
        t0 = time.time()
        try:
            r = requests.get(url, timeout=15)
            log.info("SELFTEST %s -> HTTP %s dalam %.1fs", name, r.status_code, time.time() - t0)
        except Exception as e:
            log.error("SELFTEST %s -> gagal dalam %.1fs: %s", name, time.time() - t0, type(e).__name__)

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN dan CHAT_ID wajib diisi (lihat .env.example).")

    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())

    self_test()
    state = load_state()
    usd, idr, src = get_zec_price()
    if usd is not None:
        send_telegram(
            "<b>Bot Pemantau ZEC Mining Aktif!</b>\n"
            + format_status_message(usd, idr, src)
            + "\n\nKetik /cek kapan saja untuk melihat harga terbaru."
        )

    threading.Thread(target=command_loop, args=(state,), daemon=True).start()
    log.info("Bot berjalan. Cek harga tiap %ss, cooldown %ss.", CHECK_INTERVAL, COOLDOWN_SECONDS)

    while not _stop.is_set():
        usd, idr, src = get_zec_price()
        if usd is not None:
            check_targets(state, usd, idr)
        _stop.wait(CHECK_INTERVAL)

    log.info("Bot dihentikan.")


if __name__ == "__main__":
    main()
