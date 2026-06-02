from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold HYPE walk-forward research plan")
    parser.add_argument("--config", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--step-days", type=int, default=3)
    args = parser.parse_args(argv)
    print("WALK_FORWARD_SCAFFOLD_READY")
    print("config=" + args.config)
    print("train_days=" + str(args.train_days))
    print("test_days=" + str(args.test_days))
    print("step_days=" + str(args.step_days))
    print("next_step=wire parameter sweep over rolling train windows and evaluate fixed params on test windows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
