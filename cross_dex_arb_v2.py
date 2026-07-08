"""
cross_dex_arb_ai_timing.py (v2 - pool-price -pohjainen)

Cross-DEX arbitrage-monitori Polygonille (Uniswap V3 vs QuickSwap/Algebra),
tilastollisella ajoituksella.

MUUTOS EDELLISEEN VERSIOON:
Quoter-kontraktien ABI osoittautui liian hauraaksi (Algebra Integral -versioita
on useita, eri parametrein). Tämä versio lukee hinnan suoraan poolin
globalState()/slot0()-funktiosta ja laskee hinnan sqrtPriceX96-arvosta.

HUOM: Tämä EI huomioi kaupan hintavaikutusta (slippage) - se on arvio joka
perustuu poolin senhetkiseen hintaan, ei toteutuvaan kauppaan. Riittää
arb-signaalin tunnistamiseen, mutta ennen oikeaa kauppaa kannattaa vielä
simuloida tarkka kauppa (esim. Remix-forkissa) ennen suorittamista.
"""

import time
import json
import logging
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, getcontext
from statistics import mean, pstdev

from web3 import Web3

getcontext().prec = 50

RPC_URL = "https://137.rpc.thirdweb.com/637c099dc97da50666475de7ae1022de"
POLL_INTERVAL_SECONDS = 10

WETH = Web3.to_checksum_address("0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619")
USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
WETH_DECIMALS = 18
USDC_DECIMALS = 6

UNISWAP_V3_FACTORY = Web3.to_checksum_address("0x1F98431c8aD98523631AE4a59f267346ea31F984")
UNISWAP_FEE_TIER = 500

ALGEBRA_FACTORY = Web3.to_checksum_address("0x411b0fAcC3489691f28ad58c47006AF5E3Ab3A28")

TRADE_SIZE_WETH = 1.0
FLASH_LOAN_FEE_BPS = 5
MIN_PROFIT_USD = 2.0
GAS_ESTIMATE_USD = 0.05

HISTORY_WINDOW = 60
Z_SCORE_THRESHOLD = 1.5

LOG_FILE = "arb_history.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("arb")

w3 = Web3(Web3.HTTPProvider(RPC_URL))

UNISWAP_FACTORY_ABI = json.loads('''
[{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},
{"internalType":"address","name":"tokenB","type":"address"},
{"internalType":"uint24","name":"fee","type":"uint24"}],
"name":"getPool","outputs":[{"internalType":"address","name":"pool","type":"address"}],
"stateMutability":"view","type":"function"}]
''')

UNISWAP_POOL_ABI = json.loads('''
[{"inputs":[],"name":"slot0","outputs":[
{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},
{"internalType":"int24","name":"tick","type":"int24"},
{"internalType":"uint16","name":"observationIndex","type":"uint16"},
{"internalType":"uint16","name":"observationCardinality","type":"uint16"},
{"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"},
{"internalType":"uint8","name":"feeProtocol","type":"uint8"},
{"internalType":"bool","name":"unlocked","type":"bool"}],
"stateMutability":"view","type":"function"},
{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],
"stateMutability":"view","type":"function"}]
''')

ALGEBRA_FACTORY_ABI = json.loads('''
[{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},
{"internalType":"address","name":"tokenB","type":"address"}],
"name":"poolByPair","outputs":[{"internalType":"address","name":"pool","type":"address"}],
"stateMutability":"view","type":"function"}]
''')

ALGEBRA_POOL_ABI = json.loads('''
[{"inputs":[],"name":"globalState","outputs":[
{"internalType":"uint160","name":"price","type":"uint160"},
{"internalType":"int24","name":"tick","type":"int24"},
{"internalType":"uint16","name":"fee","type":"uint16"},
{"internalType":"uint16","name":"timepointIndex","type":"uint16"},
{"internalType":"uint8","name":"communityFeeToken0","type":"uint8"},
{"internalType":"uint8","name":"communityFeeToken1","type":"uint8"},
{"internalType":"bool","name":"unlocked","type":"bool"}],
"stateMutability":"view","type":"function"},
{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],
"stateMutability":"view","type":"function"}]
''')

uni_factory = w3.eth.contract(address=UNISWAP_V3_FACTORY, abi=UNISWAP_FACTORY_ABI)
algebra_factory = w3.eth.contract(address=ALGEBRA_FACTORY, abi=ALGEBRA_FACTORY_ABI)

_uni_pool_addr = uni_factory.functions.getPool(WETH, USDC, UNISWAP_FEE_TIER).call()
_algebra_pool_addr = algebra_factory.functions.poolByPair(WETH, USDC).call()

uni_pool = w3.eth.contract(address=_uni_pool_addr, abi=UNISWAP_POOL_ABI)
algebra_pool = w3.eth.contract(address=_algebra_pool_addr, abi=ALGEBRA_POOL_ABI)

_uni_token0 = uni_pool.functions.token0().call()
_algebra_token0 = algebra_pool.functions.token0().call()


@dataclass
class Quote:
    dex: str
    amount_out: int


def _sqrt_price_to_amount_out(sqrt_price_x96: int, token0: str, amount_in_weth_wei: int) -> int:
    ratio = Decimal(sqrt_price_x96) / (Decimal(2) ** 96)
    price_raw = ratio * ratio

    amount_in_weth_human = Decimal(amount_in_weth_wei) / (Decimal(10) ** WETH_DECIMALS)

    if token0.lower() == WETH.lower():
        price_human = price_raw * (Decimal(10) ** (WETH_DECIMALS - USDC_DECIMALS))
        amount_out_usdc_human = amount_in_weth_human * price_human
    else:
        price_human = price_raw * (Decimal(10) ** (USDC_DECIMALS - WETH_DECIMALS))
        amount_out_usdc_human = amount_in_weth_human / price_human

    return int(amount_out_usdc_human * (Decimal(10) ** USDC_DECIMALS))


def get_uniswap_quote(amount_in_wei: int) -> Quote:
    slot0 = uni_pool.functions.slot0().call()
    sqrt_price_x96 = slot0[0]
    amount_out = _sqrt_price_to_amount_out(sqrt_price_x96, _uni_token0, amount_in_wei)
    return Quote(dex="uniswap_v3", amount_out=amount_out)


def get_quickswap_quote(amount_in_wei: int) -> Quote:
    state = algebra_pool.functions.globalState().call()
    sqrt_price_x96 = state[0]
    amount_out = _sqrt_price_to_amount_out(sqrt_price_x96, _algebra_token0, amount_in_wei)
    return Quote(dex="quickswap", amount_out=amount_out)


class SpreadTimer:
    def __init__(self, window: int = HISTORY_WINDOW):
        self.history = deque(maxlen=window)

    def update_and_score(self, spread_bps: float) -> tuple[float, bool]:
        self.history.append(spread_bps)
        if len(self.history) < 10:
            return 0.0, False
        mu = mean(self.history)
        sigma = pstdev(self.history) or 1e-9
        z = (spread_bps - mu) / sigma
        significant = z >= Z_SCORE_THRESHOLD
        return z, significant


def log_observation(record: dict):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def run():
    timer = SpreadTimer()
    amount_in_wei = w3.to_wei(TRADE_SIZE_WETH, "ether")

    log.info("Käynnistetään cross-DEX arb-monitori (pool-price -pohjainen)")
    log.info(f"Uniswap pool: {_uni_pool_addr}")
    log.info(f"QuickSwap pool: {_algebra_pool_addr}")
    log.info(f"Poll-väli: {POLL_INTERVAL_SECONDS}s, kauppakoko: {TRADE_SIZE_WETH} WETH")

    while True:
        try:
            uni = get_uniswap_quote(amount_in_wei)
            quick = get_quickswap_quote(amount_in_wei)

            higher, lower = max(uni.amount_out, quick.amount_out), min(uni.amount_out, quick.amount_out)
            spread_bps = ((higher - lower) / lower) * 10_000 if lower else 0

            z, significant = timer.update_and_score(spread_bps)

            buy_dex = "quickswap" if quick.amount_out < uni.amount_out else "uniswap_v3"
            sell_dex = "uniswap_v3" if buy_dex == "quickswap" else "quickswap"

            gross_profit_usd = (higher - lower) / 1e6
            flash_fee_usd = (lower / 1e6) * (FLASH_LOAN_FEE_BPS / 10_000)
            net_profit_usd = gross_profit_usd - flash_fee_usd - GAS_ESTIMATE_USD

            record = {
                "ts": int(time.time()),
                "uniswap_usdc": uni.amount_out / 1e6,
                "quickswap_usdc": quick.amount_out / 1e6,
                "spread_bps": round(spread_bps, 2),
                "z_score": round(z, 2),
                "buy_dex": buy_dex,
                "sell_dex": sell_dex,
                "net_profit_usd": round(net_profit_usd, 4),
            }
            log_observation(record)

            if significant and net_profit_usd >= MIN_PROFIT_USD:
                log.info(
                    f"🎯 SIGNAALI: osta {buy_dex} → myy {sell_dex} | "
                    f"spread={spread_bps:.1f}bps z={z:.2f} netto≈${net_profit_usd:.2f}"
                )
            else:
                log.info(
                    f"Uniswap: {uni.amount_out/1e6:.2f} USDC | QuickSwap: {quick.amount_out/1e6:.2f} USDC | "
                    f"spread={spread_bps:.1f}bps z={z:.2f} netto≈${net_profit_usd:.2f}"
                )

        except Exception as e:
            log.error(f"Virhe kierroksella: {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
