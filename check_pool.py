from web3 import Web3
import json

w3 = Web3(Web3.HTTPProvider('https://137.rpc.thirdweb.com/637c099dc97da50666475de7ae1022de'))

WETH = Web3.to_checksum_address("0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619")
USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

FACTORY_ABI = json.loads('[{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"}],"name":"poolByPair","outputs":[{"internalType":"address","name":"pool","type":"address"}],"stateMutability":"view","type":"function"}]')

factory_addr = Web3.to_checksum_address("0x411b0fAcC3489691f28ad58c47006AF5E3Ab3A28")
factory = w3.eth.contract(address=factory_addr, abi=FACTORY_ABI)

pool = factory.functions.poolByPair(WETH, USDC).call()
print("POOL ADDRESS:", pool)
if int(pool, 16) == 0:
    print("--> Poolia ei ole olemassa!")
else:
    code = w3.eth.get_code(pool)
    print("Poolin koodin pituus:", len(code))
