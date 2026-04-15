"""Parametric → structural → LLM-assisted mutation engine."""
import re, random, copy
from typing import List, Dict, Any

class HookMutator:
    def extract_params(self, source: str) -> Dict[str, Any]:
        """Extract mutable parameters via regex (fee tiers, uint constants, booleans)."""
        params = {}
        # Fee tiers: 100, 500, 3000, 10000
        fee_matches = re.findall(r'\b(100|500|3000|10000)\b', source)
        if fee_matches:
            params["fee_tier"] = int(fee_matches[0])
        # Uint constants
        uint_matches = re.finditer(r'uint\d*\s+(?:constant\s+)?(\w+)\s*=\s*(\d+)', source)
        for m in uint_matches:
            params[m.group(1)] = int(m.group(2))
        # Booleans
        bool_matches = re.finditer(r'bool\s+(?:public\s+)?(\w+)\s*=\s*(true|false)', source)
        for m in bool_matches:
            params[m.group(1)] = m.group(2) == "true"
        return params

    def parametric_variants(self, source: str, params: Dict, count: int = 6) -> List[str]:
        """Generate N variants by perturbing extracted params."""
        variants = [source]  # always include original
        fee_tiers = [100, 500, 3000, 10000]
        for _ in range(count - 1):
            variant = source
            for key, val in params.items():
                if key == "fee_tier":
                    new_fee = random.choice([f for f in fee_tiers if f != val])
                    variant = re.sub(r'\b' + str(val) + r'\b', str(new_fee), variant, count=1)
                elif isinstance(val, int):
                    delta = random.randint(-int(val * 0.2) or -10, int(val * 0.2) or 10)
                    new_val = max(0, val + delta)
                    variant = variant.replace(f"= {val}", f"= {new_val}", 1)
                elif isinstance(val, bool):
                    variant = variant.replace(
                        f"= {'true' if val else 'false'}",
                        f"= {'false' if val else 'true'}", 1
                    )
            variants.append(variant)
        return variants[:count]
