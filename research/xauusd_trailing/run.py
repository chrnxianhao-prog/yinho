from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from .data import load_mt5_csv
from .engine import run_backtest
from .models import BacktestConfig
from .walk_forward import run_walk_forward


def _json_default(value: Any) -> str:
    return str(value)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strategy_code_hash() -> str:
    package_dir = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for source in sorted(package_dir.glob("*.py")):
        digest.update(source.name.encode("utf-8"))
        digest.update(bytes.fromhex(_file_hash(source)))
    return digest.hexdigest()


def _write_result(result: Any, output_dir: Path, prefix: str = "") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{prefix}_" if prefix else ""
    result.trades.to_csv(output_dir / f"{stem}trades.csv", index=False)
    result.events.to_csv(output_dir / f"{stem}events.csv", index=False)
    result.equity.to_csv(output_dir / f"{stem}equity.csv", index=False)
    result.open_positions.to_csv(output_dir / f"{stem}open_positions.csv", index=False)
    with (output_dir / f"{stem}summary.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {"metrics": result.metrics, "audit": result.audit},
            handle,
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest-only XAUUSD trailing-stop strategy")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("research/xauusd_trailing/config.example.yaml"),
    )
    args = parser.parse_args()
    import yaml

    config_path = args.config.resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    data_config = raw.pop("data", {})
    output_config = raw.pop("output", {})
    do_walk_forward = bool(raw.pop("run_walk_forward", True))
    allowed = {item.name for item in fields(BacktestConfig)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown strategy config keys: {', '.join(unknown)}")
    config = BacktestConfig(**raw)

    source_tz = config.source_timezone
    data_paths = {
        name: Path(data_config[f"{name.lower()}_csv"]).resolve()
        for name in ("M1", "M5", "H1")
    }
    m1, m5, h1 = (load_mt5_csv(data_paths[name], source_tz) for name in ("M1", "M5", "H1"))
    result = run_backtest(m1, m5, h1, config)
    output_dir = Path(output_config.get("directory", "artifacts/xauusd_backtest")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_result(result, output_dir)

    provenance = {
        "strategy": config.symbol,
        "config_sha256": _file_hash(config_path),
        "input_sha256": {name: _file_hash(path) for name, path in data_paths.items()},
        "strategy_code_sha256": _strategy_code_hash(),
        "strategy_version": "xauusd-trailing-v1",
        "paper_or_backtest_only": True,
        "live_order_code": False,
    }
    (output_dir / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if do_walk_forward:
        reports = run_walk_forward(m1, m5, h1, config)
        rows: list[dict[str, object]] = []
        for name, report in reports.items():
            rows.append({"split": name, **report["metrics"]})
            report_result = type("SplitResult", (), report)
            _write_result(report_result, output_dir / "walk_forward", name)
        import pandas as pd

        pd.DataFrame(rows).to_csv(output_dir / "walk_forward_metrics.csv", index=False)

    print(json.dumps(result.metrics, ensure_ascii=False, indent=2, default=_json_default))
    print(f"Wrote audit outputs to: {output_dir}")


if __name__ == "__main__":
    main()
