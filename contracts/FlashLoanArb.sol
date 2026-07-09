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

/// @dev Uniswap V3 -tyylinen router (kiintea fee-tier)
interface IUniswapV3Router {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external returns (uint256 amountOut);
}

/// @dev Algebra-tyylinen router (QuickSwap - dynaaminen fee, ei fee-parametria)
interface IAlgebraRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 limitSqrtPrice;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external returns (uint256 amountOut);
}

/// @title FlashLoanArb
/// @notice Aave V3 flashloan-vastaanottaja Polygonille. Pyytaa lainan,
///         tekee kaksi swap-legia (osta halvemmalta DEXilta WETH/USDC-
///         parissa, myy kalliimmalle) ja maksaa lainan + preemion takaisin
///         jos kauppa oli kannattava (muuten koko transaktio revertoi).
contract FlashLoanArb {
    address public immutable owner;
    IPoolAddressesProvider public immutable ADDRESSES_PROVIDER;
    IPool public immutable POOL;

    // Polygon-mainnet reitittimet ja USDC - tarkistettu Uniswap/QuickSwap
    // dokumentaatiosta. Katso projektin ohje: tarkista ajantasaiset osoitteet
    // ennen kayttoa jos naita muutetaan.
    address public constant UNISWAP_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address public constant QUICKSWAP_ROUTER = 0x3012E9049d05B4B5369D690114D5A5861EbB85cb;
    address public constant USDC = 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174;
    uint24 public constant UNISWAP_FEE_TIER = 500; // 0.05%

    error NotOwner();
    error NotPool();
    error NotSelfInitiated();
    error InsufficientProfit();

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

    /// @notice Pyytaa flashloanin ja suorittaa arb-swapit executeOperation:ssa.
    /// @param asset Lainattava token (esim. WETH)
    /// @param amount Lainattava maara (raw, tokenin omalla desimaalitarkkuudella)
    /// @param buyOnUniswapFirst true = osta Uniswapista ensin, myy QuickSwapiin
    /// @param minUsdcOut Minimi USDC-maara ensimmaisesta swapista (slippage-suoja)
    /// @param minWethOut Minimi WETH-maara toisesta swapista (slippage-suoja,
    ///        tayttaa myos lainan+preemion takaisinmaksun jos > amount+premium)
    function requestFlashLoan(
        address asset,
        uint256 amount,
        bool buyOnUniswapFirst,
        uint256 minUsdcOut,
        uint256 minWethOut
    ) external onlyOwner {
        bytes memory params = abi.encode(buyOnUniswapFirst, minUsdcOut, minWethOut);
        POOL.flashLoanSimple(
            address(this),
            asset,
            amount,
            params,
            0 // referralCode - ei kaytossa
        );
    }

    /// @notice Aaven Poolin kutsuma callback flashloanin jalkeen. Tekee kaksi
    ///         swap-legia (osta halvemmalta DEXilta, myy kalliimmalle) ja
    ///         hyvaksyy takaisinmaksun jos kauppa oli kannattava.
    /// @param params abi.encode(bool buyOnUniswapFirst, uint256 minUsdcOut, uint256 minWethOut)
    ///        buyOnUniswapFirst: true = osta WETH->USDC Uniswapista, myy USDC->WETH QuickSwapiin
    ///                           false = paivastoin
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (msg.sender != address(POOL)) revert NotPool();
        if (initiator != address(this)) revert NotSelfInitiated();

        (bool buyOnUniswapFirst, uint256 minUsdcOut, uint256 minWethOut) =
            abi.decode(params, (bool, uint256, uint256));

        address buyRouter = buyOnUniswapFirst ? UNISWAP_ROUTER : QUICKSWAP_ROUTER;
        address sellRouter = buyOnUniswapFirst ? QUICKSWAP_ROUTER : UNISWAP_ROUTER;

        // --- Leg 1: WETH -> USDC halvemmalla DEXilla ---
        IERC20(asset).approve(buyRouter, amount);
        uint256 usdcReceived = _swapExactIn(
            buyRouter,
            asset,
            USDC,
            amount,
            minUsdcOut
        );

        // --- Leg 2: USDC -> WETH kalliimmalla DEXilla ---
        IERC20(USDC).approve(sellRouter, usdcReceived);
        uint256 wethReceived = _swapExactIn(
            sellRouter,
            USDC,
            asset,
            usdcReceived,
            minWethOut
        );

        uint256 amountOwed = amount + premium;
        if (wethReceived < amountOwed) revert InsufficientProfit();

        IERC20(asset).approve(address(POOL), amountOwed);

        return true;
    }

    /// @dev Yksi swap-leg. Valitsee oikean rajapinnan reitittimen osoitteen
    ///      perusteella (Uniswap V3 vs Algebra/QuickSwap - eri parametrit).
    function _swapExactIn(
        address router,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 amountOutMinimum
    ) internal returns (uint256) {
        if (router == UNISWAP_ROUTER) {
            return IUniswapV3Router(router).exactInputSingle(
                IUniswapV3Router.ExactInputSingleParams({
                    tokenIn: tokenIn,
                    tokenOut: tokenOut,
                    fee: UNISWAP_FEE_TIER,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: amountIn,
                    amountOutMinimum: amountOutMinimum,
                    sqrtPriceLimitX96: 0
                })
            );
        } else {
            return IAlgebraRouter(router).exactInputSingle(
                IAlgebraRouter.ExactInputSingleParams({
                    tokenIn: tokenIn,
                    tokenOut: tokenOut,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: amountIn,
                    amountOutMinimum: amountOutMinimum,
                    limitSqrtPrice: 0
                })
            );
        }
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
