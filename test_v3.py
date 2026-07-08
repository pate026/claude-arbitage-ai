from web3 import Web3
import json

w3 = Web3(Web3.HTTPProvider('https://137.rpc.thirdweb.com/637c099dc97da50666475de7ae1022de'))

WETH = Web3.to_checksum_address("0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619")
USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
ZERO = "0x0000000000000000000000000000000000000000"

QUICK_ABI = json.loads('[{"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"address","name":"deployer","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint160","name":"limitSqrtPrice","type":"uint160"}],"internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}]')

quick_addr = Web3.to_checksum_address("0xa77aD9f635a3FB3bCCC5E6d1A87cB269746Aba17")
quick = w3.eth.contract(address=quick_addr, abi=QUICK_ABI)

try:
    result = quick.functions.quoteExactInputSingle({
        "tokenIn": WETH, "tokenOut": USDC, "deployer": ZERO,
        "amountIn": w3.to_wei(1, "ether"), "limitSqrtPrice": 0
    }).call()
    print("QUICKSWAP OK:", result)
except Exception as e:
    print("QUICKSWAP FAIL:", repr(e))
