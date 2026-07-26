import os
import json
import time
import requests

ETHERSCAN_KEY = os.environ["ETHERSCAN_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SINGLE_BUY_THRESHOLD_USD = 5000
LARGE_BUY_THRESHOLD_USD = 10000
ACCUMULATION_THRESHOLD_USD = 5000
ACCUMULATION_WINDOW_SECONDS = 24 * 3600

DEX_ROUTERS = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3 Router 2",
}

STATE_FILE = "data/state.json"
TOKENS_FILE = "config/tokens.txt"
EXCHANGES_FILE = "config/exchanges.txt"


def load_state():
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_list_file(path):
    items = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            address, label = line.split(",", 1)
            items.append((address.strip().lower(), label.strip()))
    return items


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
    if not resp.ok:
        print("Telegram error:", resp.text)


def get_token_price_usd(contract_address):
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/token_price/ethereum",
            params={"contract_addresses": contract_address, "vs_currencies": "usd"},
            timeout=15,
        )
        data = r.json()
        return float(data.get(contract_address.lower(), {}).get("usd", 0) or 0)
    except Exception:
        return 0.0


def fetch_token_transfers(contract_address):
    params = {
        "module": "account",
        "action": "tokentx",
        "contractaddress": contract_address,
        "sort": "desc",
        "page": 1,
        "offset": 50,
        "apikey": ETHERSCAN_KEY,
    }
    r = requests.get("https://api.etherscan.io/api", params=params, timeout=20)
    data = r.json()
    result = data.get("result", [])
    return result if isinstance(result, list) else []


def main():
    state = load_state()
    tokens = load_list_file(TOKENS_FILE)
    exchanges = load_list_file(EXCHANGES_FILE)
    exchange_addresses = {addr for addr, _ in exchanges}

    for contract, symbol in tokens:
        tstate = state.setdefault(contract, {"last_tx_hash": None, "accumulation": {}})
        transfers = fetch_token_transfers(contract)
        if not transfers:
            continue

        transfers = list(reversed(transfers))
        new_last_hash = tstate["last_tx_hash"]
        seen_previous = tstate["last_tx_hash"] is None
        price = get_token_price_usd(contract)

        for tx in transfers:
            tx_hash = tx.get("hash")
            if not seen_previous:
                if tx_hash == tstate["last_tx_hash"]:
                    seen_previous = True
                continue

            from_address = tx.get("from", "").lower()
            to_address = tx.get("to", "").lower()
            new_last_hash = tx_hash

            if to_address in exchange_addresses:
                continue

            is_dex_buy = from_address in DEX_ROUTERS
            if not is_dex_buy:
                continue

            decimals = int(tx.get("tokenDecimal", 18) or 18)
            amount = int(tx.get("value", 0)) / (10 ** decimals)
            usd_value = amount * price
            if usd_value <= 0:
                continue

            send_telegram(
                f"🟢 <b>DEX Alım Tespit Edildi</b>\n"
                f"Cüzdan: {to_address}\n"
                f"Coin: {symbol}\n"
                f"Miktar: {amount:,.2f} (~${usd_value:,.0f})\n"
                f"DEX: {DEX_ROUTERS[from_address]}\n"
                f"Tx: https://etherscan.io/tx/{tx_hash}"
            )

            if usd_value >= LARGE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🚨 <b>Büyük Whale Alımı</b>\n"
                    f"Cüzdan: {to_address}\n"
                    f"Coin: {symbol}\n"
                    f"Miktar: {amount:,.2f} (${usd_value:,.0f})\n"
                    f"Tx: https://etherscan.io/tx/{tx_hash}"
                )
            elif usd_value >= SINGLE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🚨 <b>Orta Ölçekli Alım</b>\n"
                    f"Cüzdan: {to_address}\n"
                    f"Coin: {symbol}\n"
                    f"Miktar: {amount:,.2f} (${usd_value:,.0f})\n"
                    f"Tx: https://etherscan.io/tx/{tx_hash}"
                )
            else:
                now = int(time.time())
                acc = tstate["accumulation"].setdefault(to_address, {"total_usd": 0.0, "since": now, "count": 0})
                if now - acc["since"] > ACCUMULATION_WINDOW_SECONDS:
                    acc["total_usd"] = 0.0
                    acc["since"] = now
                    acc["count"] = 0
                acc["total_usd"] += usd_value
                acc["count"] += 1
                if acc["total_usd"] >= ACCUMULATION_THRESHOLD_USD:
                    send_telegram(
                        f"🟡 <b>Parça Parça Birikim Tespit Edildi</b>\n"
                        f"Cüzdan: {to_address}\n"
                        f"Coin: {symbol}\n"
                        f"Toplam: ${acc['total_usd']:,.0f} ({acc['count']} işlemde)\n"
                        f"Son Tx: https://etherscan.io/tx/{tx_hash}"
                    )
                    acc["total_usd"] = 0.0
                    acc["since"] = now
                    acc["count"] = 0

        tstate["last_tx_hash"] = new_last_hash

    save_state(state)


if __name__ == "__main__":
    main()
