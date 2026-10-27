import json
from pathlib import Path

import pytest

from research_project.example import audit_example, generate, read_samples, summarize


def test_seed_reproducibility_and_summary(tmp_path: Path) -> None:
    generate(42, 8, tmp_path / "samples.csv")
    generate(42, 8, tmp_path / "repeat.csv")
    assert (tmp_path / "samples.csv").read_bytes() == (
        tmp_path / "repeat.csv"
    ).read_bytes()
    summarize(tmp_path / "samples.csv", tmp_path / "summary.json")
    audit_example(tmp_path, seed=42, count=8)
    with pytest.raises(ValueError, match="seed and count"):
        audit_example(tmp_path, seed=41, count=8)


def test_summary_has_known_values(tmp_path: Path) -> None:
    source = tmp_path / "samples.csv"
    source.write_text("sample_id,value\n0,0.25\n1,0.75\n")
    summarize(source, tmp_path / "summary.json")
    assert json.loads((tmp_path / "summary.json").read_text()) == {
        "count": 2,
        "mean": 0.5,
        "minimum": 0.25,
        "maximum": 0.75,
    }


@pytest.mark.parametrize(
    "rows", ["0,nan\n", "0,inf\n", "0,1.5\n", "0,0.1\n0,0.2\n", "1,0.2\n", ""]
)
def test_invalid_samples_are_rejected(tmp_path: Path, rows: str) -> None:
    source = tmp_path / "samples.csv"
    source.write_text("sample_id,value\n" + rows)
    with pytest.raises(ValueError):
        read_samples(source)


def test_audit_rejects_corrupted_summary(tmp_path: Path) -> None:
    generate(42, 8, tmp_path / "samples.csv")
    summarize(tmp_path / "samples.csv", tmp_path / "summary.json")
    summary = json.loads((tmp_path / "summary.json").read_text())
    summary["mean"] = 9
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="mean differs"):
        audit_example(tmp_path, seed=42, count=8)


@pytest.mark.parametrize("count", [0, -1, 10001])
def test_generation_is_bounded(tmp_path: Path, count: int) -> None:
    with pytest.raises(ValueError, match="count"):
        generate(1, count, tmp_path / "samples.csv")


def test_parquet_matches_csv_and_corruption_is_rejected(tmp_path: Path) -> None:
    options = {"parquet_threads": 1, "parquet_memory_limit": "128MB"}
    generate(42, 8, tmp_path / "samples.csv", tmp_path / "samples.parquet", **options)
    summarize(tmp_path / "samples.csv", tmp_path / "summary.json")
    audit_example(tmp_path, seed=42, count=8, require_parquet=True, **options)
    (tmp_path / "samples.parquet").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Parquet"):
        audit_example(tmp_path, seed=42, count=8, require_parquet=True, **options)
