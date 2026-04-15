// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {PNBase} from "../base/PNBase.t.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";

/// @notice Hand-written baseline scenarios covering the routing paths every
/// hook must handle. LLM-authored scenarios layer on top via the same PNBase.
contract BaselineScenarios is PNBase {
    function test_SingleSwap_ExactInput_0For1() public {
        BalanceDelta delta = doSwap(-1 ether, true);
        assertLt(int256(delta.amount0()), 0);
        assertGt(int256(delta.amount1()), 0);
    }

    function test_SingleSwap_ExactInput_1For0() public {
        BalanceDelta delta = doSwap(-1 ether, false);
        assertLt(int256(delta.amount1()), 0);
        assertGt(int256(delta.amount0()), 0);
    }

    function test_SmallSwap() public {
        doSwap(-0.001 ether, true);
    }

    function test_AddLiquidity_InRange() public {
        doAddLiquidity(-60, 60, 1 ether);
    }

    function test_AddRemoveLiquidity_RoundTrip() public {
        uint256 tokenId = doAddLiquidity(-120, 120, 5 ether);
        doRemoveLiquidity(tokenId, 5 ether);
    }

    function test_Sandwich_BasicSurvival() public {
        sandwich(-1 ether, true, -0.5 ether);
    }
}
