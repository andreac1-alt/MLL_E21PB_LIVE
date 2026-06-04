from __future__ import annotations

from pathlib import Path

import pandas as pd
from core.config.output_paths import resolve_output_root


BASE_DIR = Path(__file__).resolve().parent
SCREENING_DAY_DIR = resolve_output_root() / "screening_day"
TRADING_DAY_DIR = resolve_output_root() / "trading_day"


def archive_year_dir(target_date: pd.Timestamp) -> Path:
    return SCREENING_DAY_DIR / target_date.strftime("%Y")


def archive_month_dir(target_date: pd.Timestamp) -> Path:
    return archive_year_dir(target_date) / target_date.strftime("%m")


def archive_day_dir(target_date: pd.Timestamp) -> Path:
    return archive_month_dir(target_date) / target_date.strftime("%Y%m%d")


def flat_archive_day_dir(target_date: pd.Timestamp) -> Path:
    return archive_year_dir(target_date) / target_date.strftime("%Y%m%d")


def legacy_archive_day_dir(target_date: pd.Timestamp) -> Path:
    return SCREENING_DAY_DIR / target_date.strftime("%Y%m%d")


def trading_day_year_dir(target_date: pd.Timestamp) -> Path:
    return TRADING_DAY_DIR / target_date.strftime("%Y")


def trading_day_month_dir(target_date: pd.Timestamp) -> Path:
    return trading_day_year_dir(target_date) / target_date.strftime("%m")


def trading_day_date_dir(target_date: pd.Timestamp) -> Path:
    return trading_day_month_dir(target_date) / target_date.strftime("%Y%m%d")


def archive_file_path(target_date: pd.Timestamp, filename: str) -> Path:
    return archive_day_dir(target_date) / filename


def resolve_archive_file(target_date: pd.Timestamp, filename: str) -> Path:
    candidates = [
        archive_file_path(target_date, filename),
        flat_archive_day_dir(target_date) / filename,
        legacy_archive_day_dir(target_date) / filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    day_dir = archive_day_dir(target_date)
    if day_dir.exists():
        timestamped_matches = sorted(
            day_dir.glob(f"*/{filename}"),
            key=lambda path: path.parent.name,
            reverse=True,
        )
        if timestamped_matches:
            return timestamped_matches[0]
    return candidates[0]
