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
