"""A seeded toy workflow used to exercise execution and evidence handling."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import statistics
from pathlib import Path

import duckdb


def parquet_options(threads: int | None, memory_limit: str | None) -> dict:
    if (
        type(threads) is not int
        or threads < 1
        or not isinstance(memory_limit, str)
        or not re.fullmatch(r"[1-9][0-9]*(KB|MB|GB)", memory_limit)
    ):
        raise ValueError(
            "Parquet operations require configured threads and memory_limit"
        )
    return {"threads": threads, "memory_limit": memory_limit}


def generate(
    seed: int,
    count: int,
    output: Path,
    parquet_output: Path | None = None,
    *,
    parquet_threads: int | None = None,
    parquet_memory_limit: str | None = None,
) -> None:
    if not 1 <= count <= 10000:
        raise ValueError("count must be between 1 and 10000")
    generator = random.Random(seed)
    with output.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_id", "value"])
        writer.writerows((index, repr(generator.random())) for index in range(count))
    if parquet_output is not None:
        # Both formats contain the same observations and an explicit Parquet schema.
        with duckdb.connect(
            config=parquet_options(parquet_threads, parquet_memory_limit)
        ) as connection:
            connection.execute("CREATE TABLE samples (sample_id BIGINT, value DOUBLE)")
            values = read_samples(output)
            connection.executemany(
                "INSERT INTO samples VALUES (?, ?)", list(enumerate(values))
            )
            connection.execute(
                "COPY (SELECT * FROM samples ORDER BY sample_id) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                [str(parquet_output)],
            )


def read_samples(path: Path) -> list[float]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["sample_id", "value"]:
            raise ValueError("unexpected sample columns")
        rows = list(reader)
    if not rows or [int(row["sample_id"]) for row in rows] != list(range(len(rows))):
        raise ValueError("sample IDs must be contiguous and unique")
    values = [float(row["value"]) for row in rows]
    if not all(math.isfinite(value) and 0 <= value < 1 for value in values):
        raise ValueError("sample values must be finite and in [0, 1)")
    return values


def summarize(source: Path, output: Path) -> None:
    values = read_samples(source)
    summary = {
        "count": len(values),
        "mean": statistics.mean(values),
        "minimum": min(values),
        "maximum": max(values),
    }
    output.write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")


def audit_example(
    directory: Path,
    *,
    seed: int,
    count: int,
    require_parquet: bool = False,
    parquet_threads: int | None = None,
    parquet_memory_limit: str | None = None,
) -> None:
    values = read_samples(directory / "samples.csv")
    generator = random.Random(seed)
    if values != [generator.random() for _ in range(count)]:
        raise ValueError("samples do not match the recorded seed and count")
    if require_parquet:
        try:
            with duckdb.connect(
                config=parquet_options(parquet_threads, parquet_memory_limit)
            ) as connection:
                schema = connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)",
                    [str(directory / "samples.parquet")],
                ).fetchall()
                if [(row[0], row[1]) for row in schema] != [
                    ("sample_id", "BIGINT"),
                    ("value", "DOUBLE"),
                ]:
                    raise ValueError("unexpected Parquet sample schema")
                rows = connection.execute(
                    "SELECT sample_id, value FROM read_parquet(?) ORDER BY sample_id",
                    [str(directory / "samples.parquet")],
                ).fetchall()
                if rows != list(enumerate(values)):
                    raise ValueError(
                        "Parquet samples differ from the seeded CSV observations"
                    )
        except duckdb.Error as error:
            raise ValueError(f"Cannot verify Parquet samples: {error}") from error
    summary = json.loads((directory / "summary.json").read_text())
    expected = {
        "count": count,
        "mean": math.fsum(values) / count,
        "minimum": min(values),
        "maximum": max(values),
    }
    if summary.keys() != expected.keys():
        raise ValueError("unexpected summary fields")
    if type(summary["count"]) is not int or summary["count"] != count:
        raise ValueError("summary count differs from the samples")
    for key in ("mean", "minimum", "maximum"):
        observed = summary[key]
        if type(observed) not in (int, float) or not math.isfinite(observed):
            raise ValueError(f"summary {key} is not finite")
        if not math.isclose(observed, expected[key], rel_tol=1e-12, abs_tol=1e-15):
            raise ValueError(f"summary {key} differs from the samples")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    generation = commands.add_parser("generate")
    generation.add_argument("--seed", type=int, required=True)
    generation.add_argument("--count", type=int, required=True)
    generation.add_argument("--output", type=Path, required=True)
    generation.add_argument("--parquet-output", type=Path)
    generation.add_argument("--parquet-threads", type=int)
    generation.add_argument("--parquet-memory-limit")
    summary = commands.add_parser("summarize")
    summary.add_argument("--input", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "generate":
        generate(
            args.seed,
            args.count,
            args.output,
            args.parquet_output,
            parquet_threads=args.parquet_threads,
            parquet_memory_limit=args.parquet_memory_limit,
        )
    else:
        summarize(args.input, args.output)


if __name__ == "__main__":
    main()
