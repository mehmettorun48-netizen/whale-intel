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

FIRST_RUN_BLOCK_LOOKBACK = 300
NEW_PAIR_RETENTION_BLOCKS = 50400

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e"
UNISWAP_V2_FACTORY = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f".lower()
WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2".lower()

DEX_ROUTERS = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3 Router 2",
}

STATE_FILE = "data/state.json"
TOKENS_FILE = "config/tokens.txt"
EXCHANGES_FILE = "config/exchanges.txt"

_dexscreener_cache = {}


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


def get_new_token_info(token_address):
    """Free DexScreener lookup for a token's name/symbol -- used only for
    the brand-new tokens flagged by the PairCreated watcher, since those
    aren't in our known tokens.txt list and we don't know their symbol yet."""
    if token_address in _dexscreener_cache:
        return _dexscreener_cache[token_address]
    info = {"symbol": "???", "name": "Unknown"}
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=15,
        )
        data = r.json()
        pairs = data.get("pairs") or []
        if pairs:
            base = pairs[0].get("baseToken", {})
            info["symbol"] = base.get("symbol", "???")
            info["name"] = base.get("name", "Unknown")
    except Exception as e:
        print("DexScreener lookup error:", e)
    _dexscreener_cache[token_address] = info
    return info


def get_latest_block():
    r = requests.get(
        "https://api.etherscan.io/v2/api",
        params={"chainid": 1, "module": "proxy", "action": "eth_blockNumber", "apikey": ETHERSCAN_KEY},
        timeout=20,
    )
    result = r.json().get("result", "0x0")
    return int(result, 16)


def fetch_logs(address, topic0, from_block, to_block):
    params = {
        "chainid": 1,
        "module": "logs",
        "action": "getLogs",
        "address": address,
        "topic0": topic0,
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


def is_exchange_related(from_address, to_address, exchange_set):
    return from_address in exchange_set or to_address in exchange_set


def main():
    state = load_state()
    tokens = load_tokens()
    exchange_addresses = load_exchanges()

    latest_block = get_latest_block()
    print(f"Latest block: {latest_block}, loaded {len(tokens)} tokens, {len(exchange_addresses)} exchange addresses")

    pairs_state = state.setdefault("new_pairs", {})
    last_pair_block = state.get("last_pair_block")
    pair_from_block = last_pair_block + 1 if last_pair_block else latest_block - FIRST_RUN_BLOCK_LOOKBACK
    pair_logs = fetch_logs(UNISWAP_V2_FACTORY, PAIR_CREATED_TOPIC, pair_from_block, latest_block)
    print(f"PairCreated: scanned blocks {pair_from_block}-{latest_block}, found {len(pair_logs)} new pairs")
    for log in pair_logs:
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        token0 = topic_to_address(topics[1])
        token1 = topic_to_address(topics[2])
        data = log.get("data", "0x")
        try:
            pair_address = "0x" + data[26:66]
        except Exception:
            continue
        if token0 == WETH_ADDRESS:
            new_token = token1
        elif token1 == WETH_ADDRESS:
            new_token = token0
        else:
            continue
        pairs_state[pair_address] = {"new_token": new_token, "created_block": latest_block}
    cutoff = latest_block - NEW_PAIR_RETENTION_BLOCKS
    for addr in list(pairs_state.keys()):
        if pairs_state[addr].get("created_block", 0) < cutoff:
            del pairs_state[addr]
    state["last_pair_block"] = latest_block
    print(f"Tracking {len(pairs_state)} new WETH pairs")

    for contract, symbol, decimals in tokens:
        tstate = state.setdefault(contract, {"last_block": None, "accumulation": {}})
        from_block = tstate["last_block"] + 1 if tstate["last_block"] else latest_block - FIRST_RUN_BLOCK_LOOKBACK

        logs = fetch_logs(contract, TRANSFER_TOPIC, from_block, latest_block)
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

            try:
                raw_value = int(log.get("data", "0x0"), 16)
            except ValueError:
                continue
            amount = raw_value / (10 ** decimals)
            usd_value = amount * price
            if usd_value <= 0:
                continue

            if symbol == "WETH" and to_address in pairs_state:
                new_token_address = pairs_state[to_address]["new_token"]
                if usd_value >= SINGLE_BUY_THRESHOLD_USD:
                    info = get_new_token_info(new_token_address)
                    send_telegram(
                        f"🆕 <b>Yeni Token'a Whale Girişi</b>\n"
                        f"Coin: {info['name']} ({info['symbol']})\n"
                        f"Kontrat: {new_token_address}\n"
                        f"Havuz: {to_address}\n"
                        f"Alım: {amount:,.4f} WETH (${usd_value:,.0f})\n"
                        f"Alıcı: {from_address}\n"
                        f"Tx: https://etherscan.io/tx/{tx_hash}\n"
                        f"DexScreener: https://dexscreener.com/ethereum/{new_token_address}"
                    )
                continue

            if is_exchange_related(from_address, to_address, exchange_addresses):
                print(f"Skipped (exchange, static list): {tx_hash}")
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
                    if not is_exchange_related(to_address, to_address, exchange_addresses):
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
