from web3 import Web3
import json

w3 = Web3(Web3.HTTPProvider('https://137.rpc.thirdweb.com/637c099dc97da50666475de7ae1022de'))

pool_addr = Web3.to_checksum_address("0x55CAaBB0d2b704FD0eF8192A7E35D8837e678207")

POOL_ABI = json.loads('[{"inputs":[],"name":"globalState","outputs":[{"internalType":"uint160","name":"price","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"fee","type":"uint16"},{"internalType":"uint16","name":"timepointIndex","type":"uint16"},{"internalType":"uint8","name":"communityFeeToken0","type":"uint8"},{"internalType":"uint8","name":"communityFeeToken1","type":"uint8"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"liquidity","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],"stateMutability":"view","type":"function"}]')

pool = w3.eth.contract(address=pool_addr, abi=POOL_ABI)

try:
    state = pool.functions.globalState().call()
    print("globalState:", state)
except Exception as e:
    print("globalState FAIL:", repr(e))

try:
    liq = pool.functions.liquidity().call()
    print("liquidity:", liq)
except Exception as e:
    print("liquidity FAIL:", repr(e))
