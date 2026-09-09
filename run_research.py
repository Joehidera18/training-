"""Run chronological validation on your existing OHLCV CSV without a server."""
import argparse
import json
from pathlib import Path
from lab.continuous import DEFAULTS
from lab.data import load_history
from lab.research import research

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--symbol", default="BTC-USD")
    parser.add_argument("--interval", choices=["5m", "15m", "1h"], default="15m")
    parser.add_argument("--fee", type=float, default=.004, help="Fee fraction per side; .004 means 0.4%%")
    parser.add_argument("--slippage", type=float, default=.0005)
    parser.add_argument("--out", type=Path, default=Path("research-result.json"))
    args = parser.parse_args()
    settings = {**DEFAULTS, "decision_interval": args.interval, "fee_rate": args.fee, "slippage_rate": args.slippage}
    if not 0 <= args.fee <= .02 or not 0 <= args.slippage <= .01:
        parser.error("Fee or slippage is outside the supported range")
    result = research(load_history(args.csv), args.symbol, settings,
        progress=lambda stage, done, total, message: print(f"{done}/{total} {message}", flush=True))
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False))
    print("Historical gate:", "PASSED for further paper testing" if result["validated"] else "NOT PASSED")
    print(args.out.resolve())
