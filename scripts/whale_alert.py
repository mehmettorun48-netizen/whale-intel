import os
import json
import time
import requests

ETHERSCAN_KEY = os.environ["ETHERSCAN_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SINGLE_BUY_THRESHOLD_USD = 1000
LARGE_BUY_THRESHOLD_USD = 5000
ACCUMULATION_THRESHOLD_USD = 5000
ACCUMULATION_WINDOW_SECONDS = 24 * 3600

MIN_MCAP_USD = 20_000

MAX_PAIR_AGE_DAYS = 30

NEW_TOKEN_AGE_HOURS = 2160
NEW_TOKEN_MCAP_CEILING = 50_000_000
PRICE_WATCH_MAX_AGE_HOURS = 72
MAX_DISTINCT_TOKENS_PER_SWAP = 7

ETHERSCAN_MIN_INTERVAL = 0.5
_last_etherscan_call = 0.0

BLOCKSCOUT_MIN_INTERVAL = 1.5  # önce 0.25 idi -- Blockscout'un anonim/
# API-key'siz erişimi çok daha düşük bir hız sınırına sahip, daha ilk
# çağrıda "429 Too many requests" hatası alındı. 1.5 saniye daha güvenli
# bir tempo.
_last_blockscout_call = 0.0

ANALYZE_SWAP_MIN_USD = 100
FIRST_RUN_BLOCK_LOOKBACK = 300
MAX_BLOCKS_PER_RUN = 30

CLUSTER_WINDOW_SECONDS = 6 * 3600
CLUSTER_TIERS = [
    (10, "🔴 Balinalar Yoğun Alıyor"),
    (5, "🟠 Güçlü Balina Birikimi"),
    (2, "🟡 Balina Birikimi Başladı"),
]

STABLECOIN_QUOTE_SYMBOLS = {
    "USDT", "USDT0", "USD₮0", "USDC", "USDC.E", "USDG", "USDE", "DAI",
    "BUSD", "TUSD", "USDP", "FDUSD", "USDD", "PYUSD", "FRAX", "GUSD",
    "LUSD",
}

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d82"
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca6"
CURVE_EXCHANGE_TOPIC = "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140"
SWAP_TOPICS = (V2_SWAP_TOPIC, V3_SWAP_TOPIC, CURVE_EXCHANGE_TOPIC)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

CHAINS = {
    "ethereum": {
        "chain_id": 1,
        "native_wrapped_address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "native_wrapped_symbol": "WETH",
        "discovery_addresses": {"0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"},
        "dexscreener_id": "ethereum",
        "coingecko_platform": "ethereum",
        "explorer": "https://etherscan.io",
        "max_blocks_per_run": 30,
    },
    "bsc": {
        "chain_id": 56,
        "native_wrapped_address": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        "native_wrapped_symbol": "WBNB",
        "discovery_addresses": {"0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"},
        "dexscreener_id": "bsc",
        "coingecko_platform": "binance-smart-chain",
        "explorer": "https://bscscan.com",
        "max_blocks_per_run": 30,
    },
    "base": {
        "chain_id": 8453,
        "native_wrapped_address": "0x4200000000000000000000000000000000000006",
        "native_wrapped_symbol": "WETH",
        "discovery_addresses": {"0x4200000000000000000000000000000000000006"},
        "dexscreener_id": "base",
        "coingecko_platform": "base",
        "explorer": "https://basescan.org",
        "max_blocks_per_run": 150,
    },
    "arbitrum": {
        "chain_id": 42161,
        "native_wrapped_address": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        "native_wrapped_symbol": "WETH",
        "discovery_addresses": {"0x82af49447d8a07e3bd95bd0d56f35241523fbab1"},
        "dexscreener_id": "arbitrum",
        "coingecko_platform": "arbitrum-one",
        "explorer": "https://arbiscan.org",
        "max_blocks_per_run": 10000,
    },
    "robinhood": {
        "chain_id": 4663,
        "native_wrapped_address": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        "native_wrapped_symbol": "WETH",
        "discovery_addresses": {"0x0bd7d308f8e1639fab988df18a8011f41eacad73"},
        "dexscreener_id": "robinhood",
        "coingecko_platform": None,
        "explorer": "https://robinhoodchain.blockscout.com",
        "api_type": "blockscout",
        "legacy_api_base": "https://robinhoodchain.blockscout.com/api",
        "max_blocks_per_run": 20000,
    },
}

STATE_FILE = "data/state.json"
TOKENS_FILE = "config/tokens.txt"
EXCHANGES_FILE = "config/exchanges.txt"

_price_cache = {}
_receipt_cache = {}
_token_pair_cache = {}


def etherscan_call(chain_id, params, timeout=20):
    global _last_etherscan_call
    elapsed = time.time() - _last_etherscan_call
    if elapsed < ETHERSCAN_MIN_INTERVAL:
        time.sleep(ETHERSCAN_MIN_INTERVAL - elapsed)
    full_params = dict(params)
    full_params["chainid"] = chain_id
    full_params["apikey"] = ETHERSCAN_KEY
    _last_etherscan_call = time.time()
    r = requests.get("https://api.etherscan.io/v2/api", params=full_params, timeout=timeout)
    return r.json()


def _to_hex_block(value):
    if value is None:
        return "latest"
    s = str(value)
    if s in ("latest", "earliest", "pending"):
        return s
    if s.startswith("0x"):
        return s
    return hex(int(s))


def blockscout_jsonrpc_call(api_base, method, rpc_params, timeout=20, _retry=0):
    """Blockscout'un standart Ethereum JSON-RPC 2.0 endpoint'i
    ({instance}/api/eth-rpc). 429 (rate limit) hatası alınırsa, artan
    bekleme süreleriyle en fazla 3 kez yeniden dener -- API key'siz
    erişimin düşük hız sınırına takılmak, tüm run'ı iptal etmesin diye."""
    global _last_blockscout_call
    elapsed = time.time() - _last_blockscout_call
    if elapsed < BLOCKSCOUT_MIN_INTERVAL:
        time.sleep(BLOCKSCOUT_MIN_INTERVAL - elapsed)
    _last_blockscout_call = time.time()
    try:
        r = requests.post(f"{api_base}/eth-rpc", json={
            "jsonrpc": "2.0",
            "method": method,
            "params": rpc_params,
            "id": 1,
        }, timeout=timeout)
        if r.status_code == 429 and _retry < 3:
            wait = 3 * (2 ** _retry)
            print(f"Blockscout rate limited ({method}), waiting {wait}s and retrying ({_retry + 1}/3)")
            time.sleep(wait)
            return blockscout_jsonrpc_call(api_base, method, rpc_params, timeout, _retry + 1)
        if not r.ok:
            print(f"Blockscout JSON-RPC call failed ({method}): HTTP {r.status_code} -- {r.text[:200]}")
            return {}
        return r.json()
    except Exception as e:
        print(f"Blockscout JSON-RPC call error ({method}): {e}")
        return {}


def blockscout_legacy_call(api_base, params, timeout=20):
    global _last_blockscout_call
    elapsed = time.time() - _last_blockscout_call
    if elapsed < BLOCKSCOUT_MIN_INTERVAL:
        time.sleep(BLOCKSCOUT_MIN_INTERVAL - elapsed)
    _last_blockscout_call = time.time()
    try:
        r = requests.get(api_base, params=params, timeout=timeout)
        if not r.ok:
            print(f"Blockscout legacy call failed: HTTP {r.status_code} -- {r.text[:200]}")
            return {}
        return r.json()
    except Exception as e:
        print(f"Blockscout legacy call error: {e}")
        return {}


def rpc_call(chain_cfg, params, timeout=20):
    if chain_cfg.get("api_type") != "blockscout":
        return etherscan_call(chain_cfg["chain_id"], params, timeout)

    api_base = chain_cfg["legacy_api_base"]
    module = params.get("module")
    action = params.get("action")

    if module == "proxy":
        if action == "eth_blockNumber":
            return blockscout_jsonrpc_call(api_base, "eth_blockNumber", [], timeout)
        if action == "eth_call":
            return blockscout_jsonrpc_call(api_base, "eth_call", [
                {"to": params.get("to"), "data": params.get("data")},
                params.get("tag", "latest"),
            ], timeout)
        if action == "eth_getTransactionReceipt":
            return blockscout_jsonrpc_call(api_base, "eth_getTransactionReceipt",
                                            [params.get("txhash")], timeout)
        if action == "eth_getBlockByNumber":
            bool_flag = str(params.get("boolean", "false")).lower() == "true"
            return blockscout_jsonrpc_call(api_base, "eth_getBlockByNumber", [
                _to_hex_block(params.get("tag")), bool_flag,
            ], timeout)
        print(f"Blockscout: bilinmeyen proxy action: {action}")
        return {}

    if module == "logs" and action == "getLogs":
        result = blockscout_jsonrpc_call(api_base, "eth_getLogs", [{
            "fromBlock": _to_hex_block(params.get("fromBlock")),
            "toBlock": _to_hex_block(params.get("toBlock")),
            "address": params.get("address"),
            "topics": [params.get("topic0")],
        }], timeout)
        if "result" in result:
            return result
        return {"result": []}

    return blockscout_legacy_call(api_base, params, timeout)


def get_chain_cfg(chain_key):
    return CHAINS.get(chain_key)


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
            chain, address, symbol, decimals = parts[0].lower(), parts[1], parts[2], int(parts[3])
            if chain not in CHAINS:
                print(f"Skipping token on unknown chain '{chain}': {line}")
                continue
            items.append((chain, address.lower(), symbol, decimals))
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


def get_token_price_usd(coingecko_platform, contract_address):
    if not coingecko_platform:
        return 0.0
    cache_key = (coingecko_platform, contract_address)
    if cache_key in _price_cache:
        return _price_cache[cache_key]
    price = 0.0
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/simple/token_price/{coingecko_platform}",
            params={"contract_addresses": contract_address, "vs_currencies": "usd"},
            timeout=15,
        )
        data = r.json()
        price = float(data.get(contract_address.lower(), {}).get("usd", 0) or 0)
    except Exception as e:
        print("Price fetch error:", e)
    _price_cache[cache_key] = price
    return price


def get_tx_receipt(chain_cfg, tx_hash):
    cache_key = (chain_cfg["chain_id"], tx_hash)
    if cache_key in _receipt_cache:
        return _receipt_cache[cache_key]
    result = {}
    try:
        data = rpc_call(chain_cfg, {
            "module": "proxy",
            "action": "eth_getTransactionReceipt",
            "txhash": tx_hash,
        })
        raw_result = data.get("result")
        if isinstance(raw_result, dict):
            result = raw_result
        else:
            print(f"Receipt fetch returned non-dict result for {tx_hash}: {raw_result}")
    except Exception as e:
        print("Receipt fetch error:", e)
    _receipt_cache[cache_key] = result
    return result


def has_swap_event(receipt):
    logs = receipt.get("logs", [])
    for log in logs:
        topic0 = (log.get("topics") or [""])[0]
        if topic0 in SWAP_TOPICS:
            return True
    return False


def is_batch_settlement(receipt):
    logs = receipt.get("logs", [])
    tokens = set()
    for log in logs:
        topics = log.get("topics") or []
        if topics and topics[0] == TRANSFER_TOPIC:
            addr = (log.get("address") or "").lower()
            if addr:
                tokens.add(addr)
    return len(tokens) > MAX_DISTINCT_TOKENS_PER_SWAP


def find_bought_transfer(receipt, native_wrapped_address):
    logs = receipt.get("logs", [])
    bought_address, recipient, raw_amount = None, None, 0
    for log in logs:
        topics = log.get("topics") or []
        if not topics or topics[0] != TRANSFER_TOPIC or len(topics) < 3:
            continue
        addr = (log.get("address") or "").lower()
        if addr == native_wrapped_address:
            continue
        bought_address = addr
        recipient = topic_to_address(topics[2])
        try:
            raw_amount = int(log.get("data", "0x0"), 16)
        except ValueError:
            raw_amount = 0
    return bought_address, recipient, raw_amount


_decimals_cache = {}


def get_token_decimals(chain_cfg, token_address):
    cache_key = (chain_cfg["chain_id"], token_address)
    if cache_key in _decimals_cache:
        return _decimals_cache[cache_key]
    decimals = 18
    try:
        data = rpc_call(chain_cfg, {
            "module": "proxy",
            "action": "eth_call",
            "to": token_address,
            "data": "0x313ce567",
            "tag": "latest",
        })
        result = data.get("result")
        if isinstance(result, str) and result.startswith("0x") and result != "0x":
            decimals = int(result, 16)
    except Exception as e:
        print("Decimals fetch error:", e)
    _decimals_cache[cache_key] = decimals
    return decimals


_creation_time_cache = {}


def get_contract_creation_time(chain_cfg, token_address):
    cache_key = (chain_cfg["chain_id"], token_address)
    if cache_key in _creation_time_cache:
        return _creation_time_cache[cache_key]
    creation_ts = None
    try:
        data = rpc_call(chain_cfg, {
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": token_address,
        })
        result = data.get("result")
        if isinstance(result, list) and result:
            tx_hash = result[0].get("txHash")
            if tx_hash:
                receipt = get_tx_receipt(chain_cfg, tx_hash)
                block_hex = receipt.get("blockNumber")
                if block_hex:
                    block_data = rpc_call(chain_cfg, {
                        "module": "proxy",
                        "action": "eth_getBlockByNumber",
                        "tag": block_hex,
                        "boolean": "false",
                    })
                    block = block_data.get("result")
                    if isinstance(block, dict):
                        ts_hex = block.get("timestamp")
                        if ts_hex:
                            creation_ts = int(ts_hex, 16)
    except Exception as e:
        print("Contract creation time fetch error:", e)
    _creation_time_cache[cache_key] = creation_ts
    return creation_ts


_pool_address_cache = {}

EXCLUDED_CATEGORY_KEYWORDS = [
    "liquid staking", "liquid restaking", "restaking", "staking",
    "wrapped-tokens", "wrapped", "stablecoin", "synthetic",
    "yield aggregator", "yield farming", "yield", "vault",
    "lp tokens", "liquidity provider", "bridged", "rebase",
    "receipt token", "tokenized btc", "tokenized eth", "interest bearing",
]


def call_view_function(chain_cfg, token_address, selector):
    try:
        data = rpc_call(chain_cfg, {
            "module": "proxy",
            "action": "eth_call",
            "to": token_address,
            "data": selector,
            "tag": "latest",
        })
        result = data.get("result")
        if isinstance(result, str) and result not in ("0x", ""):
            return result
    except Exception as e:
        print("eth_call error:", e)
    return None


def _decodes_to_address(hex_result):
    try:
        addr_hex = hex_result[-40:]
        return len(addr_hex) == 40 and int(addr_hex, 16) != 0
    except (ValueError, TypeError):
        return False


def is_erc4626_or_wrapped(chain_cfg, token_address):
    if _decodes_to_address(call_view_function(chain_cfg, token_address, "0x38d52e0f")):
        return True, "ERC4626 vault (asset() fonksiyonu var)"
    if _decodes_to_address(call_view_function(chain_cfg, token_address, "0x6f307dc3")):
        return True, "Wrapped/receipt token (underlying() fonksiyonu var)"
    return False, ""


_cg_data_cache = {}


def get_coingecko_token_data(coingecko_platform, token_address):
    if not coingecko_platform:
        return {}
    cache_key = (coingecko_platform, token_address)
    if cache_key in _cg_data_cache:
        return _cg_data_cache[cache_key]
    data = {}
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coingecko_platform}/contract/{token_address}",
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
    except Exception as e:
        print("CoinGecko token data fetch error:", e)
    _cg_data_cache[cache_key] = data
    return data


def get_coingecko_categories(coingecko_platform, token_address):
    data = get_coingecko_token_data(coingecko_platform, token_address)
    return data.get("categories") or []


MAJOR_CEX_NAMES = [
    "binance", "coinbase", "kraken", "okx", "bybit", "upbit", "kucoin",
    "bitget", "gate.io", "gateio", "htx", "huobi", "bitfinex", "crypto.com",
    "mexc", "bitstamp",
]


def is_cex_listed(coingecko_platform, token_address):
    data = get_coingecko_token_data(coingecko_platform, token_address)
    tickers = data.get("tickers") or []
    for ticker in tickers:
        market_name = ((ticker.get("market") or {}).get("name") or "").lower()
        for cex in MAJOR_CEX_NAMES:
            if cex in market_name:
                return True
    return False


def is_infrastructure_token(chain_cfg, token_address):
    categories = get_coingecko_categories(chain_cfg["coingecko_platform"], token_address)
    for cat in categories:
        cat_lower = cat.lower()
        for keyword in EXCLUDED_CATEGORY_KEYWORDS:
            if keyword in cat_lower:
                return True, f"CoinGecko kategorisi: {cat}"

    is_wrapped, reason = is_erc4626_or_wrapped(chain_cfg, token_address)
    if is_wrapped:
        return True, reason

    return False, ""


def get_dex_pool_addresses(dexscreener_id, token_address):
    cache_key = (dexscreener_id, token_address)
    if cache_key in _pool_address_cache:
        return _pool_address_cache[cache_key]
    result = set()
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=15,
        )
        data = r.json()
        for p in (data.get("pairs") or []):
            if p.get("chainId") == dexscreener_id:
                addr = (p.get("pairAddress") or "").lower()
                if addr:
                    result.add(addr)
    except Exception as e:
        print("DexScreener pool-address lookup error:", e)
    _pool_address_cache[cache_key] = result
    return result


def check_dex_token(dexscreener_id, token_address):
    cache_key = (dexscreener_id, token_address)
    if cache_key in _token_pair_cache:
        return _token_pair_cache[cache_key]
    result = None
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=15,
        )
        data = r.json()
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == dexscreener_id]
        if pairs:
            pairs.sort(key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0) or 0, reverse=True)
            result = pairs[0]
    except Exception as e:
        print("DexScreener token lookup error:", e)
    _token_pair_cache[cache_key] = result
    return result


def resolve_token_info_from_pair(pair, token_address):
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


def get_counterparty_symbol(pair, token_address):
    base = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})
    if base.get("address", "").lower() == token_address:
        return (quote.get("symbol") or "").upper()
    if quote.get("address", "").lower() == token_address:
        return (base.get("symbol") or "").upper()
    return ""


def get_pair_age_days(pair):
    pair_created_ms = pair.get("pairCreatedAt")
    if not pair_created_ms:
        return None
    return (time.time() - pair_created_ms / 1000) / 86400


def get_latest_block(chain_cfg):
    data = rpc_call(chain_cfg, {"module": "proxy", "action": "eth_blockNumber"})
    result = data.get("result", "0x0")
    return int(result, 16)


def fetch_logs(chain_cfg, address, topic0, from_block, to_block, _depth=0):
    data = rpc_call(chain_cfg, {
        "module": "logs",
        "action": "getLogs",
        "address": address,
        "topic0": topic0,
        "fromBlock": from_block,
        "toBlock": to_block,
    }, timeout=30)
    result = data.get("result", [])
    if not isinstance(result, list):
        print("getLogs unexpected response:", data.get("message"), data.get("result"))
        return []

    if len(result) >= 1000 and from_block < to_block and _depth < 20:
        mid = (from_block + to_block) // 2
        print(f"Hit 1000-log cap for blocks {from_block}-{to_block}, splitting at {mid}")
        left = fetch_logs(chain_cfg, address, topic0, from_block, mid, _depth + 1)
        right = fetch_logs(chain_cfg, address, topic0, mid + 1, to_block, _depth + 1)
        return left + right

    return result


def topic_to_address(topic_hex):
    return "0x" + topic_hex[-40:]


def update_cluster(state, cluster_key, whale_address, usd_value):
    clusters = state.setdefault("clusters", {})
    c = clusters.setdefault(cluster_key, {"whales": {}, "total_usd": 0.0, "last_tier": 0})

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


def get_token_symbol_label(chain_cfg, token_address):
    if token_address == chain_cfg.get("native_wrapped_address"):
        return chain_cfg["native_wrapped_symbol"]
    pair = check_dex_token(chain_cfg["dexscreener_id"], token_address)
    if pair:
        info, _ = resolve_token_info_from_pair(pair, token_address)
        symbol = info.get("symbol")
        if symbol:
            return symbol
    return token_address[:10] + "..."


def analyze_swap(chain_cfg, tx_hash, target_token_address, recipient_address):
    receipt = get_tx_receipt(chain_cfg, tx_hash)
    if not receipt or not has_swap_event(receipt):
        return True, ""

    if is_batch_settlement(receipt):
        return True, ""

    pool_addresses = get_dex_pool_addresses(chain_cfg["dexscreener_id"], target_token_address)
    if recipient_address in pool_addresses:
        return True, ""

    paid_address = find_paid_token(receipt, target_token_address, recipient_address)
    if not paid_address:
        return False, "⚠️ DEX swap'ı ama karşılığı tespit edilemedi\n"
    paid_symbol = get_token_symbol_label(chain_cfg, paid_address)
    if paid_symbol.upper() in STABLECOIN_QUOTE_SYMBOLS:
        return True, ""
    return False, f"DEX'te Karşılığında Verilen: {paid_symbol}\n"


def check_accumulation_breakouts(state):
    watch = state.setdefault("price_watch", {})
    now = int(time.time())

    stale_keys = [
        key for key, entry in watch.items()
        if (now - entry.get("first_seen_ts", now)) / 3600 > PRICE_WATCH_MAX_AGE_HOURS
    ]
    for key in stale_keys:
        del watch[key]

    for key, entry in watch.items():
        if entry.get("alerted"):
            continue

        chain, address = key.split(":", 1)
        chain_cfg = get_chain_cfg(chain)
        if not chain_cfg:
            continue

        if is_cex_listed(chain_cfg.get("coingecko_platform"), address):
            continue

        pair = check_dex_token(chain_cfg["dexscreener_id"], address)
        if not pair:
            continue

        counterparty_symbol = get_counterparty_symbol(pair, address)
        if counterparty_symbol in STABLECOIN_QUOTE_SYMBOLS:
            print(f"[{chain}] {entry.get('symbol')}: pool {counterparty_symbol} "
                  f"stablecoin'ine karşı -- bildirim gönderilmiyor")
            continue

        pair_age_days = get_pair_age_days(pair)
        if pair_age_days is not None and pair_age_days > MAX_PAIR_AGE_DAYS:
            print(f"[{chain}] {entry.get('symbol')}: pair {pair_age_days:.1f} gün "
                  f"-- eşiğin ({MAX_PAIR_AGE_DAYS} gün) üzerinde, bildirim "
                  f"gönderilmiyor")
            continue

        volume = pair.get("volume") or {}
        price_change = pair.get("priceChange") or {}
        change_h6 = price_change.get("h6")
        change_h1 = price_change.get("h1")
        vol_h1 = volume.get("h1") or 0

        print(f"[{chain}] {entry.get('symbol')}: h6={change_h6}, h1={change_h1}, "
              f"vol_h1=${vol_h1:,.0f}, pair_yaşı={pair_age_days} gün -- "
              f"eşik kontrolleri kaldırıldı, doğrudan bildirim gönderiliyor")

        _, current_price = resolve_token_info_from_pair(pair, address)
        mcap = pair.get("marketCap") or pair.get("fdv") or 0
        liquidity = (pair.get("liquidity", {}) or {}).get("usd", 0)
        dex_name = pair.get("dexId", "Unknown DEX")
        send_telegram(
            f"⚡ <b>Coin İzleme Bildirimi</b> [{chain.upper()}]\n\n"
            f"<pre>"
            f"Coin: {entry.get('name', 'Unknown')} ({entry.get('symbol', '???')})\n"
            f"Kontrat: {address}\n\n"
            f"Son 6 Saat Değişim: {change_h6}%\n"
            f"Son 1 Saat Değişim: {change_h1}%\n"
            f"Son 1 Saat Hacim: ${vol_h1:,.0f}\n"
            f"Fiyat: ${current_price:.8f}\n"
            f"Market Cap: ${mcap:,.0f}\n"
            f"Likidite: ${liquidity:,.0f}\n"
            f"DEX: {dex_name}"
            f"</pre>\n"
            f"Grafik: https://dexscreener.com/{chain_cfg['dexscreener_id']}/{address}\n\n"
            f"Not: Eşik kontrolleri (sakin/kırılım/hacim/cooldown) kapalı -- "
            f"bu coin izleme listesine girmiş olması yeterli oldu."
        )
        entry["alerted"] = True


def main():
    state = load_state()
    check_accumulation_breakouts(state)

    tokens = load_tokens()
    exchange_addresses = load_exchanges()

    chains_in_use = sorted({t[0] for t in tokens})
    latest_blocks = {chain: get_latest_block(CHAINS[chain]) for chain in chains_in_use}
    tracked_by_chain = {}
    for t_chain, t_contract, _, _ in tokens:
        tracked_by_chain.setdefault(t_chain, set()).add(t_contract)
    print(f"Loaded {len(tokens)} tokens across chains {chains_in_use}, {len(exchange_addresses)} exchange addresses")

    for chain, contract, symbol, decimals in tokens:
        token_start_time = time.time()
        chain_cfg = CHAINS[chain]
        latest_block = latest_blocks[chain]
        explorer = chain_cfg["explorer"]
        dex_id = chain_cfg["dexscreener_id"]

        state_key = f"{chain}:{contract}"
        tstate = state.setdefault(state_key, {"last_block": None, "accumulation": {}})
        from_block = tstate["last_block"] + 1 if tstate["last_block"] else latest_block - FIRST_RUN_BLOCK_LOOKBACK

        to_block = latest_block
        chain_max_blocks = chain_cfg.get("max_blocks_per_run", MAX_BLOCKS_PER_RUN)
        if to_block - from_block > chain_max_blocks:
            to_block = from_block + chain_max_blocks
            print(f"[{chain}] {symbol}: backlog too large ({latest_block - from_block} blocks), capping this run to {from_block}-{to_block}")

        logs = fetch_logs(chain_cfg, contract, TRANSFER_TOPIC, from_block, to_block)
        print(f"[{chain}] {symbol}: scanned blocks {from_block}-{to_block}, found {len(logs)} transfer logs")

        price = get_token_price_usd(chain_cfg["coingecko_platform"], contract)
        print(f"[{chain}] {symbol}: price=${price}")

        is_discovery_trigger = contract in chain_cfg["discovery_addresses"]

        seen_tx_hashes_this_token = set()

        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            from_address = topic_to_address(topics[1])
            to_address = topic_to_address(topics[2])
            tx_hash = log.get("transactionHash", "")

            if tx_hash and tx_hash in seen_tx_hashes_this_token:
                continue
            if tx_hash:
                seen_tx_hashes_this_token.add(tx_hash)

            try:
                raw_value = int(log.get("data", "0x0"), 16)
            except ValueError:
                continue
            amount = raw_value / (10 ** decimals)
            usd_value = amount * price
            if usd_value <= 0:
                continue

            if is_discovery_trigger:
                if usd_value < SINGLE_BUY_THRESHOLD_USD:
                    continue

                receipt = get_tx_receipt(chain_cfg, tx_hash)
                if not receipt or not has_swap_event(receipt):
                    continue

                if is_batch_settlement(receipt):
                    print(f"Skipped (batch/aggregated settlement, not a single whale trade): {tx_hash}")
                    continue

                bought_address, whale, raw_bought_amount = find_bought_transfer(receipt, contract)
                if not bought_address or not whale:
                    print(f"Skipped (swap detected but couldn't trace bought token): {tx_hash}")
                    continue

                if bought_address in tracked_by_chain.get(chain, set()):
                    continue

                if not whale or whale == ZERO_ADDRESS or whale in exchange_addresses:
                    continue

                pool_addresses = get_dex_pool_addresses(dex_id, bought_address)
                if whale in pool_addresses:
                    continue

                infra_cache_state = state.setdefault("infra_status", {})
                infra_cache_key = f"{chain}:{bought_address}"
                if infra_cache_key in infra_cache_state:
                    is_infra, infra_reason = infra_cache_state[infra_cache_key]
                else:
                    is_infra, infra_reason = is_infrastructure_token(chain_cfg, bought_address)
                    infra_cache_state[infra_cache_key] = [is_infra, infra_reason]
                if is_infra:
                    print(f"Skipped (infrastructure/staking/wrapped token - {infra_reason}): {tx_hash}")
                    continue

                if is_cex_listed(chain_cfg["coingecko_platform"], bought_address):
                    print(f"Skipped (already listed on a major CEX): {tx_hash}")
                    continue

                pair = check_dex_token(dex_id, bought_address)
                if pair:
                    info, token_price = resolve_token_info_from_pair(pair, bought_address)
                    bought_symbol = info.get("symbol", "???")
                    bought_name = info.get("name", "Unknown")
                    dex_name = pair.get("dexId", "Unknown DEX")
                    mcap = pair.get("marketCap") or pair.get("fdv") or 0
                    liquidity = (pair.get("liquidity", {}) or {}).get("usd", 0)

                    counterparty_symbol = get_counterparty_symbol(pair, bought_address)
                    if counterparty_symbol in STABLECOIN_QUOTE_SYMBOLS:
                        print(f"Skipped (token's own pool is against stablecoin "
                              f"{counterparty_symbol}, not ETH): {tx_hash}")
                        continue

                    pair_age_days = get_pair_age_days(pair)
                    if pair_age_days is not None and pair_age_days > MAX_PAIR_AGE_DAYS:
                        print(f"Skipped (pair is {pair_age_days:.1f} days old, "
                              f"above {MAX_PAIR_AGE_DAYS}-day ceiling): {tx_hash}")
                        continue
                else:
                    token_price = get_token_price_usd(chain_cfg["coingecko_platform"], bought_address)
                    bought_symbol = "???"
                    bought_name = "Bilinmiyor (DexScreener'da henüz yok)"
                    dex_name = "Bilinmiyor"
                    mcap = 0
                    liquidity = 0

                if 0.90 <= token_price <= 1.10:
                    print(f"Skipped (price near $1, likely an uncategorized stablecoin): {tx_hash}")
                    continue

                bought_decimals = get_token_decimals(chain_cfg, bought_address)
                received_amount = raw_bought_amount / (10 ** bought_decimals)
                accurate_usd_value = received_amount * token_price if token_price > 0 else usd_value
                if accurate_usd_value < SINGLE_BUY_THRESHOLD_USD:
                    continue

                paid_address = find_paid_token(receipt, bought_address, whale)
                if not paid_address:
                    paid_line = "Karşılığında Verilen: Bilinmiyor (muhtemelen doğrudan native ETH)\n"
                else:
                    paid_symbol = get_token_symbol_label(chain_cfg, paid_address)
                    if paid_symbol.upper() in STABLECOIN_QUOTE_SYMBOLS:
                        print(f"Skipped (paid with stablecoin {paid_symbol}, not ETH): {tx_hash}")
                        continue
                    paid_line = f"Karşılığında Verilen: {paid_symbol}\n"

                price_line = f"${token_price:.8f}" if token_price > 0 else "Bilinmiyor"
                mcap_line = f"${mcap:,.0f}" if mcap > 0 else "Bilinmiyor"
                liquidity_line = f"${liquidity:,.0f}" if liquidity > 0 else "Bilinmiyor"

                creation_time_cache_state = state.setdefault("creation_times", {})
                creation_cache_key = f"{chain}:{bought_address}"
                if mcap > 0 and mcap >= NEW_TOKEN_MCAP_CEILING:
                    creation_ts = None
                elif creation_cache_key in creation_time_cache_state:
                    creation_ts = creation_time_cache_state[creation_cache_key]
                else:
                    creation_ts = get_contract_creation_time(chain_cfg, bought_address)
                    if creation_ts is not None:
                        creation_time_cache_state[creation_cache_key] = creation_ts
                token_age_hours = (time.time() - creation_ts) / 3600 if creation_ts else None
                is_new_token = token_age_hours is not None and token_age_hours <= NEW_TOKEN_AGE_HOURS
                age_line = f"Kontrat Yaşı: {token_age_hours:.1f} saat\n" if token_age_hours is not None else ""

                if mcap == 0 and not is_new_token:
                    pending = state.setdefault("pending_mcap_recheck", {})
                    pending_key = f"{chain}:{bought_address}"
                    pending[pending_key] = pending.get(pending_key, 0) + 1
                    if pending[pending_key] <= 3:
                        print(f"Skipped for now (no mcap data yet, retry #{pending[pending_key]} of 3): {tx_hash}")
                        continue
                    print(f"Skipped permanently (no mcap data after 3 retries, likely stale/old token): {tx_hash}")
                    continue

                if mcap > 0 and mcap < MIN_MCAP_USD:
                    print(f"Skipped (mcap ${mcap:,.0f} below minimum ${MIN_MCAP_USD:,.0f}): {tx_hash}")
                    continue

                if is_new_token:
                    priority = "🆕"
                    header = "TAZE ÇIKAN BALİNA ALIMI"
                elif accurate_usd_value >= LARGE_BUY_THRESHOLD_USD:
                    priority = "🚨"
                    header = "Yeni Balina Alımı"
                else:
                    priority = "🟢"
                    header = "Yeni Balina Alımı"

                watch_key = f"{chain}:{bought_address}"
                if token_price > 0 and watch_key not in state.setdefault("price_watch", {}):
                    state["price_watch"][watch_key] = {
                        "baseline_price": token_price,
                        "first_seen_ts": int(time.time()),
                        "symbol": bought_symbol,
                        "name": bought_name,
                    }

                send_telegram(
                    f"{priority} <b>{header}</b> [{chain.upper()}]\n\n"
                    f"<pre>"
                    f"Satın Alınan Coin: {bought_name} ({bought_symbol})\n"
                    f"Kontrat: {bought_address}\n\n"
                    f"Alınan: {received_amount:,.4f} {bought_symbol}\n"
                    f"USD: ${accurate_usd_value:,.0f}\n"
                    f"{paid_line}"
                    f"\nFiyat: {price_line}\n"
                    f"Market Cap: {mcap_line}\n"
                    f"Likidite: {liquidity_line}\n"
                    f"DEX: {dex_name}\n"
                    f"{age_line}"
                    f"</pre>\n"
                    f"Balina: {whale}\n"
                    f"Tx: {explorer}/tx/{tx_hash}\n"
                    f"Grafik: https://dexscreener.com/{dex_id}/{bought_address}"
                )

                count, total_usd, tier_label = update_cluster(state, f"{chain}:{bought_address}", whale, accurate_usd_value)
                if tier_label:
                    send_telegram(
                        f"{tier_label} [{chain.upper()}]\n\n"
                        f"Coin: {bought_name} ({bought_symbol})\n"
                        f"Kontrat: {bought_address}\n"
                        f"Farklı Balina Sayısı: {count}\n"
                        f"Toplam Alım: ${total_usd:,.0f}\n"
                        f"Grafik: https://dexscreener.com/{dex_id}/{bought_address}"
                    )
                continue

            if from_address in exchange_addresses or to_address in exchange_addresses:
                print(f"Skipped (exchange, static list): {tx_hash}")
                continue

            if usd_value < ANALYZE_SWAP_MIN_USD:
                continue

            should_skip, swap_note = analyze_swap(chain_cfg, tx_hash, contract, to_address)
            if should_skip:
                print(f"Skipped (not a real single-whale DEX buy): {tx_hash}")
                continue

            if usd_value >= LARGE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🚨 <b>Büyük Cüzdan Hareketi</b> [{chain.upper()}]\n"
                    f"Cüzdan: {to_address}\n"
                    f"Coin: {symbol}\n"
                    f"Miktar: {amount:,.2f} (${usd_value:,.0f})\n"
                    f"{swap_note}"
                    f"Tx: {explorer}/tx/{tx_hash}\n"
                    f"Grafik: https://dexscreener.com/{dex_id}/{contract}"
                )
            elif usd_value >= SINGLE_BUY_THRESHOLD_USD:
                send_telegram(
                    f"🟢 <b>Cüzdan Hareketi</b> [{chain.upper()}]\n"
                    f"Cüzdan: {to_address}\n"
                    f"Coin: {symbol}\n"
                    f"Miktar: {amount:,.2f} (${usd_value:,.0f})\n"
                    f"{swap_note}"
                    f"Tx: {explorer}/tx/{tx_hash}\n"
                    f"Grafik: https://dexscreener.com/{dex_id}/{contract}"
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
                        f"🟡 <b>Parça Parça Birikim Tespit Edildi</b> [{chain.upper()}]\n"
                        f"Cüzdan: {to_address}\n"
                        f"Coin: {symbol}\n"
                        f"Toplam: ${acc['total_usd']:,.0f} ({acc['count']} işlemde)\n"
                        f"{swap_note}"
                        f"Son Tx: {explorer}/tx/{tx_hash}\n"
                        f"Grafik: https://dexscreener.com/{dex_id}/{contract}"
                    )
                    acc["total_usd"] = 0.0
                    acc["since"] = now
                    acc["count"] = 0

        tstate["last_block"] = to_block
        print(f"[{chain}] {symbol}: this token took {time.time() - token_start_time:.1f}s")

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
