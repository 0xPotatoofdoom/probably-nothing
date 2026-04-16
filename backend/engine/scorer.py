"""Weighted composite scorer."""

class Scorer:
    WEIGHTS = {
        "gas_efficiency":   0.40,
        "mev_resistance":   0.30,
        "liquidity_quality": 0.20,
        "code_simplicity":  0.10,
    }

    def score(self, metrics: dict) -> float:
        if metrics.get("compile_error"):
            return 0.0
        gas    = 1 - min(metrics["gas_used"] / 1_000_000, 1.0)
        mev    = 1 - min(metrics["mev_extracted"] / 10_000, 1.0)
        liq    = min(metrics["liquidity_depth"] / 100, 1.0)
        simple = 1 - min(metrics["complexity"] / 500, 1.0)
        return round(
            self.WEIGHTS["gas_efficiency"]   * gas +
            self.WEIGHTS["mev_resistance"]   * mev +
            self.WEIGHTS["liquidity_quality"] * liq +
            self.WEIGHTS["code_simplicity"]  * simple,
            4
        )
