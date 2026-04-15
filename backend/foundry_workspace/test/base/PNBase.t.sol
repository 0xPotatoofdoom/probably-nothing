// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {BaseTest} from "../utils/BaseTest.sol";
import {EasyPosm} from "../utils/libraries/EasyPosm.sol";

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IPositionManager} from "@uniswap/v4-periphery/src/interfaces/IPositionManager.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency, CurrencyLibrary} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidityAmounts} from "@uniswap/v4-core/test/utils/LiquidityAmounts.sol";
import {Constants} from "@uniswap/v4-core/test/utils/Constants.sol";

import {Hook} from "../../src/Hook.sol";
import {HookConfig} from "./HookConfig.sol";

/// @title PNBase
/// @notice Shared test base for every Probably Nothing scenario. Inherits
/// v4-template's BaseTest (deployArtifacts + Deployers) and exposes a clean
/// swap/LP API so LLM-authored scenarios only need to encode intent.
abstract contract PNBase is BaseTest {
    using EasyPosm for IPositionManager;
    using PoolIdLibrary for PoolKey;
    using CurrencyLibrary for Currency;

    Hook internal hook;
    Currency internal currency0;
    Currency internal currency1;
    PoolKey internal poolKey;
    PoolId internal poolId;

    uint24 internal constant FEE = 3000;
    int24 internal constant TICK_SPACING = 60;
    uint128 internal constant SEED_LIQUIDITY = 100e18;

    int24 internal tickLower;
    int24 internal tickUpper;
    uint256 internal seedTokenId;

    function setUp() public virtual {
        deployArtifactsAndLabel();
        (currency0, currency1) = deployCurrencyPair();

        // Mine an address whose low bits encode the hook permission flags.
        // The 0x4444 namespace prefix mirrors v4-template's convention so we
        // don't collide with other hooks deployed in the same test run.
        address flags = address(uint160(HookConfig.FLAGS) ^ (0x4444 << 144));
        bytes memory ctorArgs = abi.encode(poolManager);
        deployCodeTo("Hook.sol:Hook", ctorArgs, flags);
        hook = Hook(flags);

        poolKey = PoolKey(currency0, currency1, FEE, TICK_SPACING, IHooks(address(hook)));
        poolId = poolKey.toId();
        poolManager.initialize(poolKey, Constants.SQRT_PRICE_1_1);

        tickLower = TickMath.minUsableTick(TICK_SPACING);
        tickUpper = TickMath.maxUsableTick(TICK_SPACING);

        // Seed full-range liquidity so swap scenarios have something to trade against.
        (uint256 amount0, uint256 amount1) = LiquidityAmounts.getAmountsForLiquidity(
            Constants.SQRT_PRICE_1_1,
            TickMath.getSqrtPriceAtTick(tickLower),
            TickMath.getSqrtPriceAtTick(tickUpper),
            SEED_LIQUIDITY
        );
        (seedTokenId,) = positionManager.mint(
            poolKey,
            tickLower,
            tickUpper,
            SEED_LIQUIDITY,
            amount0 + 1,
            amount1 + 1,
            address(this),
            block.timestamp,
            ""
        );
    }

    // ─── swap helpers ──────────────────────────────────────────────────────

    function doSwap(int256 amountSpecified, bool zeroForOne) internal returns (BalanceDelta delta) {
        uint128 amountIn = uint128(amountSpecified < 0 ? uint256(-amountSpecified) : uint256(amountSpecified));
        delta = swapRouter.swapExactTokensForTokens({
            amountIn: amountIn,
            amountOutMin: 0,
            zeroForOne: zeroForOne,
            poolKey: poolKey,
            hookData: "",
            receiver: address(this),
            deadline: block.timestamp + 1
        });
    }

    function doSwapWithHookData(int256 amountSpecified, bool zeroForOne, bytes memory hookData)
        internal returns (BalanceDelta delta)
    {
        uint128 amountIn = uint128(amountSpecified < 0 ? uint256(-amountSpecified) : uint256(amountSpecified));
        delta = swapRouter.swapExactTokensForTokens({
            amountIn: amountIn,
            amountOutMin: 0,
            zeroForOne: zeroForOne,
            poolKey: poolKey,
            hookData: hookData,
            receiver: address(this),
            deadline: block.timestamp + 1
        });
    }

    // ─── liquidity helpers ─────────────────────────────────────────────────

    function doAddLiquidity(int24 lower, int24 upper, uint128 liquidity) internal returns (uint256 tokenId) {
        (uint256 amount0, uint256 amount1) = LiquidityAmounts.getAmountsForLiquidity(
            Constants.SQRT_PRICE_1_1,
            TickMath.getSqrtPriceAtTick(lower),
            TickMath.getSqrtPriceAtTick(upper),
            liquidity
        );
        (tokenId,) = positionManager.mint(
            poolKey, lower, upper, liquidity,
            amount0 + 1, amount1 + 1,
            address(this), block.timestamp, ""
        );
    }

    function doRemoveLiquidity(uint256 tokenId, uint128 liquidity) internal {
        positionManager.decreaseLiquidity(
            tokenId, liquidity, 0, 0, address(this), block.timestamp, ""
        );
    }

    // ─── MEV scenario helper ───────────────────────────────────────────────

    function sandwich(int256 victimAmount, bool zeroForOne, int256 attackerAmount) internal {
        doSwap(attackerAmount, zeroForOne);
        doSwap(victimAmount, zeroForOne);
        doSwap(-attackerAmount, !zeroForOne);
    }
}
