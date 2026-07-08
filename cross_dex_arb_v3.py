"""
cross_dex_arb_v3.py - tehostettu versio

MUUTOKSET v2:een:
1. Multicall3: molemmat pool-hinnat + block-numero haetaan YHDELLA
   RPC-kutsulla (ennen 2 erillista). Puolittaa API-kayton ja takaa
   etta molemmat hinnat ovat samasta blockista (aito spread).
2. Sama block -> skip: ei turhaa laskentaa jos ketju ei ole edennyt.
3. Tarkka poll-tahti: sleep huomioi RPC:hen kuluneen ajan.
4. Konsoliloki vain kun arvot muuttuvat (jsonl tallentaa kaiken silti).
5. RPC-timeout ettei looppi jaa jumiin.
"""

import time
import json
import logging
from collections import deque
from decimal import Decimal, getcontext
from statistics import mean, pstdev

from web3 import Web3

getcontext().prec = 50

# ----------------------------------------------------------------------
# KONFIGURAATIO
# ----------------------------------------------------------------------

RPC_URL = "https://137.rpc.thirdweb.com/637c099dc97da50666475de7ae1022de"
POLL_INTERVAL_SECONDS = 10

WETH = Web3.to_checksum_address("0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619")
USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
WETH_DECIMALS = 18
USDC_DECIMALS = 6

UNISWAP_V3_FACTORY = Web3.to_checksum_address("0x1F98431c8aD98523631AE4a59f267346ea31F984")
UNISWAP_FEE_TIER = 500

ALGEBRA_FACTORY = Web3.to_checksum_address("0x411b0fAcC3489691f28ad58c47006AF5E3Ab3A28")

# Multicall3 - sama osoite kaikilla EVM-ketjuilla
MULTICALL3 = Web3.to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")

TRADE_SIZE_WETH = 1.0
FLASH_LOAN_FEE_BPS = 5
MIN_PROFIT_USD = 2.0
GAS_ESTIMATE_USD = 0.05

HISTORY_WINDOW = 60
Z_SCORE_THRESHOLD = 1.5

LOG_FILE = "arb_history.jsonl"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("arb")

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))

# ----------------------------------------------------------------------
# ABI:t
# ----------------------------------------------------------------------

UNISWAP_FACTORY_ABI = json.loads('[{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"}],"name":"getPool","outputs":[{"internalType":"address","name":"pool","type":"address"}],"stateMutability":"view","type":"function"}]')

ALGEBRA_FACTORY_ABI = json.loads('[{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"}],"name":"poolByPair","outputs":[{"internalType":"address","name":"pool","type":"address"}],"stateMutability":"view","type":"function"}]')

TOKEN0_ABI = json.loads('[{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]')

MULTICALL3_ABI = json.loads('[{"inputs":[{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bool","name":"allowFailure","type":"bool"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call3[]","name":"calls","type":"tuple[]"}],"name":"aggregate3","outputs":[{"components":[{"internalType":"bool","name":"success","type":"bool"},{"internalType":"bytes","name":"returnData","type":"bytes"}],"internalType":"struct Multicall3.Result[]","name":"returnData","type":"tuple[]"}],"stateMutability":"payable","type":"function"}]')

# Funktioselektorit (4 tavua) - lasketaan ajonaikaisesti, ei arvailua
SLOT0_CD = Web3.keccak(text="slot0()")[:4]
GLOBALSTATE_CD = Web3.keccak(text="globalState()")[:4]
BLOCKNUM_CD = Web3.keccak(text="getBlockNumber()")[:4]

# ----------------------------------------------------------------------
# Alustus (kertaluontoiset kutsut)
# ----------------------------------------------------------------------

uni_factory = w3.eth.contract(address=UNISWAP_V3_FACTORY, abi=UNISWAP_FACTORY_ABI)
algebra_factory = w3.eth.contract(address=ALGEBRA_FACTORY, abi=ALGEBRA_FACTORY_ABI)
multicall = w3.eth.contract(address=MULTICALL3, abi=MULTICALL3_ABI)

UNI_POOL = uni_factory.functions.getPool(WETH, USDC, UNISWAP_FEE_TIER).call()
QUICK_POOL = algebra_factory.functions.poolByPair(WETH, USDC).call()

UNI_TOKEN0 = w3.eth.contract(address=UNI_POOL, abi=TOKEN0_ABI).functions.token0().call()
QUICK_TOKEN0 = w3.eth.contract(address=QUICK_POOL, abi=TOKEN0_ABI).functions.token0().call()

# Multicall-kutsulista rakennetaan kerran
CALLS = [
    (MULTICALL3, True, BLOCKNUM_CD),
    (UNI_POOL, True, SLOT0_CD),
    (QUICK_POOL, True, GLOBALSTATE_CD),
]


def fetch_snapshot():
    """Hakee block-numeron ja molempien poolien sqrtPricen YHDELLA RPC-kutsulla."""
    results = multicall.functions.aggregate3(CALLS).call()
    for i, (ok, data) in enumerate(results):
        if not ok or len(data) < 32:
            raise RuntimeError(f"Multicall-osakutsu {i} epaonnistui")
    block = int.from_bytes(results[0][1][:32], "big")
    uni_sqrt = int.from_bytes(results[1][1][:32], "big")
    quick_sqrt = int.from_bytes(results[2][1][:32], "big")
    return block, uni_sqrt, quick_sqrt


def sqrt_price_to_usdc_out(sqrt_price_x96: int, token0: str, amount_in_weth_wei: int) -> int:
    """Arvio amountOut (raw USDC) poolin hetkellisesta hinnasta. Ei slippagea."""
    ratio = Decimal(sqrt_price_x96) / (Decimal(2) ** 96)
    price_raw = ratio * ratio
    amount_in_human = Decimal(amount_in_weth_wei) / (Decimal(10) ** WETH_DECIMALS)
    if token0.lower() == WETH.lower():
        price_human = price_raw * (Decimal(10) ** (WETH_DECIMALS - USDC_DECIMALS))
        out_human = amount_in_human * price_human
    else:
        price_human = price_raw * (Decimal(10) ** (USDC_DECIMALS - WETH_DECIMALS))
        out_human = amount_in_human / price_human
    return int(out_human * (Decimal(10) ** USDC_DECIMALS))


class SpreadTimer:
    def __init__(self, window: int = HISTORY_WINDOW):
        self.history = deque(maxlen=window)

    def update_and_score(self, spread_bps: float):
        self.history.append(spread_bps)
        if len(self.history) < 10:
            return 0.0, False
        mu = mean(self.history)
        sigma = pstdev(self.history) or 1e-9
        z = (spread_bps - mu) / sigma
        return z, z >= Z_SCORE_THRESHOLD


def log_observation(record: dict):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def run():
    timer = SpreadTimer()
    amount_in_wei = w3.to_wei(TRADE_SIZE_WETH, "ether")

    log.info("Kaynnistetaan cross-DEX arb-monitori v3 (Multicall3)")
    log.info(f"Uniswap pool: {UNI_POOL}")
    log.info(f"QuickSwap pool: {QUICK_POOL}")
    log.info(f"Poll-vali: {POLL_INTERVAL_SECONDS}s, kauppakoko: {TRADE_SIZE_WETH} WETH")
    log.info("Konsoliin tulostetaan vain muuttuneet arvot; kaikki kirjataan jsonl-tiedostoon")

    last_block = None
    last_printed = None
    suppressed = 0

    while True:
        t0 = time.monotonic()
        try:
            block, uni_sqrt, quick_sqrt = fetch_snapshot()

            if block != last_block:
                last_block = block

                uni_out = sqrt_price_to_usdc_out(uni_sqrt, UNI_TOKEN0, amount_in_wei)
                quick_out = sqrt_price_to_usdc_out(quick_sqrt, QUICK_TOKEN0, amount_in_wei)

                higher, lower = max(uni_out, quick_out), min(uni_out, quick_out)
                spread_bps = ((higher - lower) / lower) * 10_000 if lower else 0

                z, significant = timer.update_and_score(spread_bps)

                buy_dex = "quickswap" if quick_out < uni_out else "uniswap_v3"
                sell_dex = "uniswap_v3" if buy_dex == "quickswap" else "quickswap"

                gross = (higher - lower) / 1e6
                flash_fee = (lower / 1e6) * (FLASH_LOAN_FEE_BPS / 10_000)
                net = gross - flash_fee - GAS_ESTIMATE_USD

                log_observation({
                    "ts": int(time.time()),
                    "block": block,
                    "uniswap_usdc": uni_out / 1e6,
                    "quickswap_usdc": quick_out / 1e6,
                    "spread_bps": round(spread_bps, 2),
                    "z_score": round(z, 2),
                    "buy_dex": buy_dex,
                    "sell_dex": sell_dex,
                    "net_profit_usd": round(net, 4),
                })

                if significant and net >= MIN_PROFIT_USD:
                    log.info(
                        f"SIGNAALI: osta {buy_dex} -> myy {sell_dex} | "
                        f"spread={spread_bps:.1f}bps z={z:.2f} netto=${net:.2f} block={block}"
                    )
                    last_printed = None
                    suppressed = 0
                else:
                    key = (round(uni_out / 1e4), round(quick_out / 1e4))
                    if key != last_printed:
                        if suppressed:
                            log.info(f"(edelliset {suppressed} blockia ennallaan)")
                        log.info(
                            f"Uniswap: {uni_out/1e6:.2f} | QuickSwap: {quick_out/1e6:.2f} USDC | "
                            f"spread={spread_bps:.1f}bps z={z:.2f} netto=${net:.2f}"
                        )
                        last_printed = key
                        suppressed = 0
                    else:
                        suppressed += 1

        except Exception as e:
            log.error(f"Virhe kierroksella: {e}")

        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, POLL_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    run()
