import os
import requests

ETHERSCAN_KEY = os.environ["ETHERSCAN_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WALLET = os.environ.get("WATCH_WALLET", "0xF977814e90dA44bFA03b6295A0616a897441aceC")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})

def main():
    params = {
        "module": "account",
        "action": "tokentx",
        "address": WALLET,
        "sort": "desc",
        "apikey": ETHERSCAN_KEY,
    }
    r = requests.get("https://api.etherscan.io/api", params=params, timeout=20)
    data = r.json()
    txs = data.get("result", [])
    if not isinstance(txs, list) or not txs:
        print("No transactions found or API error:", data.get("message"))
        return

    tx = txs[0]
    decimals = int(tx.get("tokenDecimal", 18) or 18)
    amount = int(tx.get("value", 0)) / (10 ** decimals)
    msg = (
        f"🚨 <b>Whale Alert (test)</b>\n"
        f"Wallet: {WALLET}\n"
        f"Token: {tx.get('tokenSymbol')}\n"
        f"Amount: {amount}\n"
        f"Tx: https://etherscan.io/tx/{tx.get('hash')}"
    )
    send_telegram(msg)
    print("Alert sent.")

if __name__ == "__main__":
    main()
