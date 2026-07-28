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

LOW_CAP_THRESHOLD_USD = 500_000  # market cap below this = "yeni/düşük cap coin"

FIRST_RUN_BLOCK_LOOKBACK = 300

CLUSTER_WINDOW_SECONDS = 24 * 3600
CLUSTER_TIERS = [
    (10, "🔴 Balinalar Yoğun Alıyor"),
    (5, "🟠 Güçlü Balina Birikimi"),
    (2, "🟡 Balina Birikimi Başladı"),
]

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2".lower()

V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d82"
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca6"

STATE_FILE = "data/state.json"
TOKENS_FILE = "config/tokens.txt"
EXCHANGES_FILE = "config/exchanges.txt"

_pair_cache = {}
_price_cache = {}


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
    if contract_address in _price_cache:
        return _price_cache[contract_address]
    price = 0.0
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/token_price/ethereum",
            params={"contract_addresses": contract_address, "vs_currencies": "usd"},
            timeout=15,
        )
        data = r.json()
        price = float(data.get(contract_address.lower(), {}).get("usd", 0) or 0)
    except Exception as e:
        print("Price fetch error:", e)
    _price_cache[contract_address] = price
    return price


def check_dex_pair(address):
    if address in _pair_cache:
        return _pair_cache[address]
    result = None
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/pairs/ethereum/{address}",
            timeout=15,
        )
        data = r.json()
        pairs = data.get("pairs") or []
        if pairs:
            result = pairs[0]
    except Exception as e:
        print("DexScreener pair lookup error:", e)
    _pair_cache[address] = result
    return result


def resolve_bought_token_and_price(pair):
    """DexScreener's priceUsd/priceNative are always relative to the pair's
    BASE token -- if the base token happens to be WETH (not the token the
    whale actually bought), using priceUsd directly gives WETH's price, not
    the purchased token's. This converts correctly either way."""
    base = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})
    base_is_weth = base.get("address", "").lower() == WETH_ADDRESS

    price_usd_base = float(pair.get("priceUsd", 0) or 0)
    price_native = float(pair.get("priceNative", 0) or 0)  # base priced in quote units

    if base_is_weth:
        bought = quote
        # 1 base(WETH) = price_native quote-tokens, and 1 base(WETH) = price_usd_base USD
        # => 1 quote-token = price_usd_base / price_native USD
        bought_price_usd = (price_usd_base / price_native) if price_native > 0 else 0.0
    else:
        bought = base
        bought_price_usd = price_usd_base

    return bought, bought_price_usd


def is_real_swap(tx_hash):
    """Confirms this transaction contains an actual Uniswap Swap event, not
    just a WETH transfer that happens to land in a pool address (which also
    happens on liquidity add/remove -- those are NOT purchases)."""
    try:
        r = requests.get(
            "https://api.etherscan.io/v2/api",
            params={
                "chainid": 1,
                "module": "proxy",
                "action": "eth_getTransactionReceipt",
                "txhash": tx_hash,
                "apikey": ETHERSCAN_KEY,
            },
            timeout=20,
        )
        result = r.json().get("result") or {}
        logs = result.get("logs", [])
        for log in logs:
            topic0 = (log.get("topics") or [""])[0]
            if topic0 in (V2_SWAP_TOPIC, V3_SWAP_TOPIC):
                return True
        return False
    except Exception as e:
        print("Swap check error:", e)
        return False


def get_tx_sender(tx_hash):
    try:
        r = requests.get(
            "https://api.etherscan.io/v2/api",
            params={
                "chainid": 1,
                "module": "proxy",
                "action": "eth_getTransactionByHash",
                "txhash": tx_hash,
                "apikey": ETHERSCAN_KEY,
            },
            timeout=20,
        )
        result = r.json().get("result") or {}
        return (result.get("from") or "").lower()
    except Exception as e:
        print("Tx sender lookup error:", e)
        return ""


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


def update_cluster(state, token_contract, whale_address, usd_value):
    clusters = state.setdefault("clusters", {})
    c = clusters.setdefault(token_contract, {"whales": {}, "total_usd": 0.0, "last_tier": 0})

    now = int(time.time())
    c["whales"][whale_address] = now
    c["total_usd"] += usd_value

    cutoff = now - CLUSTER_WINDOW_SECONDS
    c["whales"] = {addr: ts for addr, ts in c["whales"].items() if ts >= cutoff}
    distinct_count = len(c["whales"])

    newly_crossed = None
    for threshold, label in CLUSTER_TIERS:
        if distinct_count >= threshold and c["last_tier"] < threshold:
            newly_crossed = label
            c["last_tier"] = threshold
            break

    return distinct_count, c["total_usd"], newly_crossed


def main():
    state = load_state()
    tokens = load_tokens()
    exchange_addresses = load_exchanges()

    latest_block = get_latest_block()
    print(f"Latest block: {latest_block}, loaded {len(tokens)} tokens, {len(exchange_addresses)} exchange addresses")

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

            if symbol == "WETH":
                pair = check_dex_pair(to_address)
                if pair:
                    if usd_value < SINGLE_BUY_THRESHOLD_USD:
                        continue

                    if not is_real_swap(tx_hash):
                        print(f"Skipped (not a real swap, likely liquidity add/remove): {tx_hash}")
                        continue

                    bought, token_price = resolve_bought_token_and_price(pair)
                    bought_address = bought.get("address", "").lower()
                    bought_symbol = bought.get("symbol", "???")
                    bought_name = bought.get("name", "Unknown")
                    dex_name = pair.get("dexId", "Unknown DEX")
                    mcap = pair.get("marketCap") or pair.get("fdv") or 0
                    liquidity = (pair.get("liquidity", {}) or {}).get("usd", 0)

                    whale = get_tx_sender(tx_hash)
                    if not whale or whale in exchange_addresses:
                        continue

                    received_amount = (usd_value / token_price) if token_price > 0 else 0

                    is_low_cap = 0 < mcap < LOW_CAP_THRESHOLD_USD
                    if is_low_cap:
                        priority = "🔥"
                        header = "ERKEN BALİNA - DÜŞÜK CAP"
                    elif usd_value >= LARGE_BUY_THRESHOLD_USD:
                        priority = "🚨"
                        header = "Yeni Balina Alımı"
                    else:
                        priority = "🟢"
                        header = "Yeni Balina Alımı"

                    send_telegram(
                        f"{priority} <b>{header}</b>\n\n"
                        f"<pre>"
                        f"Satın Alınan Coin: {bought_name} ({bought_symbol})\n"
                        f"Kontrat: {bought_address}\n\n"
                        f"Alınan: {received_amount:,.2f} {bought_symbol}\n"
                        f"USD: ${usd_value:,.0f}\n\n"
                        f"Fiyat: ${token_price:.8f}\n"
                        f"Market Cap: ${mcap:,.0f}\n"
                        f"Likidite: ${liquidity:,.0f}\n"
                        f"DEX: {dex_name}"
                        f"</pre>\n"
                        f"Balina: {whale}\n"
                        f"Tx: https://etherscan.io/tx/{tx_hash}\n"
                        f"Grafik: https://dexscreener.com/ethereum/{to_address}"
                    )

                    count, total_usd, tier_label = update_cluster(state, bought_address, whale, usd_value)
                    if tier_label:
                        send_telegram(
                            f"{tier_label}\n\n"
                            f"Coin: {bought_name} ({bought_symbol})\n"
                            f"Kontrat: {bought_address}\n"
                            f"Farklı Balina Sayısı: {count}\n"
                            f"Toplam Alım: ${total_usd:,.0f}\n"
                            f"Grafik: https://dexscreener.com/ethereum/{to_address}"
                        )
                    continue

            if from_address in exchange_addresses or to_address in exchange_addresses:
                print(f"Skipped (exchange, static list): {tx_hash}")
                continue

            if usd_value >= LARGE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🚨 <b>Büyük Cüzdan Hareketi</b>\n"
                    f"Cüzdan: {to_address}\n"
                    f"Coin: {symbol}\n"
                    f"Miktar: {amount:,.2f} (${usd_value:,.0f})\n"
                    f"Tx: https://etherscan.io/tx/{tx_hash}\n"
                    f"Grafik: https://dexscreener.com/ethereum/{contract}"
                )
            elif usd_value >= SINGLE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🟢 <b>Cüzdan Hareketi</b>\n"
                    f"Cüzdan: {to_address}\n"
                    f"Coin: {symbol}\n"
                    f"Miktar: {amount:,.2f} (${usd_value:,.0f})\n"
                    f"Tx: https://etherscan.io/tx/{tx_hash}\n"
                    f"Grafik: https://dexscreener.com/ethereum/{contract}"
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
                        f"Son Tx: https://etherscan.io/tx/{tx_hash}\n"
                        f"Grafik: https://dexscreener.com/ethereum/{contract}"
                    )
                    acc["total_usd"] = 0.0
                    acc["since"] = now
                    acc["count"] = 0

        tstate["last_block"] = latest_block

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
