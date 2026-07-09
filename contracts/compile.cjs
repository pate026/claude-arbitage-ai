const solc = require('solc');
const fs = require('fs');

const source = fs.readFileSync('FlashLoanArb.sol', 'utf8');

const input = {
  language: 'Solidity',
  sources: {
    'FlashLoanArb.sol': { content: source },
  },
  settings: {
    outputSelection: {
      '*': { '*': ['abi', 'evm.bytecode.object'] },
    },
    optimizer: { enabled: true, runs: 200 },
    viaIR: true,
  },
};

const output = JSON.parse(solc.compile(JSON.stringify(input)));

if (output.errors) {
  let hasError = false;
  for (const err of output.errors) {
    console.log(err.severity.toUpperCase() + ':', err.formattedMessage);
    if (err.severity === 'error') hasError = true;
  }
  if (hasError) process.exit(1);
}

const contract = output.contracts['FlashLoanArb.sol']['FlashLoanArb'];
fs.writeFileSync('FlashLoanArb.abi.json', JSON.stringify(contract.abi, null, 2));
fs.writeFileSync('FlashLoanArb.bin', contract.evm.bytecode.object);

console.log('OK: kaannos onnistui');
console.log('ABI tallennettu: FlashLoanArb.abi.json');
console.log('Bytecode tallennettu: FlashLoanArb.bin');
