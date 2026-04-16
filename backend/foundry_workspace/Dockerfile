# probably-nothing-foundry
#
# Built on top of uniswapfoundation/v4-template so we inherit a known-good
# pinned dep tree (v4-core, v4-periphery, openzeppelin-uniswap-hooks, hookmate,
# permit2, solmate, forge-std). The Probably Nothing harness overlays its own
# Hook.sol placeholder and PNBase test scaffold, then warms forge's cache.
FROM ghcr.io/foundry-rs/foundry:latest
WORKDIR /workspace
USER root

# Clone v4-template + every transitive submodule. Slow, one-time.
RUN git clone --depth 1 --recurse-submodules --shallow-submodules \
        https://github.com/uniswapfoundation/v4-template . && \
    rm -rf src/Counter.sol test/Counter.t.sol script

# Overlay the PN scaffold.
COPY src/Hook.sol src/Hook.sol
COPY test/base test/base
COPY test/scenarios test/scenarios

# ── Compatibility shims ────────────────────────────────────────────────────────
# BaseHook moved from src/utils/ to src/base/hooks/ mid-2024.  Create a copy at
# the new path with the relative import corrected for the new directory depth, so
# repos built against either version of v4-periphery compile without modification.
RUN mkdir -p lib/uniswap-hooks/lib/v4-periphery/src/base/hooks && \
    sed 's|../base/ImmutableState.sol|../ImmutableState.sol|g' \
        lib/uniswap-hooks/lib/v4-periphery/src/utils/BaseHook.sol \
        > lib/uniswap-hooks/lib/v4-periphery/src/base/hooks/BaseHook.sol || true

# Warm the solc cache so the first runtime forge test is fast.
RUN forge build || true

ENTRYPOINT ["forge"]
CMD ["test", "--json"]
