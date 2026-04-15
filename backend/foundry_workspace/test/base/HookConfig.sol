// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @dev Permission flag bitmask for the hook under test.
/// Rewritten per-run by the Probably Nothing harness based on the
/// hook's `getHookPermissions()` declaration. This lets HookMiner
/// pre-compute a valid CREATE2 salt without first deploying the hook.
library HookConfig {
    // beforeSwap (1<<7) | afterSwap (1<<6) — matches src/Hook.sol placeholder.
    // Rewritten per-run by the Probably Nothing harness based on the user's hook.
    uint160 internal constant FLAGS = (1 << 7) | (1 << 6);
}
