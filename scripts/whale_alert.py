import os
import json
import time
import requests

ETHERSCAN_KEY = os.environ["ETHERSCAN_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SINGLE_BUY_THRESHOLD_USD = 3000
LARGE_BUY_THRESHOLD_USD = 5000
ACCUMULATION_THRESHOLD_USD = 5000
ACCUMULATION_WINDOW_SECONDS = 24 * 3600

# How many blocks back to scan on the very first run (~12s/block -> 300 blocks ~ 1 hour)
FIRST_RUN_BLOCK_LOOKBACK = 300

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

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


def load_tokens():
    items = []
    with open(TOKENS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            address, symbol, decimals = parts[0], parts[1], int(parts[2])
            items.append((address.lower(), symbol, decimals))
    return items


def load_exchanges():
    addrs = set()
    with open(EXCHANGES_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            address = line.split(",", 1)[0].strip().lower()
            addrs.add(address)
    return addrs


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
    except Exception as e:
        print("Price fetch error:", e)
        return 0.0


def get_latest_block():
    r = requests.get(
        "https://api.etherscan.io/v2/api",
        params={"chainid": 1, "module": "proxy", "action": "eth_blockNumber", "apikey": ETHERSCAN_KEY},
        timeout=20,
    )

    result = r.json().get("result", "0x0")
    return int(result, 16)


def fetch_transfer_logs(contract_address, from_block, to_block):
    params = {
        "chainid": 1,
        "module": "logs",
        "action": "getLogs",
        "address": contract_address,
        "topic0": TRANSFER_TOPIC,
        "fromBlock": from_block,
        "toBlock": to_block,
        "apikey": ETHERSCAN_KEY,
    }
    r = requests.get("https://api.etherscan.io/v2/api", params=params, timeout=30)

    data = r.json()
    result = data.get("result", [])
    if not isinstance(result, list):
        print("getLogs unexpected response:", data.get("message"), data.get("result"))
        return []
    return result


def topic_to_address(topic_hex):
    return "0x" + topic_hex[-40:]


def main():
    state = load_state()
    tokens = load_tokens()
    exchange_addresses = load_exchanges()

    latest_block = get_latest_block()
    print(f"Latest block: {latest_block}, loaded {len(tokens)} tokens, {len(exchange_addresses)} exchange addresses")

    for contract, symbol, decimals in tokens:
        tstate = state.setdefault(contract, {"last_block": None, "accumulation": {}})
        from_block = tstate["last_block"] + 1 if tstate["last_block"] else latest_block - FIRST_RUN_BLOCK_LOOKBACK

        logs = fetch_transfer_logs(contract, from_block, latest_block)
        print(f"{symbol}: scanned blocks {from_block}-{latest_block}, found {len(logs)} transfer logs")

        price = get_token_price_usd(contract)
        print(f"{symbol}: price=${price}")

        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            from_address = topic_to_address(topics[1])
            to_address = topic_to_address(topics[2])
            tx_hash = log.get("transactionHash", "")

            if to_address in exchange_addresses or from_address in exchange_addresses:
                continue

            try:
                raw_value = int(log.get("data", "0x0"), 16)
            except ValueError:
                continue
            amount = raw_value / (10 ** decimals)
            usd_value = amount * price
            if usd_value <= 0:
                continue

            router_label = DEX_ROUTERS.get(from_address)
            tag = f" (via {router_label})" if router_label else ""

            if usd_value >= LARGE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🚨 <b>Büyük Cüzdan Hareketi</b>{tag}\n"
                    f"Cüzdan: {to_address}\n"
                    f"Coin: {symbol}\n"
                    f"Miktar: {amount:,.2f} (${usd_value:,.0f})\n"
                    f"Tx: https://etherscan.io/tx/{tx_hash}"
                )
            elif usd_value >= SINGLE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🟢 <b>Cüzdan Hareketi</b>{tag}\n"
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

        tstate["last_block"] = latest_block

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
