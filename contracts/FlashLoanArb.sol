// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// ----------------------------------------------------------------------
// Minimaaliset rajapinnat (ei ulkoisia riippuvuuksia - helpompi kaantaa
// solc-js:lla Termuxissa ilman node_modules-importtien resoluutiota)
// ----------------------------------------------------------------------

interface IPoolAddressesProvider {
    function getPool() external view returns (address);
}

interface IPool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title FlashLoanArb
/// @notice Aave V3 flashloan-vastaanottaja Polygonille. Tama versio pyytaa
///         lainan ja maksaa sen takaisin - EI VIELA sisalla arbitrage-swap-
///         logiikkaa (tulee omana lisayksena executeOperation-funktioon).
contract FlashLoanArb {
    address public immutable owner;
    IPoolAddressesProvider public immutable ADDRESSES_PROVIDER;
    IPool public immutable POOL;

    error NotOwner();
    error NotPool();
    error NotSelfInitiated();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @param addressesProvider Aave V3 PoolAddressesProvider-osoite (Polygon:
    ///        0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb)
    constructor(address addressesProvider) {
        owner = msg.sender;
        ADDRESSES_PROVIDER = IPoolAddressesProvider(addressesProvider);
        POOL = IPool(IPoolAddressesProvider(addressesProvider).getPool());
    }

    /// @notice Pyytaa flashloanin. Kutsuttavissa vain omistajan toimesta.
    /// @param asset Lainattava token (esim. WETH)
    /// @param amount Lainattava maara (raw, tokenin omalla desimaalitarkkuudella)
    function requestFlashLoan(address asset, uint256 amount) external onlyOwner {
        POOL.flashLoanSimple(
            address(this),
            asset,
            amount,
            "", // params - kayttoon kun arb-logiikka lisataan
            0   // referralCode - ei kaytossa
        );
    }

    /// @notice Aaven Poolin kutsuma callback flashloanin jalkeen.
    /// @dev Tassa versiossa EI tehda mitaan lainatulla summalla - vain
    ///      hyvaksytaan takaisinmaksu. Arb-swapit lisataan tahan seuraavaksi.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata /* params */
    ) external returns (bool) {
        if (msg.sender != address(POOL)) revert NotPool();
        if (initiator != address(this)) revert NotSelfInitiated();

        // TODO (seuraava commit): tanne kaksi swap-legia (osta halvemmalta
        // DEXilta, myy kalliimmalle) ennen takaisinmaksua.

        uint256 amountOwed = amount + premium;
        IERC20(asset).approve(address(POOL), amountOwed);

        return true;
    }

    /// @notice Hataposto: palauttaa kontraktiin jaaneet tokenit omistajalle.
    ///         Kontraktia EI PIDA kayttaa pysyvana varastona (griefing-riski).
    function rescueTokens(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        if (balance > 0) {
            IERC20(token).transfer(owner, balance);
        }
    }
}
