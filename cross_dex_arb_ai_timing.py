"""
cross_dex_arb_ai_timing.py

Cross-DEX arbitrage-monitori Polygonille (Uniswap V3 vs QuickSwap/Algebra),
tilastollisella "AI timing" -kerroksella joka päättää MILLOIN kannattaa
yrittää kauppaa, ei pelkkää staattista kynnysarvoa.

REHELLINEN HUOMAUTUS:
Tämä EI ole mustan laatikon ML-malli, koska sellaiseen tarvitaan historiadataa
jota sulla ei vielä ole kerättynä. Sen sijaan tämä käyttää tilastollista
signaalia (rolling z-score + volatiliteetin huomiointi), joka on sama
periaate mitä oikeat arb-botit käyttävät "onko tämä spread poikkeuksellinen
vai normaalia kohinaa" -päätöksessä. Kun sulla on kertynyt dataa
(arb_history.jsonl), voit myöhemmin kouluttaa oikean mallin sen päälle -
tässä on jo valmis koukku sitä varten (score_with_model()).

Riippuvuudet (Termux):
    pip install web3 numpy --break-system-packages

Konfiguroi RPC_URL, TOKEN_A/TOKEN_B ja poolit alla.
"""

import time
import json
import logging
from collections import deque
from dataclasses import dataclass
from statistics import mean, pstdev
from pathlib import Path

from web3 import Web3
from decimal import Decimal, getcontext
import os
from dotenv import load_dotenv

getcontext().prec = 50

load_dotenv()

# ----------------------------------------------------------------------
# KONFIGURAATIO — muokkaa nämä omiin arvoihisi
# ----------------------------------------------------------------------

RPC_URL = os.getenv("RPC_URL")
if not RPC_URL:
    raise RuntimeError("RPC_URL puuttuu .env-tiedostosta")
POLL_INTERVAL_SECONDS = 10

WETH = Web3.to_checksum_address("0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619")
USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# Uniswap V3 QuoterV2 (Polygon)
UNISWAP_QUOTER_V2 = Web3.to_checksum_address("0x61fFE014bA17989E743c5F6cB21bF9697530B21e")
UNISWAP_FEE_TIER = 500  # 0.05%

# QuickSwap V3 (Algebra) Quoter (Polygon) — tarkista ajantasainen osoite
# quickswap docs / polygonscan ennen käyttöä
QUICKSWAP_QUOTER = Web3.to_checksum_address("0xa77aD9f635a3FB3bCCC5E6d1A87cB269746Aba17")

TRADE_SIZE_WETH = 1.0          # simuloidaan tällä koolla
FLASH_LOAN_FEE_BPS = 5         # Aave v3 flash loan fee ~0.05% = 5 bps
MIN_PROFIT_USD = 2.0           # ehdoton lattia gasin päälle
GAS_ESTIMATE_USD = 0.05        # Polygon-gas on halpa, mutta älä unohda

# Tilastollisen ajoituksen parametrit
HISTORY_WINDOW = 60            # kuinka monta viimeisintä havaintoa muistetaan
Z_SCORE_THRESHOLD = 1.5        # kuinka poikkeuksellinen spreadin pitää olla

LOG_FILE = "arb_history.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("arb")

w3 = Web3(Web3.HTTPProvider(RPC_URL))

UNISWAP_QUOTER_ABI = json.loads("""
[{"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},
{"internalType":"address","name":"tokenOut","type":"address"},
{"internalType":"uint256","name":"amountIn","type":"uint256"},
{"internalType":"uint24","name":"fee","type":"uint24"},
{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],
"internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],
"name":"quoteExactInputSingle",
"outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},
{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],
"stateMutability":"nonpayable","type":"function"}]
""")

QUICKSWAP_QUOTER_ABI = json.loads("""
[{"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},
{"internalType":"address","name":"tokenOut","type":"address"},
{"internalType":"uint256","name":"amountIn","type":"uint256"},
{"internalType":"uint160","name":"limitSqrtPrice","type":"uint160"}],
"internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],
"name":"quoteExactInputSingle",
"outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},
{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],
"stateMutability":"nonpayable","type":"function"}]
""")

uniswap_quoter = w3.eth.contract(address=UNISWAP_QUOTER_V2, abi=UNISWAP_QUOTER_ABI)
quickswap_quoter = w3.eth.contract(address=QUICKSWAP_QUOTER, abi=QUICKSWAP_QUOTER_ABI)

# ----------------------------------------------------------------------
# QuickSwap-hinta luetaan SUORAAN poolista (globalState), ei Quoterin
# kautta - Quoter-kutsu revertoi selvittamattomasta ABI-erosta, mutta
# suora pooliluku on todistetusti toimiva (sama tekniikka kuin
# cross_dex_arb_v3.py:ssa).
# ----------------------------------------------------------------------

WETH_DECIMALS = 18
USDC_DECIMALS = 6

ALGEBRA_FACTORY = Web3.to_checksum_address("0x411b0fAcC3489691f28ad58c47006AF5E3Ab3A28")

ALGEBRA_FACTORY_ABI = json.loads(
    '[{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},'
    '{"internalType":"address","name":"tokenB","type":"address"}],'
    '"name":"poolByPair","outputs":[{"internalType":"address","name":"pool","type":"address"}],'
    '"stateMutability":"view","type":"function"}]'
)

ALGEBRA_POOL_ABI = json.loads(
    '[{"inputs":[],"name":"globalState","outputs":['
    '{"internalType":"uint160","name":"price","type":"uint160"},'
    '{"internalType":"int24","name":"tick","type":"int24"},'
    '{"internalType":"uint16","name":"fee","type":"uint16"},'
    '{"internalType":"uint16","name":"timepointIndex","type":"uint16"},'
    '{"internalType":"uint8","name":"communityFeeToken0","type":"uint8"},'
    '{"internalType":"uint8","name":"communityFeeToken1","type":"uint8"},'
    '{"internalType":"bool","name":"unlocked","type":"bool"}],'
    '"stateMutability":"view","type":"function"},'
    '{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],'
    '"stateMutability":"view","type":"function"}]'
)

algebra_factory = w3.eth.contract(address=ALGEBRA_FACTORY, abi=ALGEBRA_FACTORY_ABI)
QUICK_POOL = algebra_factory.functions.poolByPair(WETH, USDC).call()
quick_pool = w3.eth.contract(address=QUICK_POOL, abi=ALGEBRA_POOL_ABI)
QUICK_TOKEN0 = quick_pool.functions.token0().call()


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


@dataclass
class Quote:
    dex: str
    amount_out: int


def get_uniswap_quote(amount_in_wei: int) -> Quote:
    params = {
        "tokenIn": WETH,
        "tokenOut": USDC,
        "amountIn": amount_in_wei,
        "fee": UNISWAP_FEE_TIER,
        "sqrtPriceLimitX96": 0,
    }
    result = uniswap_quoter.functions.quoteExactInputSingle(params).call()
    return Quote(dex="uniswap_v3", amount_out=result[0])


def get_quickswap_quote(amount_in_wei: int) -> Quote:
    global_state = quick_pool.functions.globalState().call()
    sqrt_price_x96 = global_state[0]
    amount_out = sqrt_price_to_usdc_out(sqrt_price_x96, QUICK_TOKEN0, amount_in_wei)
    return Quote(dex="quickswap", amount_out=amount_out)


# ----------------------------------------------------------------------
# Tilastollinen ajoitus ("AI timing" -kerros)
# ----------------------------------------------------------------------

class SpreadTimer:
    """
    Pitää kirjaa viimeisimmistä spread-havainnoista ja laskee z-scoren:
    kuinka monen keskihajonnan päässä nykyinen spread on liukuvasta
    keskiarvosta. Kaupataan vain kun poikkeama on tilastollisesti
    merkittävä eikä pelkkää kohinaa — tämä vähentää turhia epäonnistuneita
    yrityksiä kilpailevia botteja vastaan.
    """

    def __init__(self, window: int = HISTORY_WINDOW):
        self.history = deque(maxlen=window)

    def update_and_score(self, spread_bps: float) -> tuple[float, bool]:
        self.history.append(spread_bps)
        if len(self.history) < 10:
            # ei tarpeeksi dataa vielä luotettavaan z-scoreen
            return 0.0, False

        mu = mean(self.history)
        sigma = pstdev(self.history) or 1e-9
        z = (spread_bps - mu) / sigma
        significant = z >= Z_SCORE_THRESHOLD
        return z, significant


def score_with_model(features: dict) -> float:
    """
    Koukku tulevaisuutta varten: kun arb_history.jsonl on kertynyt
    riittävästi (esim. >1000 riviä), tähän voi ladata koulutetun
    scikit-learn-mallin (esim. logistic regression) ennustamaan
    kaupan onnistumistodennäköisyyttä. Nyt palauttaa neutraalin 0.5.
    """
    return 0.5


def log_observation(record: dict):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def run():
    timer = SpreadTimer()
    amount_in_wei = w3.to_wei(TRADE_SIZE_WETH, "ether")

    log.info("Käynnistetään cross-DEX arb-monitori (Uniswap V3 vs QuickSwap)")
    log.info(f"Poll-väli: {POLL_INTERVAL_SECONDS}s, kauppakoko: {TRADE_SIZE_WETH} WETH")

    while True:
        try:
            uni = get_uniswap_quote(amount_in_wei)
            quick = get_quickswap_quote(amount_in_wei)

            # spread basis pointteina, suhteessa halvempaan hintaan
            higher, lower = max(uni.amount_out, quick.amount_out), min(
                uni.amount_out, quick.amount_out
            )
            spread_bps = ((higher - lower) / lower) * 10_000 if lower else 0

            z, significant = timer.update_and_score(spread_bps)

            buy_dex = "quickswap" if quick.amount_out < uni.amount_out else "uniswap_v3"
            sell_dex = "uniswap_v3" if buy_dex == "quickswap" else "quickswap"

            gross_profit_usd = (higher - lower) / 1e6  # USDC on 6 desimaalia
            flash_fee_usd = (lower / 1e6) * (FLASH_LOAN_FEE_BPS / 10_000)
            net_profit_usd = gross_profit_usd - flash_fee_usd - GAS_ESTIMATE_USD

            record = {
                "ts": int(time.time()),
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
                    f"spread={spread_bps:.1f}bps z={z:.2f} "
                    f"netto≈${net_profit_usd:.2f}"
                )
                # TODO: tähän flash loan -kontraktin kutsu (FlashLoanArb.sol)
                # esim. contract.functions.executeArb(...).transact({...})
            else:
                log.info(
                    f"spread={spread_bps:.1f}bps z={z:.2f} "
                    f"netto≈${net_profit_usd:.2f} — ei riitä (odotetaan)"
                )

        except Exception as e:
            log.error(f"Virhe kierroksella: {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
