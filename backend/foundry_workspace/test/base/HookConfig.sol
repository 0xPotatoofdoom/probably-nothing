// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @dev Permission flag bitmask and constructor-args factory for the hook under test.
/// Both are rewritten per-run by the Probably Nothing harness.
library HookConfig {
    // Rewritten per-run based on the user's hook's getHookPermissions().
    uint160 internal constant FLAGS = (1 << 7) | (1 << 6);

    // CTOR_PATTERN controls how PNBase assembles constructor calldata:
    //   0 = (IPoolManager)                         — standard v4-template hooks
    //   1 = (IPoolManager, address owner)          — hooks with an owner param
    //   2 = (IPoolManager, address owner, uint24)  — hooks with owner + fee param
    uint8 internal constant CTOR_PATTERN = 0;

    function ctorArgs(address poolManager) internal pure returns (bytes memory) {
        if (CTOR_PATTERN == 1) return abi.encode(poolManager, address(0));
        if (CTOR_PATTERN == 2) return abi.encode(poolManager, address(0), uint24(3000));
        return abi.encode(poolManager);
    }
}
