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

MAX_DISTINCT_TOKENS_PER_SWAP = 4  # more than this = likely a batch/aggregated
# settlement tx (e.g. CoW Protocol) bundling many unrelated users' trades --
# not a single whale's swap, and not reliably attributable to one wallet.

BATCH_CHECK_MIN_USD = 50  # below this, skip the extra receipt-fetch API call;
# dust-level transfers aren't worth the API budget either way.

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


_receipt_cache = {}


def get_tx_receipt(tx_hash):
    """Fetches (and caches) the full transaction receipt, including all logs
    and the tx sender. Used both to confirm a real swap happened and to find
    which token the whale actually ended up receiving."""
    if tx_hash in _receipt_cache:
        return _receipt_cache[tx_hash]
    result = {}
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
        data = r.json()
        raw_result = data.get("result")
        if isinstance(raw_result, dict):
            result = raw_result
        else:
            print(f"Receipt fetch returned non-dict result for {tx_hash}: {raw_result}")
    except Exception as e:
        print("Receipt fetch error:", e)
    _receipt_cache[tx_hash] = result
    return result


def has_swap_event(receipt):
    """Confirms this transaction contains an actual Uniswap V2/V3 Swap event,
    not just a WETH transfer that happens to pass through a DEX-adjacent
    address (which also happens on liquidity add/remove -- those are NOT
    purchases)."""
    logs = receipt.get("logs", [])
    for log in logs:
        topic0 = (log.get("topics") or [""])[0]
        if topic0 in (V2_SWAP_TOPIC, V3_SWAP_TOPIC):
            return True
    return False


def is_batch_settlement(receipt):
    """Detects aggregated settlement transactions (e.g. CoW Protocol batch
    auctions) that bundle many different users' trades into one tx. These
    involve an unusually high number of distinct tokens moving at once and
    can't be reliably attributed to a single whale's trade -- the "wallet"
    involved is typically a shared router/settlement contract, not a person."""
    logs = receipt.get("logs", [])
    tokens = set()
    for log in logs:
        topics = log.get("topics") or []
        if topics and topics[0] == TRANSFER_TOPIC:
            addr = (log.get("address") or "").lower()
            if addr:
                tokens.add(addr)
    return len(tokens) > MAX_DISTINCT_TOKENS_PER_SWAP


def find_bought_token_address(receipt, weth_address):
    """Finds which ERC20 token the whale actually received in this tx, by
    looking at every Transfer log in the receipt (in order) and taking the
    LAST one that isn't WETH. This works regardless of whether the swap went
    straight to a pool or was routed through an aggregator/router contract --
    the final leg of any swap is always a Transfer of the output token, so
    this is far more reliable than assuming WETH's recipient IS the pool."""
    logs = receipt.get("logs", [])
    bought_address = None
    for log in logs:
        topics = log.get("topics") or []
        if not topics or topics[0] != TRANSFER_TOPIC:
            continue
        addr = (log.get("address") or "").lower()
        if addr == weth_address:
            continue
        bought_address = addr
    return bought_address


_token_pair_cache = {}


def check_dex_token(token_address):
    """Looks up DexScreener pairs by the TOKEN's contract address (not a pool
    address). This is robust to any routing path since we already know the
    exact token from find_bought_token_address."""
    if token_address in _token_pair_cache:
        return _token_pair_cache[token_address]
    result = None
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=15,
        )
        data = r.json()
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "ethereum"]
        if pairs:
            pairs.sort(key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0) or 0, reverse=True)
            result = pairs[0]
    except Exception as e:
        print("DexScreener token lookup error:", e)
    _token_pair_cache[token_address] = result
    return result


def resolve_token_info_from_pair(pair, token_address):
    """DexScreener's priceUsd/priceNative are always relative to the pair's
    BASE token. Figures out whether our token is the base or the quote side
    of the returned pair and returns (token_info, price_usd) either way."""
    base = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})

    if base.get("address", "").lower() == token_address:
        return base, float(pair.get("priceUsd", 0) or 0)

    if quote.get("address", "").lower() == token_address:
        price_usd_base = float(pair.get("priceUsd", 0) or 0)
        price_native = float(pair.get("priceNative", 0) or 0)
        price = (price_usd_base / price_native) if price_native > 0 else 0.0
        return quote, price

    return {}, 0.0


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


def find_paid_token(receipt, target_token_address, payer_address):
    """Given a swap tx, finds what OTHER token `payer_address` sent out in
    the same tx (i.e. what they paid to acquire target_token_address)."""
    logs = receipt.get("logs", [])
    for log in logs:
        topics = log.get("topics") or []
        if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
            continue
        addr = (log.get("address") or "").lower()
        if addr == target_token_address:
            continue
        from_addr = topic_to_address(topics[1])
        if from_addr == payer_address:
            return addr
    return None


def get_token_symbol_label(token_address):
    """Best-effort human-readable symbol for a token address, without
    failing hard if DexScreener doesn't know it."""
    if token_address == WETH_ADDRESS:
        return "WETH"
    pair = check_dex_token(token_address)
    if pair:
        info, _ = resolve_token_info_from_pair(pair, token_address)
        symbol = info.get("symbol")
        if symbol:
            return symbol
    return token_address[:10] + "..."


def analyze_swap(tx_hash, target_token_address, recipient_address):
    """For a plain (non-WETH-triggered) tracked-token transfer, checks
    whether it was part of a real DEX swap and, if so, whether it's a
    single whale's trade or a batch/aggregated settlement bundling many
    users together. Returns (should_skip, swap_note_line)."""
    receipt = get_tx_receipt(tx_hash)
    if not receipt or not has_swap_event(receipt):
        return False, "ℹ️ DEX işlemi değil (direkt cüzdan transferi)\n"

    if is_batch_settlement(receipt):
        return True, ""  # can't attribute to one whale, skip alerting entirely

    paid_address = find_paid_token(receipt, target_token_address, recipient_address)
    if not paid_address:
        return False, "⚠️ DEX swap'ı ama karşılığı tespit edilemedi\n"
    paid_symbol = get_token_symbol_label(paid_address)
    return False, f"DEX'te Karşılığında Verilen: {paid_symbol}\n"


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
                if usd_value < SINGLE_BUY_THRESHOLD_USD:
                    continue

                receipt = get_tx_receipt(tx_hash)
                if not receipt or not has_swap_event(receipt):
                    # Plain WETH transfer (deposit, wallet consolidation, etc.) --
                    # not a purchase of anything, so there's no "which coin" to report.
                    continue

                if is_batch_settlement(receipt):
                    print(f"Skipped (batch/aggregated settlement, not a single whale trade): {tx_hash}")
                    continue

                bought_address = find_bought_token_address(receipt, WETH_ADDRESS)
                if not bought_address:
                    print(f"Skipped (swap detected but couldn't trace bought token): {tx_hash}")
                    continue

                whale = (receipt.get("from") or "").lower()
                if not whale or whale in exchange_addresses:
                    continue

                pair = check_dex_token(bought_address)
                if pair:
                    info, token_price = resolve_token_info_from_pair(pair, bought_address)
                    bought_symbol = info.get("symbol", "???")
                    bought_name = info.get("name", "Unknown")
                    dex_name = pair.get("dexId", "Unknown DEX")
                    mcap = pair.get("marketCap") or pair.get("fdv") or 0
                    liquidity = (pair.get("liquidity", {}) or {}).get("usd", 0)
                else:
                    # Token so new DexScreener hasn't indexed it yet -- still
                    # worth alerting on, just without market data.
                    token_price = 0.0
                    bought_symbol = "???"
                    bought_name = "Bilinmiyor (DexScreener'da henüz yok)"
                    dex_name = "Bilinmiyor"
                    mcap = 0
                    liquidity = 0

                received_amount = (usd_value / token_price) if token_price > 0 else 0

                is_low_cap = mcap == 0 or mcap < LOW_CAP_THRESHOLD_USD
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
                    f"Grafik: https://dexscreener.com/ethereum/{bought_address}"
                )

                count, total_usd, tier_label = update_cluster(state, bought_address, whale, usd_value)
                if tier_label:
                    send_telegram(
                        f"{tier_label}\n\n"
                        f"Coin: {bought_name} ({bought_symbol})\n"
                        f"Kontrat: {bought_address}\n"
                        f"Farklı Balina Sayısı: {count}\n"
                        f"Toplam Alım: ${total_usd:,.0f}\n"
                        f"Grafik: https://dexscreener.com/ethereum/{bought_address}"
                    )
                continue

            if from_address in exchange_addresses or to_address in exchange_addresses:
                print(f"Skipped (exchange, static list): {tx_hash}")
                continue

            should_skip, swap_note = (False, "")
            if usd_value >= BATCH_CHECK_MIN_USD:
                should_skip, swap_note = analyze_swap(tx_hash, contract, to_address)
            if should_skip:
                print(f"Skipped (batch/aggregated settlement, not a single whale trade): {tx_hash}")
                continue

            if usd_value >= LARGE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🚨 <b>Büyük Cüzdan Hareketi</b>\n"
                    f"Cüzdan: {to_address}\n"
                    f"Coin: {symbol}\n"
                    f"Miktar: {amount:,.2f} (${usd_value:,.0f})\n"
                    f"{swap_note}"
                    f"Tx: https://etherscan.io/tx/{tx_hash}\n"
                    f"Grafik: https://dexscreener.com/ethereum/{contract}"
                )
            elif usd_value >= SINGLE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🟢 <b>Cüzdan Hareketi</b>\n"
                    f"Cüzdan: {to_address}\n"
                    f"Coin: {symbol}\n"
                    f"Miktar: {amount:,.2f} (${usd_value:,.0f})\n"
                    f"{swap_note}"
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
                        f"{swap_note}"
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
