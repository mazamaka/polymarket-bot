"""Redeem resolved positions on Polymarket CTF contract.

Handles both regular and NegRisk markets via on-chain transactions
on Polygon network.
"""

import logging
import time

import requests
from eth_abi import encode
from eth_account import Account

from config import settings

logger = logging.getLogger(__name__)

# Contracts
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEGRISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
POLYGON_RPC_FALLBACK = "https://polygon-rpc.com"

# Function selectors (keccak256 of signature, first 4 bytes)
REGULAR_SELECTOR = bytes.fromhex("01b7037c")
NEGRISK_SELECTOR = bytes.fromhex("dbeccb23")

# Rate limiting
_last_redeem_ts: float = 0.0
_REDEEM_COOLDOWN: int = 600  # 10 minutes between redeem runs


def _rpc_call(method: str, params: list, retries: int = 2) -> dict:
    """Send JSON-RPC call to Polygon with retry and fallback."""
    rpcs = [POLYGON_RPC, POLYGON_RPC_FALLBACK]
    last_error = None

    for attempt in range(retries + 1):
        rpc_url = rpcs[attempt % len(rpcs)]
        try:
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
            resp = requests.post(rpc_url, json=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                raise RuntimeError(f"RPC error: {result['error']}")
            return result
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            if attempt < retries:
                time.sleep(1)
    raise RuntimeError(f"RPC failed after {retries + 1} attempts: {last_error}")


def _get_gas_price() -> int:
    result = _rpc_call("eth_gasPrice", [])
    return int(result["result"], 16)


def _get_nonce(address: str) -> int:
    result = _rpc_call("eth_getTransactionCount", [address, "pending"])
    return int(result["result"], 16)


def _estimate_gas(wallet: str, to_address: str, calldata: bytes) -> int:
    """Estimate gas for transaction. Returns 0 if tx would revert."""
    try:
        result = _rpc_call(
            "eth_estimateGas",
            [{"from": wallet, "to": to_address, "data": "0x" + calldata.hex()}],
        )
        estimated = int(result["result"], 16)
        return int(estimated * 1.3)  # 30% buffer
    except RuntimeError as e:
        logger.warning("Gas estimation failed (tx would revert?): %s", e)
        return 0


def _is_neg_risk(token_id: str) -> bool:
    try:
        resp = requests.get(
            "https://clob.polymarket.com/neg-risk",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("neg_risk", False)
    except requests.RequestException as e:
        logger.warning("neg-risk check failed for %s: %s", token_id[:16], e)
        return False


def _build_regular_calldata(condition_id: str) -> bytes:
    """Build calldata for regular CTF redeemPositions."""
    condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))
    if len(condition_bytes) > 32:
        raise ValueError(f"condition_id too long: {len(condition_bytes)} bytes")
    condition_bytes = condition_bytes.rjust(32, b"\x00")

    encoded = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_ADDRESS, b"\x00" * 32, condition_bytes, [1, 2]],
    )
    return REGULAR_SELECTOR + encoded


def _build_negrisk_calldata(condition_id: str) -> bytes:
    """Build calldata for NegRisk adapter redeemPositions."""
    condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))
    if len(condition_bytes) > 32:
        raise ValueError(f"condition_id too long: {len(condition_bytes)} bytes")
    condition_bytes = condition_bytes.rjust(32, b"\x00")

    encoded = encode(["bytes32", "uint256[]"], [condition_bytes, [1, 2]])
    return NEGRISK_SELECTOR + encoded


def _send_redeem_tx(
    to_address: str,
    calldata: bytes,
    gas_limit: int,
    nonce: int,
) -> str:
    """Sign and send redeem transaction, return tx hash."""
    account = Account.from_key(settings.polygon_wallet_private_key)
    gas_price = _get_gas_price()

    tx = {
        "nonce": nonce,
        "gasPrice": gas_price,
        "gas": gas_limit,
        "to": to_address,
        "value": 0,
        "data": calldata,
        "chainId": 137,
    }

    signed = account.sign_transaction(tx)
    raw_tx = "0x" + signed.raw_transaction.hex().removeprefix("0x")

    result = _rpc_call("eth_sendRawTransaction", [raw_tx])
    return result["result"]


def _wait_for_receipt(tx_hash: str, timeout: int = 90) -> dict | None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            result = _rpc_call("eth_getTransactionReceipt", [tx_hash], retries=0)
            receipt = result.get("result")
            if receipt is not None:
                return receipt
        except RuntimeError:
            pass
        time.sleep(3)
    return None


def redeem_resolved_positions(positions: list[dict]) -> list[dict]:
    """Redeem all redeemable positions.

    Returns list of result dicts with: question, condition_id, success,
    tx_hash (if success), error (if failed).
    """
    global _last_redeem_ts

    now = time.monotonic()
    if now - _last_redeem_ts < _REDEEM_COOLDOWN:
        remaining = int(_REDEEM_COOLDOWN - (now - _last_redeem_ts))
        logger.info("Redeem cooldown: %d seconds remaining", remaining)
        return []

    redeemable = [p for p in positions if p.get("redeemable")]
    if not redeemable:
        return []

    if not settings.polygon_wallet_private_key:
        return [
            {
                "question": "ALL",
                "condition_id": "",
                "success": False,
                "error": "POLYGON_WALLET_PRIVATE_KEY not configured",
            }
        ]

    # Check native token (POL) balance for gas
    wallet = settings.polygon_wallet_address
    if not wallet:
        wallet = Account.from_key(settings.polygon_wallet_private_key).address

    try:
        result = _rpc_call("eth_getBalance", [wallet, "latest"])
        native_balance = int(result["result"], 16) / 1e18
        if native_balance < 0.01:
            logger.warning(
                "Insufficient POL for gas: %.6f POL. Send POL to %s",
                native_balance,
                wallet,
            )
            return [
                {
                    "question": "ALL",
                    "condition_id": "",
                    "success": False,
                    "error": f"Insufficient POL for gas: {native_balance:.6f}. Send POL to {wallet}",
                }
            ]
    except (RuntimeError, KeyError) as e:
        logger.warning("Gas balance check failed: %s", e)

    results: list[dict] = []
    seen_conditions: set[str] = set()
    nonce = _get_nonce(wallet)

    for pos in redeemable:
        condition_id = pos.get("market_id", "") or pos.get("conditionId", "")
        question = pos.get("question", "Unknown")[:80]
        token_id = pos.get("token_id", "") or pos.get("asset", "")

        if not condition_id or condition_id in seen_conditions:
            continue
        seen_conditions.add(condition_id)

        try:
            neg_risk = _is_neg_risk(token_id) if token_id else False

            if neg_risk:
                calldata = _build_negrisk_calldata(condition_id)
                to_addr = NEGRISK_ADAPTER
                market_type = "NegRisk"
            else:
                calldata = _build_regular_calldata(condition_id)
                to_addr = CTF_CONTRACT
                market_type = "Regular"

            # Pre-check: estimate gas (detects reverts before spending gas)
            gas_limit = _estimate_gas(wallet, to_addr, calldata)
            if gas_limit == 0:
                logger.info(
                    "SKIP redeem %s: tx would revert (already redeemed?)", question
                )
                results.append(
                    {
                        "question": question,
                        "condition_id": condition_id,
                        "success": False,
                        "error": "Would revert (already redeemed?)",
                    }
                )
                continue

            logger.info(
                "Redeeming %s (%s): %s", question, market_type, condition_id[:16]
            )

            tx_hash = _send_redeem_tx(to_addr, calldata, gas_limit, nonce)
            nonce += 1
            logger.info("Redeem tx sent: %s", tx_hash)

            receipt = _wait_for_receipt(tx_hash)
            if receipt is None:
                results.append(
                    {
                        "question": question,
                        "condition_id": condition_id,
                        "success": False,
                        "tx_hash": tx_hash,
                        "error": "Timeout waiting for receipt",
                    }
                )
                continue

            status = int(receipt.get("status", "0x0"), 16)
            if status == 1:
                results.append(
                    {
                        "question": question,
                        "condition_id": condition_id,
                        "success": True,
                        "tx_hash": tx_hash,
                    }
                )
                logger.info("Redeem SUCCESS: %s | tx: %s", question, tx_hash)
            else:
                results.append(
                    {
                        "question": question,
                        "condition_id": condition_id,
                        "success": False,
                        "tx_hash": tx_hash,
                        "error": "Transaction reverted",
                    }
                )
                logger.warning("Redeem REVERTED: %s | tx: %s", question, tx_hash)

        except (RuntimeError, requests.RequestException, ValueError) as e:
            logger.error("Redeem error for %s: %s", question, e)
            results.append(
                {
                    "question": question,
                    "condition_id": condition_id,
                    "success": False,
                    "error": str(e),
                }
            )

    # Only set cooldown if we actually attempted something
    if results:
        _last_redeem_ts = time.monotonic()

    return results
