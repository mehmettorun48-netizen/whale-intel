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

PRICE_WATCH_MAX_AGE_HOURS = 72  # bir coin ilk bildirimden bu kadar saat
# sonra takip listesinden düşürülür -- liste sınırsız büyümesin diye.

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

MAX_BLOCKS_PER_RUN = 30  # global fallback -- kullanılan asıl değer artık her
# chain'in kendi CHAINS[chain]["max_blocks_per_run"] alanından okunuyor,
# çünkü farklı chainlerin blok üretim hızı çok farklı (Ethereum ~12sn/blok,
# Base ~2sn/blok, Arbitrum ~0.25sn/blok). Bu sabit sadece chain_cfg'de
# max_blocks_per_run tanımlı değilse devreye giriyor.
# Önce 2000, sonra 150 idi -- ama WETH o kadar yüksek hacimli
# ki (150 blokta 6506 transfer logu, Etherscan'in 1000-log limitini bile
# aşıp bölünüyor) 150 bloklukbir "yakalama" parçası bile ~10 dakika
# sürüyordu ve cron aralığıyla çakışıyordu. 30, normal (birikimsiz) bir
# run'ın işlediği hacme (~5 dakikada üretilen blok sayısı) yakın olduğu
# için, birikim varken de her run'ın normal hızda kalmasını sağlıyor --
# birikimin tamamen erimesi daha uzun sürer ama hiçbir run asla çakışmaz.

CLUSTER_WINDOW_SECONDS = 6 * 3600  # önce 24 saatti -- 24 saatlik bir pencerede
# 2 farklı balina alımı çok da anlamlı bir "ani ilgi" sinyali değil (gün
# içine yayılmış olabilir). 6 saate indirmek, sinyali "kısa sürede birden
# fazla balina aynı coin'e ilgi gösteriyor" anlamına gelecek şekilde
# sıkılaştırıyor -- gerçek bir erken uyarı için daha anlamlı.
CLUSTER_TIERS = [
    (10, "🔴 Balinalar Yoğun Alıyor"),
    (5, "🟠 Güçlü Balina Birikimi"),
    (2, "🟡 Balina Birikimi Başladı"),
]

ACCUMULATION_MAX_RANGE_PCT = 10  # son 6 saatte fiyat bu yüzdeden fazla
# hareket etmemişse "dipte akümülasyon/yatay bant" sayılır -- coin sakin,
# birikim aşamasında demektir.
BREAKOUT_MIN_H1_PCT = 15  # son 1 saatte fiyat bu yüzdeden fazla yukarı
# hareket ettiyse, akümülasyon bandını "kırdı" sayılır.
BREAKOUT_MIN_VOLUME_MULTIPLIER = 3  # kırılımın gerçek alım baskısıyla
# desteklendiğini doğrulamak için son 5 dakikalık hacim, normal temposunun
# (son 1 saatlik hacmin 1/12'si) en az bu katı olmalı.
BREAKOUT_COOLDOWN_HOURS = 6  # aynı coin için kırılım bildirimi bu süre
# dolmadan tekrar gönderilmez.

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
# "max_blocks_per_run" chain başına ayrı tanımlanıyor çünkü blok üretim
# hızı chain'den chain'e çok farklı -- aynı sabiti tüm chainlere uygulamak,
# hızlı chainlerde (Base, Arbitrum) backlog'un çok daha hızlı büyümesine
# yol açar.
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
        "max_blocks_per_run": 30,
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
        "max_blocks_per_run": 30,
    },
    "base": {
        "chain_id": 8453,
        "native_wrapped_address": "0x4200000000000000000000000000000000000006",
        "native_wrapped_symbol": "WETH",
        "discovery_addresses": {
            "0x4200000000000000000000000000000000000006",  # WETH (Base)
        },
        "dexscreener_id": "base",
        "coingecko_platform": "base",
        "explorer": "https://basescan.org",
        "max_blocks_per_run": 150,  # Base bloğu ~2sn'de bir üretiliyor
        # (Ethereum'un ~6 katı hız) -- 5 dakikalık cron'da Base'de ~150
        # blok üretiliyor. NOT: Base, Etherscan'in ücretsiz API planında
        # desteklenmiyor -- "Free API access is not supported for this
        # chain" hatası alınıyorsa, Etherscan hesabında Lite plana (veya
        # üstüne) geçilmesi gerekiyor.
    },
    "arbitrum": {
        "chain_id": 42161,
        "native_wrapped_address": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        "native_wrapped_symbol": "WETH",
        "discovery_addresses": {
            "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH (Arbitrum)
        },
        "dexscreener_id": "arbitrum",
        "coingecko_platform": "arbitrum-one",
        "explorer": "https://arbiscan.org",
        "max_blocks_per_run": 300,  # Arbitrum bloğu ~0.25sn'de bir
        # üretiliyor (Ethereum'un ~48 katı hız) -- 5 dakikalık cron'da
        # binlerce blok üretiliyor, bu yüzden çok daha yüksek tutulmalı.
        # Arbitrum, Etherscan'in ücretsiz API planında destekleniyor.
    },
}

STATE_FILE = "data/state.json"
T
