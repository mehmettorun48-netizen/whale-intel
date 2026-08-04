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

LOW_CAP_THRESHOLD_USD = 500_000  # market cap below this = "yeni/düşük cap coin"

NEW_TOKEN_AGE_HOURS = 72  # kontratın deploy'undan bu kadar saat geçmemişse
# "taze çıkan" olarak öne çıkarılır -- market cap'ten bağımsız bir sinyal,
# çünkü hızlı pump eden taze bir coin'in mcap'i düşük cap eşiğini çoktan
# geçmiş olabilir (tam da "01" örneğinde olduğu gibi: mcap zaten $19M).

NEW_TOKEN_MCAP_CEILING = 50_000_000  # bu mcap'in üzerindeki coinler için
# kontrat yaşı hiç sorgulanmaz -- onlarca milyon dolarlık bir mcap'e ulaşmış
# bir coin pratikte zaten "keşfedilmiş" demektir, yaşını bilmenin bir değeri
# yok. Bu eşik, her run'da onlarca aday için gereksiz Etherscan çağrısı
# yapılmasını (ve run süresinin dakikalarca uzamasını) önlüyor.

MAX_DISTINCT_TOKENS_PER_SWAP = 7  # more than this = likely a batch/aggregated
# settlement tx (e.g. CoW Protocol) bundling many unrelated users' trades --
# not a single whale's swap, and not reliably attributable to one wallet.
# Kept generous because single-user aggregator routes (1inch/Paraswap/0x
# splitting one trade across several pools) can legitimately touch 5-6
# distinct tokens without being a multi-user batch.

ETHERSCAN_MIN_INTERVAL = 0.5  # seconds between calls -- Etherscan's free tier
# allows only ~3 requests/sec; without pacing, a busy run fires dozens of
# receipt lookups back-to-back and most of them get rejected with a rate
# limit error, which then looks like "no swap found" everywhere.
_last_etherscan_call = 0.0

ANALYZE_SWAP_MIN_USD = 100  # below this, skip the extra receipt-fetch call
# for directly-tracked tokens -- too small to be worth the API budget or to
# meaningfully affect the accumulation total.

FIRST_RUN_BLOCK_LOOKBACK = 300

MAX_BLOCKS_PER_RUN = 150  # bir run'ın tek seferde tarayabileceği en fazla
# blok sayısı. Bu değer önce 2000'di, ama normal bir run zaten ~25-50 blok
# tarıyor (5 dakikalık cron aralığında üretilen blok sayısı kadar) -- 2000
# blok, bu normal hacmin ~40-80 katı aday coin demekti ve bir run'ı 18+
# dakikaya kadar uzatabiliyordu. Bu da "Save state" hiç çalışamadığı için
# ilerlemenin kaydedilmemesine ve bir sonraki run'ın AYNI birikimi baştan
# taramaya çalışmasına yol açan bir kısır döngü yaratıyordu. 150 gibi küçük
# bir değer, birikim varken bile her run'ın hızlı bitip state'i kaydetmesini
# ve birikimin run run kademeli olarak erimesini sağlıyor.

CLUSTER_WINDOW_SECONDS = 24 * 3600
CLUSTER_TIERS = [
    (10, "🔴 Balinalar Yoğun Alıyor"),
    (5, "🟠 Güçlü Balina Birikimi"),
    (2, "🟡 Balina Birikimi Başladı"),
]

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d82"
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca6"
CURVE_EXCHANGE_TOPIC = "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140"
# Covers Uniswap V2, Uniswap V3, and every fork that reuses their Swap event
# signature (Sushiswap, PancakeSwap V2/V3 on BSC, etc.), plus Curve pools.
# Does NOT cover Balancer, 0x/1inch's own settlement events, or other AMM
# designs with different event signatures -- those would need their own
# verified topic hashes.
SWAP_TOPICS = (V2_SWAP_TOPIC, V3_SWAP_TOPIC, CURVE_EXCHANGE_TOPIC)

# Per-chain configuration. Adding a new EVM chain is just adding an entry
# here (as long as Etherscan's v2 API and DexScreener both support it).
CHAINS = {
    "ethereum": {
        "chain_id": 1,
        "native_wrapped_address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "native_wrapped_symbol": "WETH",
        "discovery_addresses": {
            "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
            # DAI/USDe/PYUSD/USDC/USDT hepsi denendi, hepsi kaldırıldı --
            # her ek keşif tetikleyicisi run süresini artırıyor ve
            # cron-job.org'un 5 dakikalık tetikleme aralığıyla çakışıp
            # run'ların kuyrukta iptal edilmesine yol açıyordu. WETH tek
            # başına zaten en zengin keşif kaynağı (en çok altcoin
            # WETH'e karşı işlem görüyor).
        },
        "dexscreener_id": "ethereum",
        "coingecko_platform": "ethereum",
        "explorer": "https://etherscan.io",
    },
    "bsc": {
        "chain_id": 56,
        "native_wrapped_address": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        "native_wrapped_symbol": "WBNB",
        "discovery_addresses": {
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        },
        "dexscreener_id": "bsc",
        "coingecko_platform": "binance-smart-chain",
        "explorer": "https://bscscan.com",
    },
}

STATE_FILE = "data/state.json"
TOKENS_FILE = "config/tokens.txt"
EXCHANGES_FILE = "config/exchanges.txt"

_price_cache = {}
_receipt_cache = {}
_token_pair_cache = {}


def etherscan_call(chain_id, params, timeout=20):
    """Single choke point for every Etherscan API request, so we can pace
    them and never blow past the rate limit (shared across all chains,
    since it's the same account/key)."""
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


def load_state():
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_tokens():
    """Format: chain,address,symbol,decimals -- e.g.
    ethereum,0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2,WETH,18"""
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


def get_tx_receipt(chain_id, tx_hash):
    """Fetches (and caches) the full transaction receipt, including all logs
    and the tx sender. Used both to confirm a real swap happened and to find
    which token the whale actually ended up receiving."""
    cache_key = (chain_id, tx_hash)
    if cache_key in _receipt_cache:
        return _receipt_cache[cache_key]
    result = {}
    try:
        data = etherscan_call(chain_id, {
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
    """Confirms this transaction contains an actual DEX Swap event, not just
    a token transfer that happens to pass through a DEX-adjacent address
    (which also happens on liquidity add/remove -- those are NOT purchases)."""
    logs = receipt.get("logs", [])
    for log in logs:
        topic0 = (log.get("topics") or [""])[0]
        if topic0 in SWAP_TOPICS:
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


def find_bought_transfer(receipt, native_wrapped_address):
    """Finds the LAST Transfer log in the receipt whose token isn't the
    chain's wrapped native token. Returns (token_address, recipient, raw_amount).
    This works regardless of whether the swap went straight to a pool or was
    routed through an aggregator/router/solver -- the final leg of any swap
    is always a Transfer of the output token to whoever actually receives it,
    which is more reliable than assuming tx.from is the real trader (that's
    false for CoW Protocol, 1inch Fusion, and other intent-based DEXes where
    a solver -- not the user -- submits the transaction)."""
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


def get_token_decimals(chain_id, token_address):
    """Reads a token's decimals() directly from the contract via eth_call,
    for tokens we discover dynamically (not pre-configured in tokens.txt)."""
    cache_key = (chain_id, token_address)
    if cache_key in _decimals_cache:
        return _decimals_cache[cache_key]
    decimals = 18
    try:
        data = etherscan_call(chain_id, {
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


def get_contract_creation_time(chain_id, token_address):
    """Returns the unix timestamp of the token contract's deployment, or
    None if it can't be determined. Used to catch genuinely NEW tokens --
    market cap alone can't do this, since a token can pump hard in its
    first hours and blow past the low-cap threshold before we ever see it."""
    cache_key = (chain_id, token_address)
    if cache_key in _creation_time_cache:
        return _creation_time_cache[cache_key]
    creation_ts = None
    try:
        data = etherscan_call(chain_id, {
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": token_address,
        })
        result = data.get("result")
        if isinstance(result, list) and result:
            tx_hash = result[0].get("txHash")
            if tx_hash:
                receipt = get_tx_receipt(chain_id, tx_hash)
                block_hex = receipt.get("blockNumber")
                if block_hex:
                    block_data = etherscan_call(chain_id, {
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


# ---------------------------------------------------------------------------
# Infrastructure-token classifier
#
# Goal: tell a genuine tradeable altcoin/memecoin apart from DeFi
# infrastructure tokens (liquid staking, liquid restaking, wrapped assets,
# synthetic assets, stablecoins, yield/vault tokens, receipt tokens, etc.)
# WITHOUT a per-symbol blacklist, so it keeps working on tokens that don't
# exist yet. Two independent signals are combined:
#
#   1. On-chain contract behavior (ERC4626 vaults expose asset(); wrapped/
#      receipt tokens commonly expose underlying()). This looks at what the
#      contract actually IS, not what it's named.
#   2. CoinGecko's own, continuously-updated category taxonomy. New staking/
#      restaking/wrapped tokens get classified there as they launch, so this
#      adapts automatically -- we only hardcode which CATEGORY TYPES to
#      exclude (matching the token *types* requested), never token names.
#
# This is a heuristic, not a proof. It will occasionally miss an infra token
# CoinGecko hasn't categorized yet, or (rarely) exclude a legitimate token
# that happens to expose one of these functions for unrelated reasons. Given
# the choice, we bias toward excluding when uncertain, per user preference.
# ---------------------------------------------------------------------------

EXCLUDED_CATEGORY_KEYWORDS = [
    "liquid staking", "liquid restaking", "restaking", "staking",
    "wrapped-tokens", "wrapped", "stablecoin", "synthetic",
    "yield aggregator", "yield farming", "yield", "vault",
    "lp tokens", "liquidity provider", "bridged", "rebase",
    "receipt token", "tokenized btc", "tokenized eth", "interest bearing",
]


def call_view_function(chain_id, token_address, selector):
    """Calls a zero-argument view function via eth_call and returns the raw
    hex result, or None if the call reverted / the function doesn't exist."""
    try:
        data = etherscan_call(chain_id, {
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


def is_erc4626_or_wrapped(chain_id, token_address):
    """Checks contract behavior directly: ERC4626 vaults expose asset(), and
    most wrapped/receipt tokens expose underlying() -- both return the
    address of the token they represent/wrap. Neither is name-based."""
    if _decodes_to_address(call_view_function(chain_id, token_address, "0x38d52e0f")):
        return True, "ERC4626 vault (asset() fonksiyonu var)"
    if _decodes_to_address(call_view_function(chain_id, token_address, "0x6f307dc3")):
        return True, "Wrapped/receipt token (underlying() fonksiyonu var)"
    return False, ""


_cg_data_cache = {}


def get_coingecko_token_data(coingecko_platform, token_address):
    """Fetches (and caches) the full CoinGecko contract-lookup response for a
    token once, so categories and exchange-listing checks share a single API
    call instead of hitting CoinGecko twice per token."""
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
    """CoinGecko's own category taxonomy for a token -- updates on its own
    as new tokens launch and get classified, no code changes needed here."""
    data = get_coingecko_token_data(coingecko_platform, token_address)
    return data.get("categories") or []


MAJOR_CEX_NAMES = [
    "binance", "coinbase", "kraken", "okx", "bybit", "upbit", "kucoin",
    "bitget", "gate.io", "gateio", "htx", "huobi", "bitfinex", "crypto.com",
    "mexc", "bitstamp",
]
# Lowercased substrings matched against CoinGecko's exchange "market" name
# per ticker. Substring match (not exact) because CoinGecko sometimes
# suffixes regional/entity variants (e.g. "Binance US").


def is_cex_listed(coingecko_platform, token_address):
    """Returns True if the token already trades on a major centralized
    exchange, per CoinGecko's ticker data. Used to keep alerts to genuinely
    DEX-only, unlisted tokens -- once a coin lands on a major CEX it's no
    longer the kind of early/undiscovered signal being looked for here."""
    data = get_coingecko_token_data(coingecko_platform, token_address)
    tickers = data.get("tickers") or []
    for ticker in tickers:
        market_name = ((ticker.get("market") or {}).get("name") or "").lower()
        for cex in MAJOR_CEX_NAMES:
            if cex in market_name:
                return True
    return False


def is_infrastructure_token(chain_cfg, token_address):
    """Returns (should_exclude, reason). Checks the CHEAP signal first
    (CoinGecko's categories, a single non-Etherscan API call) and only pays
    for the two Etherscan eth_call probes (asset()/underlying()) if the
    category check didn't already answer -- most common staking/wrapped
    counterparties to WETH (stETH, wstETH, rETH, weETH, etc.) are already
    categorized on CoinGecko, so this skips 2 Etherscan calls for them."""
    categories = get_coingecko_categories(chain_cfg["coingecko_platform"], token_address)
    for cat in categories:
        cat_lower = cat.lower()
        for keyword in EXCLUDED_CATEGORY_KEYWORDS:
            if keyword in cat_lower:
                return True, f"CoinGecko kategorisi: {cat}"

    is_wrapped, reason = is_erc4626_or_wrapped(chain_cfg["chain_id"], token_address)
    if is_wrapped:
        return True, reason

    return False, ""



def get_dex_pool_addresses(dexscreener_id, token_address):
    """Returns the set of known AMM pool/pair contract addresses for a token,
    so we can tell a real trader apart from pool infrastructure. Needed
    because a pool receiving a token (someone selling INTO it) looks
    identical, at the single-Transfer-log level, to a whale receiving it."""
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
    """Looks up DexScreener pairs by the TOKEN's contract address (not a pool
    address), filtered to the right chain."""
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


def get_latest_block(chain_id):
    data = etherscan_call(chain_id, {"module": "proxy", "action": "eth_blockNumber"})
    result = data.get("result", "0x0")
    return int(result, 16)


def fetch_logs(chain_id, address, topic0, from_block, to_block, _depth=0):
    """Etherscan's getLogs caps out at 1000 results per call. If we hit that
    cap, it means there's more data we haven't seen -- so we split the block
    range in half and fetch each half separately, recursively, until every
    chunk comes back under the cap. Without this, busy tokens (like WETH)
    silently lose whatever transfers happened to fall past the 1000th log,
    which could easily be the exact whale buy we're looking for."""
    data = etherscan_call(chain_id, {
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
        left = fetch_logs(chain_id, address, topic0, from_block, mid, _depth + 1)
        right = fetch_logs(chain_id, address, topic0, mid + 1, to_block, _depth + 1)
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


def get_token_symbol_label(chain_cfg, token_address):
    """Best-effort human-readable symbol for a token address, without
    failing hard if DexScreener doesn't know it."""
    if token_address == chain_cfg["native_wrapped_address"]:
        return chain_cfg["native_wrapped_symbol"]
    pair = check_dex_token(chain_cfg["dexscreener_id"], token_address)
    if pair:
        info, _ = resolve_token_info_from_pair(pair, token_address)
        symbol = info.get("symbol")
        if symbol:
            return symbol
    return token_address[:10] + "..."


def analyze_swap(chain_cfg, tx_hash, target_token_address, recipient_address):
    """For a plain (non-native-wrapped-triggered) tracked-token transfer,
    checks whether it was part of a real DEX swap AND whether the tracked
    token actually ended up with a real trader (a BUY) rather than flowing
    into a pool (a SELL of the tracked token -- not what we want to alert
    on). Uses known pool addresses rather than tx.from, since tx.from is a
    solver/relayer -- not the actual trader -- on intent-based DEXes like
    CoW Protocol or 1inch Fusion. Returns (should_skip, note)."""
    receipt = get_tx_receipt(chain_cfg["chain_id"], tx_hash)
    if not receipt or not has_swap_event(receipt):
        return True, ""  # not a swap at all -- nothing to alert on

    if is_batch_settlement(receipt):
        return True, ""  # can't attribute to one whale, skip alerting entirely

    pool_addresses = get_dex_pool_addresses(chain_cfg["dexscreener_id"], target_token_address)
    if recipient_address in pool_addresses:
        # The tracked token went INTO a pool -- someone sold it, not a buy.
        return True, ""

    paid_address = find_paid_token(receipt, target_token_address, recipient_address)
    if not paid_address:
        return False, "⚠️ DEX swap'ı ama karşılığı tespit edilemedi\n"
    paid_symbol = get_token_symbol_label(chain_cfg, paid_address)
    return False, f"DEX'te Karşılığında Verilen: {paid_symbol}\n"


def main():
    state = load_state()
    tokens = load_tokens()
    exchange_addresses = load_exchanges()

    chains_in_use = sorted({t[0] for t in tokens})
    latest_blocks = {chain: get_latest_block(CHAINS[chain]["chain_id"]) for chain in chains_in_use}
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
        if to_block - from_block > MAX_BLOCKS_PER_RUN:
            to_block = from_block + MAX_BLOCKS_PER_RUN
            print(f"[{chain}] {symbol}: backlog too large ({latest_block - from_block} blocks), capping this run to {from_block}-{to_block}")

        logs = fetch_logs(chain_cfg["chain_id"], contract, TRANSFER_TOPIC, from_block, to_block)
        print(f"[{chain}] {symbol}: scanned blocks {from_block}-{to_block}, found {len(logs)} transfer logs")

        price = get_token_price_usd(chain_cfg["coingecko_platform"], contract)
        print(f"[{chain}] {symbol}: price=${price}")

        is_discovery_trigger = contract in chain_cfg["discovery_addresses"]

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

            if is_discovery_trigger:
                if usd_value < SINGLE_BUY_THRESHOLD_USD:
                    continue

                receipt = get_tx_receipt(chain_cfg["chain_id"], tx_hash)
                if not receipt or not has_swap_event(receipt):
                    continue

                if is_batch_settlement(receipt):
                    print(f"Skipped (batch/aggregated settlement, not a single whale trade): {tx_hash}")
                    continue

                bought_address, whale, raw_bought_amount = find_bought_transfer(
                    receipt, contract
                )
                if not bought_address or not whale:
                    print(f"Skipped (swap detected but couldn't trace bought token): {tx_hash}")
                    continue

                if bought_address in tracked_by_chain.get(chain, set()):
                    # Already directly tracked as its own token entry -- that
                    # branch handles it (more accurately, since it isn't
                    # anchored to this particular native-token leg of a
                    # multi-hop route). Avoid a duplicate/inconsistent alert.
                    continue

                if not whale or whale in exchange_addresses:
                    continue

                pool_addresses = get_dex_pool_addresses(dex_id, bought_address)
                if whale in pool_addresses:
                    # The bought token actually went to a pool, not a real
                    # trader (e.g. this native-token leg was really someone
                    # else selling bought_address into liquidity).
                    continue

                infra_cache_state = state.setdefault("infra_status", {})
                infra_cache_key = f"{chain}:{bought_address}"
                if infra_cache_key in infra_cache_state:
                    is_infra, infra_reason = infra_cache_state[infra_cache_key]
                else:
                    # infra durumu bir tokenin özelliği, zamanla değişmez --
                    # bu yüzden state.json'a kalıcı olarak kaydediyoruz.
                    # WETH'in en sık karşılaştığı sahte-altcoin'ler (stETH,
                    # wstETH, weETH gibi) her run'da tekrar tekrar aday
                    # olarak geliyordu; artık ilk kontrolden sonra bir daha
                    # asla ne CoinGecko'ya ne Etherscan'e sorulmuyorlar.
                    is_infra, infra_reason = is_infrastructure_token(chain_cfg, bought_address)
                    infra_cache_state[infra_cache_key] = [is_infra, infra_reason]
                if is_infra:
                    print(f"Skipped (infrastructure/staking/wrapped token - {infra_reason}): {tx_hash}")
                    continue

                if is_cex_listed(chain_cfg["coingecko_platform"], bought_address):
                    # Already trades on a major centralized exchange -- not
                    # the DEX-only, genuinely undiscovered signal the person
                    # wants to see.
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
                else:
                    token_price = get_token_price_usd(chain_cfg["coingecko_platform"], bought_address)
                    bought_symbol = "???"
                    bought_name = "Bilinmiyor (DexScreener'da henüz yok)"
                    dex_name = "Bilinmiyor"
                    mcap = 0
                    liquidity = 0

                if 0.90 <= token_price <= 1.10:
                    # Trading within a few percent of $1 -- almost certainly
                    # a stablecoin/synthetic-dollar token that CoinGecko
                    # hasn't categorized yet (e.g. DUSD "Dialectic USD"),
                    # not a genuine altcoin whale buy. A real altcoin
                    # landing exactly on a dollar peg by coincidence is
                    # essentially never worth alerting on either way.
                    print(f"Skipped (price near $1, likely an uncategorized stablecoin): {tx_hash}")
                    continue

                # Compute the REAL received amount/USD value from the bought
                # token's own transfer + decimals, not from whichever native-
                # token leg happened to trigger this scan -- on multi-hop
                # routes (e.g. USDC->WETH->TOKEN) that leg's value can be
                # very different from the whale's actual total spend.
                bought_decimals = get_token_decimals(chain_cfg["chain_id"], bought_address)
                received_amount = raw_bought_amount / (10 ** bought_decimals)
                accurate_usd_value = received_amount * token_price if token_price > 0 else usd_value
                if accurate_usd_value < SINGLE_BUY_THRESHOLD_USD:
                    # The native-token leg looked big, but the whale's actual
                    # total buy (once traced properly) doesn't clear the bar.
                    continue

                paid_address = find_paid_token(receipt, bought_address, whale)
                if not paid_address:
                    # Couldn't confirm this whale actually paid something to
                    # receive the token -- likely not a genuine swap for this
                    # address (e.g. a bridge withdrawal, fee transfer, or
                    # relayer/settlement contract doing something unrelated).
                    print(f"Skipped (couldn't confirm a two-sided trade for the whale): {tx_hash}")
                    continue
                paid_symbol = get_token_symbol_label(chain_cfg, paid_address)
                paid_line = f"Karşılığında Verilen: {paid_symbol}\n"

                price_line = f"${token_price:.8f}" if token_price > 0 else "Bilinmiyor"
                mcap_line = f"${mcap:,.0f}" if mcap > 0 else "Bilinmiyor"
                liquidity_line = f"${liquidity:,.0f}" if liquidity > 0 else "Bilinmiyor"

                creation_time_cache_state = state.setdefault("creation_times", {})
                creation_cache_key = f"{chain}:{bought_address}"
                if mcap > 0 and mcap >= NEW_TOKEN_MCAP_CEILING:
                    # Already well-established by market cap -- not worth
                    # the two extra Etherscan calls to find out just how
                    # old it is; it's never going to qualify as "new".
                    creation_ts = None
                elif creation_cache_key in creation_time_cache_state:
                    creation_ts = creation_time_cache_state[creation_cache_key]
                else:
                    # Bu iki ekstra Etherscan çağrısı (getcontractcreation +
                    # eth_getBlockByNumber), yoğun bir run'da onlarca kez
                    # tekrarlanınca run süresini dakikalarca uzatıyordu --
                    # aynı coin defalarca görülse bile her seferinde yeniden
                    # sorgulanıyordu. Artık state.json'a kalıcı olarak
                    # kaydediyoruz, bir coin'in yaşı bir kere öğrenilince bir
                    # daha asla tekrar sorgulanmıyor. Başarısız aramaları
                    # (None) kaydetmiyoruz, böylece geçici bir API hatası
                    # coin'i kalıcı olarak "yaşı bilinmiyor" durumuna
                    # kilitlemez -- bir sonraki run'da tekrar denenir.
                    creation_ts = get_contract_creation_time(chain_cfg["chain_id"], bought_address)
                    if creation_ts is not None:
                        creation_time_cache_state[creation_cache_key] = creation_ts
                token_age_hours = (time.time() - creation_ts) / 3600 if creation_ts else None
                is_new_token = token_age_hours is not None and token_age_hours <= NEW_TOKEN_AGE_HOURS
                age_line = f"Kontrat Yaşı: {token_age_hours:.1f} saat\n" if token_age_hours is not None else ""

                if mcap == 0 and not is_new_token:
                    # Market cap verisi yok VE coin taze değil -- muhtemelen
                    # DexScreener/CoinGecko'nun geçici bir hatası, gerçek bir
                    # "düşük cap" sinyali değil (örn. swETH gibi köklü ama o an
                    # verisi çekilemeyen bir token). Yanlış "ERKEN BALİNA"
                    # etiketiyle göndermek yerine hiç göndermiyoruz.
                    print(f"Skipped (no mcap data and not a new token, likely stale/old token): {tx_hash}")
                    continue

                is_low_cap = 0 < mcap < LOW_CAP_THRESHOLD_USD

                if is_new_token:
                    priority = "🆕"
                    header = "TAZE ÇIKAN BALİNA ALIMI"
                elif is_low_cap:
                    priority = "🔥"
                    header = "ERKEN BALİNA - DÜŞÜK CAP"
                elif accurate_usd_value >= LARGE_BUY_THRESHOLD_USD:
                    priority = "🚨"
                    header = "Yeni Balina Alımı"
                else:
                    priority = "🟢"
                    header = "Yeni Balina Alımı"

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
