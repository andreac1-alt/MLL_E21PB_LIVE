from __future__ import annotations

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from core.config.data_paths import MARKET_DATA_ROOT
from core.market.semaforo import (
    compute_blue_on_for_date,
    compute_market_street_light_for_date,
    load_daily_history_from_cache,
)
from core.portfolio.momentum_signal import load_momentum_signal
from core.portfolio.etf_context import save_etf_context_for_sd
from core.trade_console.mobile_service import build_trade_console_payload
from core.screening.second_screen import SecondScreenConfig
from tools.strategy_EMA21_SMA50 import calculate_quantity


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
SCREENING_DAY_DIR = OUTPUT_DIR / "screening_day"
PORTFOLIO_DIR = (
    OUTPUT_DIR
    / "portfolio_live"
    / "full"
    / "MLL_PB"
    / "base"
)
POSITIONS_PATH = PORTFOLIO_DIR / "portfolio_positions_daily.csv"
ACTIONS_PATH = PORTFOLIO_DIR / "portfolio_actions_daily.csv"
STATE_PATH = PORTFOLIO_DIR / "portfolio_state_daily.csv"
MOMENTUM_PATH = PORTFOLIO_DIR / "market" / "momentum_inv5d_signal.csv"
TRADE_STATE_ROOT = OUTPUT_DIR / "trade_state"
ETF_CONTEXT_ROOT = OUTPUT_DIR / "etf_context"
BREADTH_HISTORY_PATH = OUTPUT_DIR / "breadth" / "history" / "universe_breadth_daily.csv"
OFFICIAL_SIZING_SLOPE_THRESHOLD = 0.45
OFFICIAL_SIZING_SLOPE_ADD = 0.25
OFFICIAL_SIZING_MOMENTUM_ADD = 0.25
OFFICIAL_SIZING_SMA20_P20_ADD = 0.25
OFFICIAL_SIZING_SMA20_P10_ADD = 0.50
ETF_SCORE_PASS_MULTIPLIER = 1.25
ETF_SCORE_FAIL_MULTIPLIER = 0.50
DEFAULT_STRATEGY_ID = "MLL_PB"
DEFAULT_VARIANT_ID = "base"


def configure_page() -> None:
    st.set_page_config(page_title="MLL1 Live", page_icon="ML", layout="wide")


@st.cache_data(show_spinner=False)
def read_csv(path: str) -> pd.DataFrame:
    csv_path = Path(path)
    if not csv_path.exists():
        return pd.DataFrame()
    return pd.read_csv(csv_path, keep_default_na=False)


def load_live_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    state = read_csv(str(STATE_PATH))
    positions = read_csv(str(POSITIONS_PATH))
    actions = read_csv(str(ACTIONS_PATH))
    momentum = read_csv(str(MOMENTUM_PATH))
    return state, positions, actions, momentum


def format_yes_no(value: bool | None) -> str | None:
    if value is None:
        return None
    return "YES" if bool(value) else "NO"


def metric_delta_color(value: bool | None) -> str:
    if value is None:
        return "off"
    return "normal" if bool(value) else "inverse"


def get_semaphore_style(color: str) -> tuple[str, str, str]:
    normalized = str(color).strip().upper()
    palette = {
        "GREEN": ("rgba(34, 197, 94, 0.18)", "rgba(34, 197, 94, 0.42)", "#166534"),
        "BLUE": ("rgba(37, 99, 235, 0.16)", "rgba(37, 99, 235, 0.38)", "#1d4ed8"),
        "YELLOW": ("rgba(245, 158, 11, 0.18)", "rgba(245, 158, 11, 0.38)", "#b45309"),
        "RED": ("rgba(239, 68, 68, 0.18)", "rgba(239, 68, 68, 0.38)", "#b91c1c"),
    }
    return palette.get(normalized, ("rgba(148, 163, 184, 0.16)", "rgba(148, 163, 184, 0.38)", "#475569"))


def to_date_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def latest_date(df: pd.DataFrame, column: str) -> pd.Timestamp | None:
    if df.empty or column not in df.columns:
        return None
    dates = to_date_series(df[column]).dropna()
    if dates.empty:
        return None
    return pd.Timestamp(dates.max()).normalize()


def previous_market_session(before_date: pd.Timestamp) -> pd.Timestamp | None:
    import pandas_market_calendars as mcal

    normalized = pd.Timestamp(before_date).normalize()
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=normalized - pd.Timedelta(days=14),
        end_date=normalized - pd.Timedelta(days=1),
    )
    sessions = [pd.Timestamp(session_date).normalize() for session_date in schedule.index]
    return sessions[-1] if sessions else None


def next_market_session(after_date: pd.Timestamp) -> pd.Timestamp | None:
    import pandas_market_calendars as mcal

    normalized = pd.Timestamp(after_date).normalize()
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=normalized + pd.Timedelta(days=1),
        end_date=normalized + pd.Timedelta(days=14),
    )
    sessions = [pd.Timestamp(session_date).normalize() for session_date in schedule.index]
    return sessions[0] if sessions else None


def screening_day_dir(screen_date: pd.Timestamp) -> Path:
    normalized = pd.Timestamp(screen_date).normalize()
    return (
        SCREENING_DAY_DIR
        / normalized.strftime("%Y")
        / normalized.strftime("%m")
        / normalized.strftime("%Y%m%d")
    )


def resolve_screening_artifact(screen_date: pd.Timestamp, filename: str) -> Path | None:
    day_dir = screening_day_dir(screen_date)
    candidates: list[Path] = []
    direct_path = day_dir / filename
    if direct_path.exists():
        candidates.append(direct_path)

    candidates.extend(day_dir.glob(f"*/{filename}"))
    if not candidates:
        return None

    return max(candidates, key=lambda path: (path.stat().st_mtime, str(path)))


def load_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def available_screen_dates() -> list[pd.Timestamp]:
    dates: list[pd.Timestamp] = []
    for year_dir in sorted(SCREENING_DAY_DIR.glob("20[0-9][0-9]")):
        for month_dir in sorted(year_dir.glob("[0-1][0-9]")):
            for day_dir in sorted(month_dir.glob("[0-9]" * 8)):
                if day_dir.is_dir() and day_dir.name.isdigit():
                    parsed = pd.to_datetime(day_dir.name, format="%Y%m%d", errors="coerce")
                    if pd.notna(parsed):
                        dates.append(pd.Timestamp(parsed).normalize())
    return dates


def latest_available_screen_date() -> pd.Timestamp | None:
    dates = available_screen_dates()
    return dates[-1] if dates else None


def load_screening_day_bundle(screen_date: pd.Timestamp) -> dict[str, object]:
    normalized = pd.Timestamp(screen_date).normalize()
    stamp = normalized.strftime("%Y%m%d")
    first_all_path = resolve_screening_artifact(normalized, f"first_screen_all_{stamp}.csv")
    first_passed_path = resolve_screening_artifact(normalized, f"first_screen_passed_{stamp}.csv")
    first_summary_path = resolve_screening_artifact(normalized, f"first_screen_summary_{stamp}.txt")
    second_all_path = resolve_screening_artifact(normalized, f"second_screen_all_{stamp}.csv")
    second_passed_path = resolve_screening_artifact(normalized, f"second_screen_passed_{stamp}.csv")
    second_symbols_path = resolve_screening_artifact(normalized, f"second_screen_symbols_{stamp}.txt")

    return {
        "screen_date": normalized,
        "first_all_path": first_all_path,
        "first_passed_path": first_passed_path,
        "first_summary_path": first_summary_path,
        "second_all_path": second_all_path,
        "second_passed_path": second_passed_path,
        "second_symbols_path": second_symbols_path,
        "first_all_df": read_csv(str(first_all_path)) if first_all_path else pd.DataFrame(),
        "first_passed_df": read_csv(str(first_passed_path)) if first_passed_path else pd.DataFrame(),
        "first_summary_text": load_text(first_summary_path),
        "second_all_df": read_csv(str(second_all_path)) if second_all_path else pd.DataFrame(),
        "second_passed_df": read_csv(str(second_passed_path)) if second_passed_path else pd.DataFrame(),
        "second_symbols_text": load_text(second_symbols_path),
    }


def etf_context_path_for_date(screen_date: pd.Timestamp) -> Path:
    normalized = pd.Timestamp(screen_date).normalize()
    return (
        ETF_CONTEXT_ROOT
        / normalized.strftime("%Y")
        / normalized.strftime("%m")
        / normalized.strftime("%Y%m%d")
        / f"etf_context_{normalized.strftime('%Y%m%d')}.csv"
    )


@st.cache_data(show_spinner=False)
def load_screening_history() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for screen_date in available_screen_dates():
        bundle = load_screening_day_bundle(screen_date)
        rows.append(
            {
                "screen_date": screen_date,
                "first_screen_count": len(bundle["first_passed_df"]),
                "second_screen_count": len(bundle["second_passed_df"]),
                "first_screen_total": len(bundle["first_all_df"]),
                "second_screen_total": len(bundle["second_all_df"]),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("screen_date").reset_index(drop=True)


@st.cache_data(show_spinner=False)
def load_breadth_history() -> pd.DataFrame:
    if not BREADTH_HISTORY_PATH.exists():
        return pd.DataFrame()
    df = pd.read_csv(BREADTH_HISTORY_PATH, keep_default_na=False)
    if df.empty:
        return df
    for col in ["date", "effective_date", "created_at"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", format="mixed")
    for col in [
        "universe_total",
        "valid_sma5_count",
        "valid_sma20_count",
        "above_sma5_count",
        "above_sma20_count",
        "above_sma5_pct",
        "above_sma20_pct",
        "missing_sma5_count",
        "missing_sma20_count",
        "above_sma20_pct_p20",
        "above_sma20_pct_p10",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def screening_outputs_exist(screen_date: pd.Timestamp) -> bool:
    normalized = pd.Timestamp(screen_date).normalize()
    stamp = normalized.strftime("%Y%m%d")
    required = [
        resolve_screening_artifact(normalized, f"first_screen_all_{stamp}.csv"),
        resolve_screening_artifact(normalized, f"first_screen_passed_{stamp}.csv"),
        resolve_screening_artifact(normalized, f"first_screen_summary_{stamp}.txt"),
        resolve_screening_artifact(normalized, f"second_screen_all_{stamp}.csv"),
        resolve_screening_artifact(normalized, f"second_screen_passed_{stamp}.csv"),
        resolve_screening_artifact(normalized, f"second_screen_symbols_{stamp}.txt"),
    ]
    return all(path is not None for path in required)


def first_missing_screen_date(latest_bd: pd.Timestamp | None) -> pd.Timestamp | None:
    if latest_bd is None:
        return None
    candidate = next_market_session(latest_bd)
    for _ in range(260):
        if candidate is None:
            return None
        if not screening_outputs_exist(candidate):
            return candidate
        candidate = next_market_session(candidate)
    return None


def trade_state_path_for_date(buy_date: pd.Timestamp) -> Path:
    normalized = pd.Timestamp(buy_date).normalize()
    return (
        TRADE_STATE_ROOT
        / normalized.strftime("%Y")
        / normalized.strftime("%m")
        / normalized.strftime("%Y%m%d")
        / f"trade_state_{normalized.strftime('%Y%m%d')}.csv"
    )


def trade_sizing_path_for_date(buy_date: pd.Timestamp) -> Path:
    normalized = pd.Timestamp(buy_date).normalize()
    return (
        TRADE_STATE_ROOT
        / normalized.strftime("%Y")
        / normalized.strftime("%m")
        / normalized.strftime("%Y%m%d")
        / f"trade_sizing_{normalized.strftime('%Y%m%d')}.csv"
    )


def render_header(state: pd.DataFrame) -> None:
    st.title("MLL1 E21PB Live")
    st.caption("Portfolio live trade_state-based. SD = Screen Date, BD = seduta operativa / data di ingresso.")
    latest_bd = latest_date(state, "date")
    if latest_bd is not None:
        st.success(f"Portfolio aggiornato a BD {latest_bd.strftime('%Y-%m-%d')}")
    else:
        st.warning("Portfolio state non trovato.")


def render_overview_tab(state: pd.DataFrame) -> None:
    st.subheader("Overview")
    latest_bd = latest_date(state, "date")
    suggested_sd = first_missing_screen_date(latest_bd)
    suggested_bd = previous_market_session(suggested_sd) if suggested_sd is not None else None
    latest_sd = latest_available_screen_date()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Ultima BD portfolio", latest_bd.strftime("%Y-%m-%d") if latest_bd is not None else "n/a")
    col2.metric("Prima SD senza screening", suggested_sd.strftime("%Y-%m-%d") if suggested_sd is not None else "n/a")
    col3.metric("BD precedente alla SD", suggested_bd.strftime("%Y-%m-%d") if suggested_bd is not None else "n/a")
    col4.metric("Ultima SD con cartella", latest_sd.strftime("%Y-%m-%d") if latest_sd is not None else "n/a")

    st.markdown("Comando operativo:")
    st.code("venv/bin/python scripts/run_live_sd.py", language="bash")
    st.caption("Premi Invio al prompt per usare la SD suggerita.")

    rows = [
        {"item": "Workspace Root", "value": str(BASE_DIR)},
        {"item": "MARKET_DATA_ROOT", "value": str(MARKET_DATA_ROOT)},
        {"item": "portfolio_live", "value": str(PORTFOLIO_DIR)},
        {"item": "trade_state", "value": str(TRADE_STATE_ROOT)},
        {"item": "screening_day", "value": str(SCREENING_DAY_DIR)},
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def render_first_screen_summary_metrics(results_df: pd.DataFrame, passed_df: pd.DataFrame) -> None:
    analyzed_count = len(results_df)
    passed_count = len(passed_df)

    breadth_sma5 = "n/a"
    breadth_sma20 = "n/a"
    if not results_df.empty:
        if "above_sma5" in results_df.columns:
            above_sma5 = results_df["above_sma5"].astype(str).str.lower().isin({"true", "1", "yes"})
            breadth_sma5 = f"{above_sma5.mean() * 100:.2f}%"
        if "above_sma20" in results_df.columns:
            above_sma20 = results_df["above_sma20"].astype(str).str.lower().isin({"true", "1", "yes"})
            breadth_sma20 = f"{above_sma20.mean() * 100:.2f}%"

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Ticker analizzati", analyzed_count)
    col2.metric("Passed", passed_count)
    col3.metric("Breadth > SMA5", breadth_sma5)
    col4.metric("Breadth > SMA20", breadth_sma20)


def render_second_screen_summary_metrics(results_df: pd.DataFrame, screened_df: pd.DataFrame) -> None:
    analyzed_count = len(results_df)
    passed_count = len(screened_df)
    pass_rate = f"{(passed_count / analyzed_count * 100):.2f}%" if analyzed_count else "n/a"

    col1, col2, col3 = st.columns(3)
    col1.metric("Ticker analizzati", analyzed_count)
    col2.metric("Passed", passed_count)
    col3.metric("Pass rate", pass_rate)


def get_atr_distance_style(value: float | None) -> tuple[str, str, str, str]:
    if value is None or pd.isna(value):
        return ("#94a3b8", "rgba(148, 163, 184, 0.18)", "rgba(148, 163, 184, 0.36)", "N/D")

    numeric = float(value)
    if -2.0 <= numeric <= 2.0:
        return ("#15803d", "rgba(34, 197, 94, 0.18)", "rgba(34, 197, 94, 0.36)", "OK")
    if (-3.0 <= numeric < -2.0) or (2.0 < numeric <= 3.0):
        return ("#a16207", "rgba(234, 179, 8, 0.18)", "rgba(234, 179, 8, 0.36)", "ATT")
    return ("#b91c1c", "rgba(239, 68, 68, 0.18)", "rgba(239, 68, 68, 0.36)", "OUT")


def get_sma50_atr_distance_style(value: float | None) -> tuple[str, str, str, str]:
    if value is None or pd.isna(value):
        return ("#94a3b8", "rgba(148, 163, 184, 0.18)", "rgba(148, 163, 184, 0.36)", "N/D")

    numeric = float(value)
    if numeric < 0.0:
        return ("#b91c1c", "rgba(239, 68, 68, 0.18)", "rgba(239, 68, 68, 0.36)", "LOW")
    if numeric < 1.0:
        return ("#15803d", "rgba(34, 197, 94, 0.18)", "rgba(34, 197, 94, 0.36)", "OK")
    if numeric < 2.0:
        return ("#15803d", "rgba(34, 197, 94, 0.18)", "rgba(34, 197, 94, 0.36)", "GOOD")
    if numeric <= 3.0:
        return ("#15803d", "rgba(34, 197, 94, 0.18)", "rgba(34, 197, 94, 0.36)", "SWEET")
    if numeric <= 4.0:
        return ("#15803d", "rgba(34, 197, 94, 0.18)", "rgba(34, 197, 94, 0.36)", "OK")
    if numeric <= 4.5:
        return ("#a16207", "rgba(234, 179, 8, 0.18)", "rgba(234, 179, 8, 0.36)", "EXT")
    return ("#b91c1c", "rgba(239, 68, 68, 0.18)", "rgba(239, 68, 68, 0.36)", "HIGH")


def render_atr_badge(container, text_color: str, background: str, border: str, label: str) -> None:
    container.markdown(
        f"""
        <div style="margin-top:0.35rem;">
            <span style="
                display:inline-flex;
                align-items:center;
                gap:0.4rem;
                padding:0.2rem 0.55rem;
                border-radius:999px;
                border:1px solid {border};
                background:{background};
                color:{text_color};
                font-size:0.78rem;
                font-weight:700;
            ">
                <span style="
                    width:0.55rem;
                    height:0.55rem;
                    border-radius:999px;
                    background:{text_color};
                    display:inline-block;
                "></span>
                {label}
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def format_second_screen_param_chips(config: SecondScreenConfig) -> str:
    chips = [
        f"SMA 10 · window {config.sma10_window}",
        f"EMA 21 · window {config.ema21_window}",
        f"SMA 50 · window {config.sma50_window}",
        f"ADR · window {config.adr_window}",
        f"Price vs SMA10 · max dist {config.max_dist_sma10_pct:.1f}%",
        f"Price vs EMA21 · max dist {config.max_dist_ema21_pct:.1f}%",
        f"EMA21 slope 5d · min {config.min_ema21_slope_pct_5:.2f}%",
        f"Sessions since high · {config.min_sessions_since_high}-{config.max_sessions_since_high}",
        f"Prev open-close diff · <= {config.max_prev_day_open_close_diff_pct:.1f}%",
        f"Intraday vs ADR14 · <= {config.max_intraday_vs_adr14_multiple:.1f}x",
        f"Last day excursion · <= {config.max_last_day_excursion_pct:.1f}%",
    ]
    rendered_chips = "".join(
        (
            '<div style="display:inline-flex;align-items:center;padding:0.62rem 0.95rem;'
            'border-radius:14px;border:1px solid rgba(19,34,47,0.14);'
            'background:rgba(255,252,246,0.96);color:#13222f;font-weight:600;'
            'letter-spacing:0.01em;box-shadow:0 8px 20px rgba(19,34,47,0.06);">'
            f"{chip}</div>"
        )
        for chip in chips
    )
    return (
        '<div style="display:flex;flex-wrap:wrap;gap:0.55rem;margin:0.4rem 0 1rem 0;">'
        + rendered_chips
        + "</div>"
    )


def render_screening_count_history_chart(
    *,
    screen_date: pd.Timestamp,
    count_col: str,
    title: str,
    key_prefix: str,
) -> None:
    history_df = load_screening_history()
    if history_df.empty or count_col not in history_df.columns:
        st.info("Storico screening non disponibile.")
        return

    target_ts = pd.Timestamp(screen_date).normalize()
    min_ts = history_df["screen_date"].min()
    max_ts = history_df["screen_date"].max()
    default_end_ts = min(target_ts, max_ts)
    default_start_ts = min(max(pd.Timestamp("2026-01-01"), min_ts), default_end_ts)

    st.markdown(f"### {title}")
    st.caption("Serie storica derivata dagli artifact in `output/screening_day/`.")
    start_col, end_col = st.columns(2)
    start_date = start_col.date_input(
        "Da",
        value=default_start_ts.date(),
        min_value=min_ts.date(),
        max_value=max_ts.date(),
        key=f"{key_prefix}_history_start_{target_ts.date().isoformat()}",
    )
    end_date = end_col.date_input(
        "A",
        value=default_end_ts.date(),
        min_value=min_ts.date(),
        max_value=max_ts.date(),
        key=f"{key_prefix}_history_end_{target_ts.date().isoformat()}",
    )
    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    if start_ts > end_ts:
        st.warning("Start date successiva a End date.")
        return

    chart_df = history_df.loc[history_df["screen_date"].between(start_ts, end_ts), ["screen_date", count_col]].copy()
    if chart_df.empty:
        st.info("Nessun dato disponibile nell'intervallo selezionato.")
        return

    chart_df[f"{count_col}_sma20"] = chart_df[count_col].rolling(20, min_periods=1).mean()
    plot_df = chart_df.rename(
        columns={
            count_col: "Passed",
            f"{count_col}_sma20": "Passed SMA20",
        }
    ).melt(
        id_vars=["screen_date"],
        value_vars=["Passed", "Passed SMA20"],
        var_name="serie",
        value_name="count",
    )
    chart = (
        alt.Chart(plot_df)
        .mark_line()
        .encode(
            x=alt.X("screen_date:T", title=None),
            y=alt.Y("count:Q", title=None),
            color=alt.Color(
                "serie:N",
                scale=alt.Scale(domain=["Passed", "Passed SMA20"], range=["#2563eb", "#eab308"]),
                title=None,
            ),
            tooltip=[
                alt.Tooltip("screen_date:T", title="Data"),
                alt.Tooltip("serie:N", title="Serie"),
                alt.Tooltip("count:Q", title="Valore", format=".2f"),
            ],
        )
        .properties(height=320)
    )
    st.altair_chart(chart, use_container_width=True)


def render_second_screen_last_90_days_chart(screen_date: pd.Timestamp) -> None:
    history_df = load_screening_history()
    if history_df.empty or "second_screen_count" not in history_df.columns:
        st.info("Storico second screen non disponibile.")
        return

    target_ts = pd.Timestamp(screen_date).normalize()
    chart_df = history_df.loc[
        history_df["screen_date"] <= target_ts,
        ["screen_date", "second_screen_count"],
    ].copy()
    if chart_df.empty:
        st.info("Nessun dato storico second screen disponibile fino alla SD selezionata.")
        return

    chart_df = chart_df.sort_values("screen_date").tail(90).copy()
    chart_df["second_screen_sma20"] = chart_df["second_screen_count"].rolling(20, min_periods=1).mean()
    plot_df = chart_df.rename(
        columns={
            "second_screen_count": "Passed",
            "second_screen_sma20": "Passed SMA20",
        }
    ).melt(
        id_vars=["screen_date"],
        value_vars=["Passed", "Passed SMA20"],
        var_name="serie",
        value_name="count",
    )
    chart = (
        alt.Chart(plot_df)
        .mark_line()
        .encode(
            x=alt.X("screen_date:T", title=None),
            y=alt.Y("count:Q", title=None),
            color=alt.Color(
                "serie:N",
                scale=alt.Scale(domain=["Passed", "Passed SMA20"], range=["#7c3aed", "#ea580c"]),
                title=None,
            ),
            tooltip=[
                alt.Tooltip("screen_date:T", title="Data"),
                alt.Tooltip("serie:N", title="Serie"),
                alt.Tooltip("count:Q", title="Valore", format=".2f"),
            ],
        )
        .properties(height=320)
    )
    st.markdown("### Passed ultimi 90 giorni")
    st.caption("Ultime 90 SD disponibili negli artifact `screening_day`.")
    st.altair_chart(chart, use_container_width=True)


def render_second_screen_etf_context_section(
    screened_df: pd.DataFrame,
    screen_date: pd.Timestamp,
    *,
    key_prefix: str = "second_screen",
) -> None:
    st.markdown("### ETF Context")
    report_path = etf_context_path_for_date(screen_date)
    action_col1, action_col2 = st.columns([1, 3])
    with action_col1:
        rebuild = st.button(
            "Rigenera ETF context",
            key=f"{key_prefix}_rebuild_etf_context_{pd.Timestamp(screen_date).strftime('%Y%m%d')}",
            disabled=screened_df.empty or "ticker" not in screened_df.columns,
        )
    with action_col2:
        st.caption(f"Report: {report_path}")

    if rebuild:
        tickers = screened_df.get("ticker", pd.Series(dtype=str)).astype(str).str.strip().str.upper().tolist()
        tickers = [ticker for ticker in tickers if ticker]
        if tickers:
            with st.spinner("Calcolo ETF context sui ticker del second screen..."):
                save_etf_context_for_sd(pd.Timestamp(screen_date).normalize(), tickers=tickers)
            read_csv.clear()
            st.success("ETF context rigenerato.")
            st.rerun()
        else:
            st.warning("Nessun ticker valido nel second screen.")

    report_df = read_csv(str(report_path))
    if report_df.empty:
        st.info("Nessun report ETF context salvato per questa SD.")
        return

    if not screened_df.empty and "ticker" in screened_df.columns and "ticker" in report_df.columns:
        live_tickers = set(screened_df["ticker"].astype(str).str.strip().str.upper())
        report_df = report_df.loc[report_df["ticker"].astype(str).str.strip().str.upper().isin(live_tickers)].copy()

    st.dataframe(report_df, width="stretch", hide_index=True)


def render_screen_date_picker(state_key: str, default_screen_date: pd.Timestamp | None, label: str) -> pd.Timestamp:
    if default_screen_date is None:
        default_screen_date = pd.Timestamp.today().normalize()
    if state_key not in st.session_state:
        st.session_state[state_key] = default_screen_date.date()

    nav_col1, nav_col2, nav_col3 = st.columns([1, 2, 1])
    with nav_col1:
        if st.button("-1 giorno", key=f"{state_key}_prev_day"):
            st.session_state[state_key] = (pd.Timestamp(st.session_state[state_key]) - pd.Timedelta(days=1)).date()
    with nav_col3:
        if st.button("+1 giorno", key=f"{state_key}_next_day"):
            st.session_state[state_key] = (pd.Timestamp(st.session_state[state_key]) + pd.Timedelta(days=1)).date()

    selected_date = st.date_input(label, value=st.session_state[state_key], key=state_key)
    return pd.Timestamp(selected_date).normalize()


def render_first_screen_tab() -> None:
    st.subheader("First Screen")
    default_sd = latest_available_screen_date()
    selected_sd = render_screen_date_picker("first_screen_sd", default_sd, "SD first screen")
    bundle = load_screening_day_bundle(selected_sd)
    first_all_df = bundle["first_all_df"]
    first_passed_df = bundle["first_passed_df"]
    first_summary_text = bundle["first_summary_text"]

    if first_all_df.empty and first_passed_df.empty:
        st.info(f"Nessun output first screen trovato per SD {selected_sd.strftime('%Y-%m-%d')}.")
        return

    st.caption(f"Risultati del first screen per SD {selected_sd.strftime('%Y-%m-%d')}.")
    render_first_screen_summary_metrics(first_all_df, first_passed_df)

    if first_summary_text:
        with st.expander("Summary txt"):
            st.text(first_summary_text)

    st.markdown("Passed")
    st.dataframe(first_passed_df, width="stretch", hide_index=True)
    if not first_passed_df.empty and "ticker" in first_passed_df.columns:
        st.code(", ".join(first_passed_df["ticker"].astype(str).tolist()))

    if not first_all_df.empty:
        with st.expander("Mostra tutti i risultati del first screen"):
            st.dataframe(first_all_df, width="stretch", hide_index=True)


def render_second_screen_tab() -> None:
    st.subheader("Second Screen")
    default_sd = latest_available_screen_date()
    selected_sd = render_screen_date_picker("second_screen_sd", default_sd, "SD second screen")
    bundle = load_screening_day_bundle(selected_sd)
    second_all_df = bundle["second_all_df"]
    second_passed_df = bundle["second_passed_df"]
    second_symbols_text = bundle["second_symbols_text"]

    if second_all_df.empty and second_passed_df.empty:
        st.info(f"Nessun output second screen trovato per SD {selected_sd.strftime('%Y-%m-%d')}.")
        return

    st.caption(f"Risultati del second screen per SD {selected_sd.strftime('%Y-%m-%d')}.")
    render_second_screen_summary_metrics(second_all_df, second_passed_df)

    st.markdown("### Parametri Second Screen")
    st.caption("Parametri standard correnti usati dal filtro di secondo screen.")
    st.markdown(
        format_second_screen_param_chips(SecondScreenConfig()),
        unsafe_allow_html=True,
    )

    if second_symbols_text:
        st.markdown("Ticker passati")
        st.code(second_symbols_text.strip())

    st.markdown("Passed")
    st.dataframe(second_passed_df, width="stretch", hide_index=True)
    if not second_passed_df.empty and "ticker" in second_passed_df.columns:
        chart_labels = second_passed_df["ticker"].astype(str).str.strip().str.upper().tolist()
        selected_chart_ticker = st.selectbox(
            "Ticker chart",
            options=chart_labels,
            index=0,
            key="second_screen_chart_ticker",
        )
        components.html(
            f"""
            <div class="tradingview-widget-container" style="height:560px;width:100%;">
              <div id="tradingview_second_screen_chart" style="height:100%;width:100%;"></div>
              <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
              <script type="text/javascript">
                new TradingView.widget({{
                  "autosize": true,
                  "symbol": "{selected_chart_ticker}",
                  "interval": "D",
                  "timezone": "Europe/Rome",
                  "theme": "light",
                  "style": "1",
                  "locale": "it",
                  "toolbar_bg": "#f5efe3",
                  "enable_publishing": false,
                  "hide_top_toolbar": false,
                  "hide_legend": false,
                  "save_image": false,
                  "container_id": "tradingview_second_screen_chart",
                  "studies": ["Volume@tv-basicstudies"]
                }});
              </script>
            </div>
            """,
            height=580,
        )

    render_screening_count_history_chart(
        screen_date=selected_sd,
        count_col="second_screen_count",
        title="Storico Second Screen Count",
        key_prefix="second_screen_count",
    )

    if not second_all_df.empty:
        with st.expander("Mostra tutti i risultati del second screen"):
            st.dataframe(second_all_df, width="stretch", hide_index=True)

    render_second_screen_etf_context_section(second_passed_df, selected_sd, key_prefix="second_screen")
    render_second_screen_last_90_days_chart(selected_sd)


def render_portfolio_tab(state: pd.DataFrame, positions: pd.DataFrame, actions: pd.DataFrame) -> None:
    st.subheader("Portfolio")
    if state.empty:
        st.warning("Nessun portfolio_state_daily.csv disponibile.")
        return

    latest_bd = latest_date(state, "date")
    latest_state = state.loc[to_date_series(state["date"]).eq(latest_bd)].tail(1).iloc[0]

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Equity MTM R", latest_state.get("equity_mtm_r", "n/a"))
    col2.metric("Realized R", latest_state.get("realized_r_cum", "n/a"))
    col3.metric("Unrealized R", latest_state.get("unrealized_r", "n/a"))
    col4.metric("Open positions", latest_state.get("open_positions_count", "n/a"))
    col5.metric("Drawdown MTM R", latest_state.get("drawdown_mtm_r", "n/a"))

    st.markdown("Ultimo state")
    st.dataframe(state.tail(12), width="stretch", hide_index=True)

    if not positions.empty and latest_bd is not None:
        latest_positions = positions.loc[to_date_series(positions["date"]).eq(latest_bd)].copy()
        open_positions = latest_positions.loc[latest_positions["is_open"].astype(str).str.lower().isin(["true", "1"])]
        st.markdown("Posizioni aperte ultima BD")
        cols = [
            "ticker",
            "bd" if "bd" in open_positions.columns else "entry_date",
            "r_multiplier",
            "shares_initial",
            "shares_open",
            "realized_r_partial",
            "unrealized_r",
            "total_mtm_r",
            "position_status",
        ]
        st.dataframe(open_positions[[c for c in cols if c in open_positions.columns]], width="stretch", hide_index=True)

    if not actions.empty and latest_bd is not None:
        latest_actions = actions.loc[to_date_series(actions["action_date"]).eq(latest_bd)].copy()
        st.markdown(f"Azioni su BD {latest_bd.strftime('%Y-%m-%d')}")
        if latest_actions.empty:
            st.info("Nessuna action registrata per l'ultima BD.")
        else:
            cols = [
                "action_date",
                "ticker",
                "action_type",
                "shares_delta",
                "shares_open_after",
                "realized_r_delta",
                "realized_r_cum_position",
                "note",
            ]
            st.dataframe(latest_actions[[c for c in cols if c in latest_actions.columns]], width="stretch", hide_index=True)


def render_operations_tab(actions: pd.DataFrame) -> None:
    st.subheader("Operazioni")
    st.caption("Una riga per ogni operazione eseguita. Ordinamento di default: cronologico inverso.")

    if actions.empty:
        st.warning("Nessun portfolio_actions_daily.csv disponibile.")
        return

    operations = actions.copy()
    if "action_date" in operations.columns:
        operations["action_date"] = pd.to_datetime(operations["action_date"], errors="coerce").dt.normalize()
    date_col = "bd" if "bd" in operations.columns else "entry_date" if "entry_date" in operations.columns else None
    if date_col is not None:
        operations[date_col] = pd.to_datetime(operations[date_col], errors="coerce").dt.normalize()
    for col in ["action_seq", "shares_delta", "shares_open_after"]:
        if col in operations.columns:
            operations[col] = pd.to_numeric(operations[col], errors="coerce")
    for col in ["realized_r_delta", "realized_r_cum_position"]:
        if col in operations.columns:
            operations[col] = pd.to_numeric(operations[col], errors="coerce")

    sort_cols = [col for col in ["action_date", "action_seq"] if col in operations.columns]
    if sort_cols:
        operations = operations.sort_values(sort_cols, ascending=[False] * len(sort_cols), kind="stable")

    latest_action_date = latest_date(operations, "action_date")
    bd_count = 0
    if "action_type" in operations.columns and "action_date" in operations.columns:
        buy_mask = operations["action_type"].astype(str).str.upper().eq("BUY")
        bd_count = operations.loc[buy_mask, "action_date"].nunique()
    metric_col1, metric_col2, metric_col3 = st.columns(3)
    metric_col1.metric("Totale operazioni", len(operations))
    metric_col2.metric(
        "Data piu recente",
        "N/D" if latest_action_date is None else latest_action_date.strftime("%Y-%m-%d"),
    )
    metric_col3.metric("BD con BUY", int(bd_count))

    display_columns = [
        col
        for col in [
            "action_date",
            "ticker",
            "action_type",
            date_col if date_col is not None else "bd",
            "action_seq",
            "shares_delta",
            "shares_open_after",
            "realized_r_delta",
            "realized_r_cum_position",
            "note",
            "position_id",
        ]
        if col in operations.columns
    ]
    display_df = operations[display_columns].copy() if display_columns else operations.copy()
    st.dataframe(display_df, width="stretch", hide_index=True)


def render_market_tab(state: pd.DataFrame, momentum: pd.DataFrame) -> None:
    st.subheader("Market")
    default_sd = latest_available_screen_date()
    selected_sd = render_screen_date_picker("market_sd", default_sd, "SD market")
    effective_sd = pd.Timestamp(selected_sd).normalize()

    semaforo_result = None
    blue_on = None
    blue_on_weak_count = None
    daily_history = None
    try:
        daily_history = load_daily_history_from_cache("SPY")
        semaforo_result = compute_market_street_light_for_date(effective_sd)
        blue_on, blue_on_weak_count = compute_blue_on_for_date(effective_sd)
    except Exception as exc:
        st.warning(f"Impossibile calcolare il semaforo di mercato: {exc}")

    with st.container(border=True):
        st.markdown("### Semaforo di Mercato")
        market_col1, market_col2, market_col3, market_col4 = st.columns(4)
        market_col1.metric(
            "Semaforo",
            semaforo_result.market_street_light if semaforo_result is not None else "N/D",
        )
        market_col2.metric("Blue On", "YES" if blue_on else "NO" if blue_on is not None else "N/D")
        market_col3.metric(
            "Blue On weak count",
            "N/D" if blue_on_weak_count is None else str(blue_on_weak_count),
        )
        market_col4.metric("SD", effective_sd.strftime("%Y-%m-%d"))

        if semaforo_result is not None:
            rule_col1, rule_col2, rule_col3, rule_col4 = st.columns(4)
            rule_col1.metric("Rule 5DMA", "YES" if semaforo_result.rule_5dma else "NO")
            rule_col2.metric("Rule Daily Buy", "YES" if semaforo_result.rule_daily_buy else "NO")
            rule_col3.metric("Rule Weekly Buy", "YES" if semaforo_result.rule_weekly_buy else "NO")
            rule_col4.metric("Core score", f"{semaforo_result.core_score}/3")

            price_col1, price_col2, price_col3, price_col4 = st.columns(4)
            price_col1.metric("Close", f"{semaforo_result.close:.2f}")
            price_col2.metric("SMA5", f"{semaforo_result.sma5:.2f}")
            price_col3.metric("SMA10", f"{semaforo_result.sma10:.2f}")
            price_col4.metric("SMA20", f"{semaforo_result.sma20:.2f}")

    if semaforo_result is not None:
        with st.container(border=True):
            st.markdown("### Dettaglio Regole")
            detail_df = pd.DataFrame(
                [
                    {
                        "rule": "5DMA",
                        "passed": semaforo_result.rule_5dma,
                        "registrato": f"{semaforo_result.close:.2f}",
                        "medie": f"SMA5 {semaforo_result.sma5:.2f}",
                    },
                    {
                        "rule": "Daily Buy",
                        "passed": semaforo_result.rule_daily_buy,
                        "registrato": f"{semaforo_result.close:.2f}",
                        "medie": f"SMA10 {semaforo_result.sma10:.2f} | SMA20 {semaforo_result.sma20:.2f}",
                    },
                    {
                        "rule": "Weekly Buy",
                        "passed": semaforo_result.rule_weekly_buy,
                        "registrato": f"{semaforo_result.weekly_close:.2f}",
                        "medie": (
                            f"W-SMA10 {semaforo_result.weekly_sma10:.2f} | "
                            f"W-SMA20 {semaforo_result.weekly_sma20:.2f}"
                        ),
                    },
                ]
            )
            st.dataframe(detail_df, width="stretch", hide_index=True)

        if daily_history is not None and not daily_history.empty:
            try:
                trading_dates = pd.Index(pd.to_datetime(daily_history.index)).sort_values().unique()
                anchor_candidates = trading_dates[trading_dates <= effective_sd]
                if len(anchor_candidates) > 0:
                    anchor_date = pd.Timestamp(anchor_candidates[-1]).normalize()
                    anchor_idx = trading_dates.get_loc(anchor_date)
                    start_idx = max(0, anchor_idx - 10)
                    end_idx = min(len(trading_dates) - 1, anchor_idx + 10)
                    window_dates = trading_dates[start_idx : end_idx + 1]

                    window_rows: list[dict[str, object]] = []
                    for dt in window_dates:
                        window_result = compute_market_street_light_for_date(
                            pd.Timestamp(dt),
                            ticker="SPY",
                            daily_df=daily_history,
                        )
                        window_rows.append(
                            {
                                "date": pd.Timestamp(dt).strftime("%Y-%m-%d"),
                                "core_score": window_result.core_score,
                                "semaforo_color": window_result.market_street_light,
                                "is_selected": pd.Timestamp(dt).normalize() == anchor_date,
                            }
                        )

                    with st.container(border=True):
                        st.markdown("### Intorno della Data")
                        st.caption(
                            "Sequenza del semaforo sulle sedute di mercato: 10 prima e 10 dopo la data effettiva usata dal calcolo."
                        )
                        if anchor_date != effective_sd:
                            st.caption(
                                f"La data selezionata non e' una seduta di mercato; l'ancora usata e' {anchor_date.strftime('%Y-%m-%d')}."
                            )

                        cards = []
                        for row in window_rows:
                            color = row["semaforo_color"]
                            bg, border, text = get_semaphore_style(color)
                            selected_style = (
                                "box-shadow: 0 0 0 4px rgba(19, 34, 47, 0.14); "
                                "border-width: 3px; "
                            ) if row["is_selected"] else ""
                            selected_label = (
                                "<div style='font-size:0.72rem; opacity:0.8;'>data selezionata</div>"
                                if row["is_selected"]
                                else ""
                            )
                            min_width = "138px" if row["is_selected"] else "96px"
                            padding = "0.9rem 0.95rem" if row["is_selected"] else "0.58rem 0.62rem"
                            date_font = "0.88rem" if row["is_selected"] else "0.72rem"
                            color_font = "1.12rem" if row["is_selected"] else "0.9rem"
                            score_font = "0.84rem" if row["is_selected"] else "0.74rem"
                            cards.append(
                                f"""
                                <div style="
                                    min-width: {min_width};
                                    padding: {padding};
                                    border-radius: 14px;
                                    border: 1px solid {border};
                                    background: {bg};
                                    color: {text};
                                    {selected_style}
                                " {'data-selected="true"' if row["is_selected"] else ""}>
                                    <div style="font-size:{date_font}; font-weight:600;">{row['date']}</div>
                                    <div style="font-size:{color_font}; font-weight:800; margin-top:0.25rem;">{color}</div>
                                    <div style="font-size:{score_font}; margin-top:0.2rem;">score {row['core_score']}/3</div>
                                    {selected_label}
                                </div>
                                """
                            )

                        components.html(
                            f"""
                            <div id="semaphore-strip" style="
                                display:flex;
                                gap:0.7rem;
                                overflow-x:auto;
                                padding:0.25rem 0 0.5rem 0;
                                background: transparent;
                            ">
                                {''.join(cards)}
                            </div>
                            <script>
                            const strip = document.getElementById("semaphore-strip");
                            const selected = strip ? strip.querySelector('[data-selected="true"]') : null;
                            if (strip && selected) {{
                                const targetLeft =
                                    selected.offsetLeft - (strip.clientWidth / 2) + (selected.clientWidth / 2);
                                strip.scrollLeft = Math.max(0, targetLeft);
                            }}
                            </script>
                            """,
                            height=190,
                            scrolling=True,
                        )

                        table_df = pd.DataFrame(window_rows).rename(columns={"is_selected": "selected_date"})
                        st.dataframe(table_df, width="stretch", hide_index=True)
            except Exception as exc:
                st.caption(f"Intorno della Data non disponibile: {exc}")

    breadth_history_df = load_breadth_history()
    breadth_row = pd.DataFrame()
    if not breadth_history_df.empty and "effective_date" in breadth_history_df.columns:
        breadth_row = breadth_history_df.loc[breadth_history_df["effective_date"] == effective_sd].tail(1)

    with st.container(border=True):
        st.markdown("### Breadth")
        if breadth_row.empty:
            st.info("Nessun dato breadth salvato per questa SD.")
        else:
            breadth = breadth_row.iloc[-1]
            breadth_col1, breadth_col2, breadth_col3, breadth_col4 = st.columns(4)
            breadth_col1.metric("Breadth > SMA20", f"{float(breadth.get('above_sma20_pct', 0.0)):.2f}%")
            breadth_col2.metric("Breadth > SMA5", f"{float(breadth.get('above_sma5_pct', 0.0)):.2f}%")
            breadth_col3.metric(
                "Valid SMA20 / Universe",
                f"{int(breadth.get('valid_sma20_count', 0) or 0)}/{int(breadth.get('universe_total', 0) or 0)}",
            )
            breadth_col4.metric(
                "P20 / P10",
                f"{float(breadth.get('above_sma20_pct_p20', 0.0)):.2f}% / {float(breadth.get('above_sma20_pct_p10', 0.0)):.2f}%",
            )
            st.caption(
                f"SMA20: {int(breadth.get('above_sma20_count', 0) or 0)}/{int(breadth.get('valid_sma20_count', 0) or 0)} ticker sopra media, "
                f"SMA5: {int(breadth.get('above_sma5_count', 0) or 0)}/{int(breadth.get('valid_sma5_count', 0) or 0)}. "
                f"Mancanti: SMA20 {int(breadth.get('missing_sma20_count', 0) or 0)}, "
                f"SMA5 {int(breadth.get('missing_sma5_count', 0) or 0)}."
            )

            chart_df = breadth_history_df.loc[breadth_history_df["effective_date"] <= effective_sd].copy()
            if not chart_df.empty:
                chart_df = chart_df[["effective_date", "above_sma20_pct", "above_sma5_pct"]].tail(120)
                plot_df = chart_df.melt(
                    id_vars=["effective_date"],
                    value_vars=["above_sma20_pct", "above_sma5_pct"],
                    var_name="serie",
                    value_name="value",
                )
                chart = (
                    alt.Chart(plot_df)
                    .mark_line()
                    .encode(
                        x=alt.X("effective_date:T", title=None),
                        y=alt.Y("value:Q", title="% breadth"),
                        color=alt.Color(
                            "serie:N",
                            scale=alt.Scale(
                                domain=["above_sma20_pct", "above_sma5_pct"],
                                range=["#2563eb", "#f59e0b"],
                            ),
                            title=None,
                        ),
                        tooltip=[
                            alt.Tooltip("effective_date:T", title="Data"),
                            alt.Tooltip("serie:N", title="Serie"),
                            alt.Tooltip("value:Q", title="Valore", format=".2f"),
                        ],
                    )
                    .properties(height=320)
                )
                st.caption("Andamento storico della breadth dell'universo operativo fino alla SD selezionata.")
                st.altair_chart(chart, use_container_width=True)

    latest_bd = latest_date(state, "date")
    suggested_sd = first_missing_screen_date(latest_bd)
    status_col1, status_col2 = st.columns(2)
    status_col1.metric(
        "Prima SD senza screening",
        suggested_sd.strftime("%Y-%m-%d") if suggested_sd is not None else "n/a",
    )
    status_col2.metric(
        "Screening presente sulla SD selezionata",
        "YES" if screening_outputs_exist(effective_sd) else "NO",
    )

    st.markdown("### Ultimi screening presenti")
    rows: list[dict[str, object]] = []
    for day_dir in sorted(SCREENING_DAY_DIR.glob("2026/*/*"))[-20:]:
        if not day_dir.is_dir() or not day_dir.name.isdigit():
            continue
        sd = pd.Timestamp(day_dir.name)
        rows.append(
            {
                "SD": sd.strftime("%Y-%m-%d"),
                "complete": screening_outputs_exist(sd),
                "folder": str(day_dir.relative_to(BASE_DIR)),
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.markdown("### Momentum inv5d")
    if momentum.empty:
        st.info("Momentum signal non disponibile.")
    else:
        st.dataframe(momentum.tail(15), width="stretch", hide_index=True)


def render_trade_console_tab() -> None:
    st.subheader("Trade Console")
    st.caption(
        "Console operativa per un ticker singolo: verifica baseline second screen, mercato, ETF context e trade stimato/reale."
    )

    default_sd = latest_available_screen_date()
    selected_sd = render_screen_date_picker("trade_console_sd", default_sd, "SD trade console")
    bundle = load_screening_day_bundle(selected_sd)
    second_screen_df = bundle["second_passed_df"].copy()

    payload_preview = build_trade_console_payload("SPY", selected_sd) if False else None
    meta_col1, meta_col2, meta_col3 = st.columns(3)
    meta_col1.metric("Screen Date", selected_sd.strftime("%Y-%m-%d"))
    buy_hint = next_market_session(pd.Timestamp(selected_sd).normalize()) or pd.Timestamp(selected_sd).normalize()
    meta_col2.metric("BD", buy_hint.strftime("%Y-%m-%d"))
    meta_col3.metric("Ticker in watchlist", int(len(second_screen_df)))

    ticker_source_options = ["Ticker discrezionale"]
    if not second_screen_df.empty and "ticker" in second_screen_df.columns:
        ticker_source_options.append("Ticker da second screen")

    ticker_source = st.radio(
        "Ticker source",
        options=ticker_source_options,
        horizontal=True,
        key="trade_console_ticker_source",
    )

    selected_row = None
    if ticker_source == "Ticker da second screen":
        option_df = second_screen_df.copy()
        option_df["ticker"] = option_df["ticker"].astype(str).str.strip().str.upper()
        ticker_options = option_df["ticker"].dropna().tolist()
        if not ticker_options:
            st.info("Il second screen non contiene ticker selezionabili per questa SD.")
            return
        selected_ticker = st.selectbox(
            "Ticker second screen",
            options=ticker_options,
            index=0,
            key="trade_console_selected_second_screen_ticker",
        )
        selected_match = option_df.loc[option_df["ticker"] == selected_ticker]
        if selected_match.empty:
            st.warning("Impossibile trovare il ticker selezionato nel second screen.")
            return
        selected_row = selected_match.iloc[-1]
        ticker_input = selected_ticker
    else:
        ticker_input = st.text_input(
            "Ticker",
            value="",
            key="trade_console_manual_ticker",
            placeholder="Es. NVDA",
        ).strip().upper()

    if not ticker_input:
        st.info("Inserisci un ticker oppure selezionalo dal second screen.")
        return

    console = build_trade_console_payload(ticker_input, selected_sd, second_screen_row=selected_row)
    bd = pd.Timestamp(console["bd"]).normalize()
    breadth_history = load_breadth_history()
    price_snapshot = console.get("price_snapshot", {})
    analysis_row = console.get("analysis_row") or {}
    etf_context = console.get("etf_context")
    reference_etf_snapshot = console.get("reference_etf_snapshot") or {}
    dist_ema21 = console.get("dist_ema21_atr14_multiple")
    dist_sma50 = analysis_row.get("dist_sma50_atr14_multiple")
    spy_dist_ema21 = reference_etf_snapshot.get("dist_ema21_atr14_multiple")
    spy_dist_sma50 = reference_etf_snapshot.get("dist_sma50_atr14_multiple")
    atr_text, atr_bg, atr_border, atr_label = get_atr_distance_style(dist_ema21)
    sma50_text, sma50_bg, sma50_border, sma50_label = get_sma50_atr_distance_style(dist_sma50)
    spy_atr_text, spy_atr_bg, spy_atr_border, spy_atr_label = get_atr_distance_style(spy_dist_ema21)
    spy_sma50_text, spy_sma50_bg, spy_sma50_border, spy_sma50_label = get_sma50_atr_distance_style(spy_dist_sma50)
    with st.container(border=True):
        st.markdown("### Dati")
        data_col1, data_col2, data_col3, data_col4 = st.columns(4)
        data_col1.metric("Ticker", str(console["ticker"]))
        data_col2.metric("Baseline compatible", "YES" if analysis_row.get("passed_second_screen") else "NO")
        data_col3.metric(
            f"Close {selected_sd.strftime('%Y-%m-%d')}",
            "N/D" if price_snapshot.get("close") is None else f"{float(price_snapshot['close']):.2f}",
        )
        data_col4.metric(
            f"High {selected_sd.strftime('%Y-%m-%d')}",
            "N/D" if price_snapshot.get("high") is None else f"{float(price_snapshot['high']):.2f}",
        )

        data_col6, data_col7, data_col8, data_col9, data_col10 = st.columns(5)
        data_col6.metric(
            f"Low {selected_sd.strftime('%Y-%m-%d')}",
            "N/D" if price_snapshot.get("low") is None else f"{float(price_snapshot['low']):.2f}",
        )
        data_col7.metric(
            "Price change %",
            "N/D" if price_snapshot.get("price_change_pct") is None else f"{float(price_snapshot['price_change_pct']):+.2f}%",
        )
        data_col8.metric(
            "Close Range Position",
            "N/D"
            if price_snapshot.get("close_range_position_pct") is None
            else f"{float(price_snapshot['close_range_position_pct']):.1f}%",
        )
        data_col9.metric(
            "EMA21 slope 5d",
            "N/D" if analysis_row.get("ema21_slope_pct_5") is None else f"{float(analysis_row['ema21_slope_pct_5']):.2f}%",
        )
        data_col10.metric(
            "ATR14 %",
            "N/D" if analysis_row.get("atr14_pct") is None else f"{float(analysis_row['atr14_pct']):.2f}%",
        )

        data_col11, data_col12 = st.columns(2)
        data_col11.metric(
            "Ticker vs EMA21 (xATR)",
            "N/D"
            if dist_ema21 is None
            else f"{float(dist_ema21):.2f}",
        )
        render_atr_badge(data_col11, atr_text, atr_bg, atr_border, atr_label)
        data_col12.metric(
            "Dist SMA50 (ATR)",
            "N/D"
            if dist_sma50 is None
            else f"{float(dist_sma50):.2f}",
        )
        render_atr_badge(data_col12, sma50_text, sma50_bg, sma50_border, sma50_label)

    with st.container(border=True):
        st.markdown("### Mercato")
        market_col1, market_col2, market_col3 = st.columns(3)
        market_col1.metric("Semaforo", str(analysis_row.get("semaforo_color") or "N/D"))
        market_col2.metric("Blue On", "YES" if analysis_row.get("blue_on") else "NO")
        market_col3.metric(
            "Blue On weak count",
            "N/D" if analysis_row.get("blue_on_weak_count") is None else str(analysis_row.get("blue_on_weak_count")),
        )
        market_col4, market_col5 = st.columns(2)
        market_col4.metric(
            "SPY E21 xATR",
            "N/D" if spy_dist_ema21 is None else f"{float(spy_dist_ema21):.2f}",
        )
        render_atr_badge(market_col4, spy_atr_text, spy_atr_bg, spy_atr_border, spy_atr_label)
        market_col5.metric(
            "SPY SMA50 xATR",
            "N/D" if spy_dist_sma50 is None else f"{float(spy_dist_sma50):.2f}",
        )
        render_atr_badge(market_col5, spy_sma50_text, spy_sma50_bg, spy_sma50_border, spy_sma50_label)

    slope_value = analysis_row.get("ema21_slope_pct_5")
    slope_qualified = None if slope_value is None else float(slope_value) > OFFICIAL_SIZING_SLOPE_THRESHOLD
    slope_add = OFFICIAL_SIZING_SLOPE_ADD if slope_qualified else 0.0

    momentum_value = None
    momentum_qualified = None
    momentum_reference_date = previous_market_session(previous_market_session(bd) or bd)
    momentum_signal_df = load_momentum_signal(DEFAULT_STRATEGY_ID, DEFAULT_VARIANT_ID, layer="live")
    if momentum_reference_date is not None and not momentum_signal_df.empty:
        momentum_row = momentum_signal_df.loc[momentum_signal_df["bd"] == momentum_reference_date].tail(1)
        if not momentum_row.empty:
            raw_momentum_value = pd.to_numeric(momentum_row.iloc[-1].get("prev_momentum_5d_sum"), errors="coerce")
            if pd.notna(raw_momentum_value):
                momentum_value = float(raw_momentum_value)
            bootstrap_neutral = bool(momentum_row.iloc[-1].get("bootstrap_neutral", False))
            raw_momentum_top_half = bool(momentum_row.iloc[-1].get("prev_momentum_5d_top_half", False))
            momentum_qualified = raw_momentum_top_half and not bootstrap_neutral
    momentum_add = OFFICIAL_SIZING_MOMENTUM_ADD if momentum_qualified else 0.0

    breadth_above_sma20 = None
    breadth_p20 = None
    breadth_p10 = None
    breadth_under_p20 = None
    breadth_under_p10 = None
    if not breadth_history.empty and "effective_date" in breadth_history.columns:
        breadth_df = breadth_history.copy()
        breadth_df["effective_date"] = pd.to_datetime(breadth_df["effective_date"], errors="coerce").dt.normalize()
        breadth_row = breadth_df.loc[breadth_df["effective_date"] == selected_sd].tail(1)
        if not breadth_row.empty:
            raw_above_sma20 = pd.to_numeric(breadth_row.iloc[-1].get("above_sma20_pct"), errors="coerce")
            raw_p20 = pd.to_numeric(breadth_row.iloc[-1].get("above_sma20_pct_p20"), errors="coerce")
            raw_p10 = pd.to_numeric(breadth_row.iloc[-1].get("above_sma20_pct_p10"), errors="coerce")
            if pd.notna(raw_above_sma20):
                breadth_above_sma20 = float(raw_above_sma20)
            if pd.notna(raw_p20):
                breadth_p20 = float(raw_p20)
            if pd.notna(raw_p10):
                breadth_p10 = float(raw_p10)
            if breadth_above_sma20 is not None and breadth_p20 is not None:
                breadth_under_p20 = breadth_above_sma20 < breadth_p20
            if breadth_above_sma20 is not None and breadth_p10 is not None:
                breadth_under_p10 = breadth_above_sma20 < breadth_p10

    breadth_add = 0.0
    if breadth_under_p10:
        breadth_add = OFFICIAL_SIZING_SMA20_P10_ADD
    elif breadth_under_p20:
        breadth_add = OFFICIAL_SIZING_SMA20_P20_ADD

    total_multiplier = 1.0 + slope_add + momentum_add + breadth_add
    etf_allowed = bool(getattr(etf_context, "allowed", False)) if etf_context is not None else None
    etf_multiplier = (
        ETF_SCORE_PASS_MULTIPLIER
        if etf_allowed is True
        else ETF_SCORE_FAIL_MULTIPLIER
        if etf_allowed is False
        else None
    )
    total_with_etf = total_multiplier * etf_multiplier if etf_multiplier is not None else None
    effective_r_multiplier = total_with_etf if total_with_etf is not None else total_multiplier
    effective_risk_amount = 25.0 * (total_with_etf if total_with_etf is not None else total_multiplier)
    entry_type = "BUY AT OPEN" if dist_ema21 is not None and float(dist_ema21) > 0 else "U&R"

    with st.container(border=True):
        st.markdown("### Moltiplicatori")
        mult_col1, mult_col2, mult_col3, mult_col4, mult_col5 = st.columns(5)
        mult_col1.metric(
            f"EMA21 slope > {OFFICIAL_SIZING_SLOPE_THRESHOLD:.2f}",
            "N/D" if slope_value is None else f"{float(slope_value):.2f}%",
            delta=format_yes_no(slope_qualified),
            delta_color=metric_delta_color(slope_qualified),
        )
        mult_col2.metric(
            "Momentum 5d in R (BD-2)",
            "N/D" if momentum_value is None else f"{float(momentum_value):.2f}R",
            delta=(
                format_yes_no(momentum_qualified)
                + (f" | ref {momentum_reference_date.strftime('%Y-%m-%d')}" if momentum_reference_date is not None else "")
            )
            if momentum_qualified is not None
            else (f"ref {momentum_reference_date.strftime('%Y-%m-%d')}" if momentum_reference_date is not None else None),
            delta_color=metric_delta_color(momentum_qualified),
        )
        mult_col3.metric(
            f"%SMA20 < p20 ({'N/D' if breadth_p20 is None else f'{breadth_p20:.2f}'})",
            "N/D" if breadth_above_sma20 is None else f"{float(breadth_above_sma20):.2f}%",
            delta=format_yes_no(breadth_under_p20),
            delta_color=metric_delta_color(breadth_under_p20),
        )
        mult_col4.metric(
            f"%SMA20 < p10 ({'N/D' if breadth_p10 is None else f'{breadth_p10:.2f}'})",
            "N/D" if breadth_above_sma20 is None else f"{float(breadth_above_sma20):.2f}%",
            delta=format_yes_no(breadth_under_p10),
            delta_color=metric_delta_color(breadth_under_p10),
        )
        mult_col5.metric("Totale", f"{total_multiplier:.2f}x")

        mult2_col1, mult2_col2 = st.columns(2)
        mult2_col1.metric(
            "ETF mult",
            "N/D" if etf_multiplier is None else f"{etf_multiplier:.2f}x",
            delta=None if etf_context is None else str(getattr(etf_context, "reason", "")),
        )
        mult2_col2.metric(
            "Totale con ETF",
            "N/D" if total_with_etf is None else f"{total_with_etf:.2f}x",
        )
        st.caption(
            f"Candidato live: slope +{slope_add:.2f}x, momentum +{momentum_add:.2f}x, breadth +{breadth_add:.2f}x."
            + (
                f" ETF {'GREEN' if etf_allowed else 'RED'} applica {etf_multiplier:.2f}x, totale {total_with_etf:.2f}x."
                if etf_multiplier is not None and total_with_etf is not None
                else " ETF non disponibile per la data selezionata."
            )
        )

    if etf_context is not None:
        with st.container(border=True):
            st.markdown("### ETF Filter")
            etf_col1, etf_col2, etf_col3, etf_col4 = st.columns(4)
            etf_col1.metric("ETF filter", "GREEN" if bool(getattr(etf_context, "allowed", False)) else "RED")
            etf_col2.metric(
                "Context score",
                "N/D"
                if getattr(etf_context, "context_score", None) is None
                else f"{float(etf_context.context_score):.2f}",
            )
            etf_col3.metric(
                "ETF percentile",
                "N/D"
                if getattr(etf_context, "reference_etf_percentile", None) is None
                else f"{float(etf_context.reference_etf_percentile):.1f}%",
            )
            etf_col4.metric(
                "Avg RS vs SPY",
                "N/D"
                if getattr(etf_context, "average_relative_strength_pct", None) is None
                else f"{float(etf_context.average_relative_strength_pct):.2f}%",
            )

    trade_result = console.get("trade_result")
    estimated_trade = console.get("estimated_trade")
    if trade_result is not None:
        with st.container(border=True):
            st.markdown("### Entry")
            risk_per_share = float(trade_result.entry_price) - float(trade_result.initial_stop_loss)
            adjusted_quantity = calculate_quantity(
                float(trade_result.entry_price),
                float(trade_result.initial_stop_loss),
                float(effective_risk_amount),
            )
            total_initial_risk = risk_per_share * adjusted_quantity
            trade_col1, trade_col2, trade_col3, trade_col4, trade_col5, trade_col6 = st.columns(6)
            trade_col1.metric("BD", pd.Timestamp(trade_result.buy_date).strftime("%Y-%m-%d"))
            trade_col2.metric("Entry Type", entry_type)
            trade_col3.metric("Entry", f"{float(trade_result.entry_price):.2f}")
            trade_col4.metric("Initial SL", f"{float(trade_result.initial_stop_loss):.2f}")
            trade_col5.metric(f"R con mult ({effective_r_multiplier:.2f}R)", f"{effective_risk_amount:.2f}")
            trade_col6.metric("Quantity", str(adjusted_quantity))
            trade_col7, trade_col8, trade_col9, trade_col10 = st.columns(4)
            trade_col7.metric("Risk per share", f"{risk_per_share:.2f}")
            trade_col8.metric("Total initial risk", f"{total_initial_risk:.2f}")
            trade_col9.metric("Final SL", f"{float(trade_result.stop_loss):.2f}")
            trade_col10.metric("Target Price 2R", f"{float(trade_result.entry_price) + (risk_per_share * 2.0):.2f}")

            lifecycle_rows = [
                {
                    "label": leg.label,
                    "date": pd.Timestamp(leg.date).strftime("%Y-%m-%d"),
                    "price": leg.price,
                    "shares": leg.shares,
                    "reason": leg.reason,
                }
                for leg in trade_result.legs
            ]
            if lifecycle_rows:
                with st.expander("Trade lifecycle"):
                    st.dataframe(pd.DataFrame(lifecycle_rows), width="stretch", hide_index=True)
    elif estimated_trade:
        with st.container(border=True):
            st.markdown("### Entry")
            st.info(
                "Trade reale non ancora simulabile: mostro una stima pre-trade basata sul Close della screen date."
            )
            estimated_entry = float(estimated_trade["entry_proxy"])
            estimated_stop = float(estimated_trade["stop_loss"])
            estimated_risk_per_share = estimated_entry - estimated_stop
            adjusted_quantity = calculate_quantity(
                estimated_entry,
                estimated_stop,
                float(effective_risk_amount),
            )
            total_initial_risk = estimated_risk_per_share * adjusted_quantity
            est_col1, est_col2, est_col3, est_col4, est_col5, est_col6 = st.columns(6)
            est_col1.metric("BD stimata", bd.strftime("%Y-%m-%d"))
            est_col2.metric("Entry Type", entry_type)
            est_col3.metric("Entry stimata", f"{estimated_entry:.2f}")
            est_col4.metric("SL stimato", f"{estimated_stop:.2f}")
            est_col5.metric(f"R con mult ({effective_r_multiplier:.2f}R)", f"{effective_risk_amount:.2f}")
            est_col6.metric("Quantity", str(adjusted_quantity))
            est_col7, est_col8, est_col9 = st.columns(3)
            est_col7.metric("Risk per share", f"{estimated_risk_per_share:.2f}")
            est_col8.metric("Total initial risk", f"{total_initial_risk:.2f}")
            est_col9.metric("Target Price 2R", f"{estimated_entry + (estimated_risk_per_share * 2.0):.2f}")
            with st.expander("Dettaglio stima pre-trade"):
                st.write(str(estimated_trade.get("stop_loss_reason", "")))
    else:
        with st.container(border=True):
            st.markdown("### Entry")
            trade_error = str(console.get("trade_error") or "")
            st.warning(
                "Impossibile simulare il trade con lo strategy runner."
                + (f" Dettaglio: {trade_error}" if trade_error else "")
            )

    if analysis_row:
        summary_fields = [
            "ticker",
            "requested_screen_date",
            "effective_screen_date",
            "passed_second_screen",
            "semaforo_color",
            "blue_on",
            "blue_on_weak_count",
            "close",
            "dist_sma10_pct",
            "dist_ema21_pct",
            "dist_sma50_pct",
            "ema21_slope_pct_5",
            "sessions_since_last_h",
            "prev_day_open_close_diff_pct",
            "max_intraday_vs_adr14_multiple_prev_5d",
        ]
        available_fields = [field for field in summary_fields if field in analysis_row]
        if available_fields:
            snapshot_df = pd.DataFrame(
                [{"field": field, "value": analysis_row.get(field)} for field in available_fields]
            )
            st.markdown("### Snapshot")
            st.dataframe(snapshot_df, width="stretch", hide_index=True)

        with st.expander("Regole second screen"):
            rule_fields = [field for field in analysis_row.keys() if str(field).startswith("rule_")]
            if rule_fields:
                rules_df = pd.DataFrame(
                    [{"rule": field, "value": analysis_row.get(field)} for field in sorted(rule_fields)]
                )
                st.dataframe(rules_df, width="stretch", hide_index=True)
            else:
                st.info("Nessun dettaglio regole disponibile per questo ticker.")

    components.html(
        f"""
        <div class="tradingview-widget-container" style="height:560px;width:100%;">
          <div id="tradingview_trade_console_chart" style="height:100%;width:100%;"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
          <script type="text/javascript">
            new TradingView.widget({{
              "autosize": true,
              "symbol": "{ticker_input}",
              "interval": "D",
              "timezone": "Europe/Rome",
              "theme": "light",
              "style": "1",
              "locale": "it",
              "toolbar_bg": "#f5efe3",
              "enable_publishing": false,
              "hide_top_toolbar": false,
              "hide_legend": false,
              "save_image": false,
              "container_id": "tradingview_trade_console_chart",
              "studies": ["Volume@tv-basicstudies"]
            }});
          </script>
        </div>
        """,
        height=580,
    )
    render_second_screen_etf_context_section(
        second_screen_df.loc[second_screen_df["ticker"].astype(str).str.strip().str.upper() == str(console["ticker"]).upper()]
        if not second_screen_df.empty and "ticker" in second_screen_df.columns
        else pd.DataFrame(),
        selected_sd,
        key_prefix="trade_console",
    )



def _resolve_bd_column(df: pd.DataFrame) -> str | None:
    if df.empty:
        return None
    if "bd" in df.columns:
        return "bd"
    if "entry_date" in df.columns:
        return "entry_date"
    return None


def load_trade_state_df_for_latest_bd(state: pd.DataFrame) -> pd.DataFrame:
    latest_bd = latest_date(state, "date")
    if latest_bd is None:
        return pd.DataFrame()
    path = trade_state_path_for_date(latest_bd)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, keep_default_na=False)
    if df.empty:
        return df
    for col in ["screen_date", "buy_date", "entry_date", "bars_seen_until", "last_update_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()
    for col in ["entry_price", "initial_stop_loss", "current_stop_loss", "target_close_price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["first_take_profit_done", "second_exit_done"]:
        if col in df.columns:
            df[col] = df[col].fillna(False).astype(bool)
    if "ticker" in df.columns:
        df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    return df


def build_closed_positions_summary(positions: pd.DataFrame, actions: pd.DataFrame, state: pd.DataFrame) -> pd.DataFrame:
    if positions.empty or actions.empty:
        return pd.DataFrame()

    positions_df = positions.copy()
    actions_df = actions.copy()
    positions_bd_col = _resolve_bd_column(positions_df)
    actions_bd_col = _resolve_bd_column(actions_df)
    if positions_bd_col is None or actions_bd_col is None:
        return pd.DataFrame()

    positions_df[positions_bd_col] = pd.to_datetime(positions_df[positions_bd_col], errors="coerce").dt.normalize()
    positions_df["date"] = pd.to_datetime(positions_df["date"], errors="coerce").dt.normalize()
    actions_df[actions_bd_col] = pd.to_datetime(actions_df[actions_bd_col], errors="coerce").dt.normalize()
    actions_df["action_date"] = pd.to_datetime(actions_df["action_date"], errors="coerce").dt.normalize()
    actions_df["shares_open_after"] = pd.to_numeric(actions_df.get("shares_open_after"), errors="coerce")
    actions_df["realized_r_delta"] = pd.to_numeric(actions_df.get("realized_r_delta"), errors="coerce")
    actions_df["realized_r_cum_position"] = pd.to_numeric(actions_df.get("realized_r_cum_position"), errors="coerce")
    if "ticker" in positions_df.columns:
        positions_df["ticker"] = positions_df["ticker"].astype(str).str.strip().str.upper()
    if "ticker" in actions_df.columns:
        actions_df["ticker"] = actions_df["ticker"].astype(str).str.strip().str.upper()

    position_static = (
        positions_df.sort_values(["date", "position_id"])
        .groupby("position_id", dropna=False)
        .agg(
            ticker=("ticker", "last"),
            bd=(positions_bd_col, "min"),
            entry_seq=("entry_seq", "last"),
            r_multiplier=("r_multiplier", "last"),
            shares_initial=("shares_initial", "max"),
        )
        .reset_index()
    )

    close_actions = actions_df.loc[
        actions_df["action_type"].astype(str).str.upper().str.startswith("SELL")
        & (actions_df["shares_open_after"].fillna(-1) == 0)
    ].copy()
    if close_actions.empty:
        return pd.DataFrame()

    close_summary = (
        close_actions.sort_values(["action_date", "action_seq", "position_id"])
        .groupby("position_id", dropna=False)
        .agg(
            close_date=("action_date", "last"),
            realized_r_total=("realized_r_cum_position", "last"),
            close_action_type=("action_type", "last"),
            close_note=("note", "last"),
        )
        .reset_index()
    )

    partial_summary = (
        actions_df.loc[actions_df["action_type"].astype(str).str.upper().eq("SELL_PARTIAL_1")]
        .groupby("position_id", dropna=False)
        .size()
        .reset_index(name="partial_count")
    )

    summary = close_summary.merge(position_static, on="position_id", how="left")
    summary = summary.merge(partial_summary, on="position_id", how="left")
    summary["partial_count"] = pd.to_numeric(summary.get("partial_count"), errors="coerce").fillna(0).astype(int)
    summary["holding_days"] = (summary["close_date"] - summary["bd"]).dt.days.add(1)
    summary["outcome"] = "breakeven"
    summary.loc[summary["realized_r_total"] > 0, "outcome"] = "win"
    summary.loc[summary["realized_r_total"] < 0, "outcome"] = "loss"

    trade_state_df = load_trade_state_df_for_latest_bd(state)
    if not trade_state_df.empty:
        trade_state_df["buy_date"] = pd.to_datetime(trade_state_df.get("buy_date"), errors="coerce").dt.normalize()
        trade_state_df = trade_state_df.sort_values(["ticker", "buy_date", "trade_id"]).copy()
        trade_state_df["bd_rank"] = trade_state_df.groupby(["ticker", "buy_date"]).cumcount() + 1
        summary = summary.sort_values(["ticker", "bd", "position_id"]).copy()
        summary["bd_rank"] = summary.groupby(["ticker", "bd"]).cumcount() + 1
        context_cols = [
            col for col in [
                "trade_id", "screen_date", "trade_status", "close_reason",
                "entry_mode", "first_take_profit_done", "second_exit_done",
            ] if col in trade_state_df.columns
        ]
        summary = summary.merge(
            trade_state_df[["ticker", "buy_date", "bd_rank", *context_cols]],
            left_on=["ticker", "bd", "bd_rank"],
            right_on=["ticker", "buy_date", "bd_rank"],
            how="left",
        )
        if "buy_date" in summary.columns:
            summary = summary.drop(columns=["buy_date"])

    etf_rows = []
    unique_screen_dates = sorted(pd.to_datetime(summary.get("screen_date"), errors="coerce").dropna().unique().tolist())
    for screen_date in unique_screen_dates:
        ctx_path = etf_context_path_for_date(pd.Timestamp(screen_date))
        if not ctx_path.exists():
            continue
        ctx_df = pd.read_csv(ctx_path, keep_default_na=False)
        if ctx_df.empty or "ticker" not in ctx_df.columns:
            continue
        ctx_df["screen_date"] = pd.to_datetime(ctx_df.get("screen_date"), errors="coerce").dt.normalize()
        ctx_df["ticker"] = ctx_df["ticker"].astype(str).str.strip().str.upper()
        etf_rows.append(ctx_df)
    if etf_rows:
        etf_df = pd.concat(etf_rows, ignore_index=True)
        keep_cols = [c for c in ["screen_date", "ticker", "recommended", "context_score", "reference_etf_percentile"] if c in etf_df.columns]
        summary = summary.merge(etf_df[keep_cols].drop_duplicates(subset=["screen_date", "ticker"]), on=["screen_date", "ticker"], how="left")

    slope_rows = []
    for screen_date in unique_screen_dates:
        archive = load_screening_day_bundle(pd.Timestamp(screen_date))
        second_df = archive.get("second_passed_df")
        if second_df is None or second_df.empty or "ticker" not in second_df.columns:
            continue
        second_df = second_df.copy()
        second_df["ticker"] = second_df["ticker"].astype(str).str.strip().str.upper()
        second_df["screen_date"] = pd.Timestamp(screen_date)
        cols = [c for c in ["screen_date", "ticker", "ema21_slope_pct_5", "second_screen_rank"] if c in second_df.columns]
        slope_rows.append(second_df[cols])
    if slope_rows:
        slope_df = pd.concat(slope_rows, ignore_index=True)
        summary = summary.merge(slope_df.drop_duplicates(subset=["screen_date", "ticker"]), on=["screen_date", "ticker"], how="left")

    summary = summary.sort_values(["bd", "close_date", "ticker", "position_id"]).reset_index(drop=True)
    return summary


def _summarize_closed_trades(closed_df: pd.DataFrame) -> dict[str, object]:
    if closed_df.empty:
        return {
            "trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "win_rate": None,
            "realized_r_total": 0.0, "expectancy_r": None, "avg_win_r": None, "avg_loss_r": None,
            "best_trade_r": None, "worst_trade_r": None, "avg_holding_days": None,
        }
    wins = closed_df.loc[closed_df["outcome"] == "win"]
    losses = closed_df.loc[closed_df["outcome"] == "loss"]
    breakeven = closed_df.loc[closed_df["outcome"] == "breakeven"]
    decision_count = len(wins) + len(losses)
    return {
        "trades": len(closed_df),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": (len(wins) / decision_count) if decision_count else None,
        "realized_r_total": float(pd.to_numeric(closed_df["realized_r_total"], errors="coerce").fillna(0).sum()),
        "expectancy_r": float(pd.to_numeric(closed_df["realized_r_total"], errors="coerce").fillna(0).mean()) if len(closed_df) else None,
        "avg_win_r": float(pd.to_numeric(wins["realized_r_total"], errors="coerce").mean()) if len(wins) else None,
        "avg_loss_r": float(pd.to_numeric(losses["realized_r_total"], errors="coerce").mean()) if len(losses) else None,
        "best_trade_r": float(pd.to_numeric(closed_df["realized_r_total"], errors="coerce").max()) if len(closed_df) else None,
        "worst_trade_r": float(pd.to_numeric(closed_df["realized_r_total"], errors="coerce").min()) if len(closed_df) else None,
        "avg_holding_days": float(pd.to_numeric(closed_df["holding_days"], errors="coerce").mean()) if len(closed_df) else None,
    }


def render_closed_trade_summary(summary: dict[str, object], *, prefix: str = "") -> None:
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric(f"{prefix}Trade chiusi".strip(), int(summary["trades"]))
    col2.metric("Win rate", "N/D" if summary["win_rate"] is None else f"{summary['win_rate'] * 100:.2f}%")
    col3.metric("Realized R", f"{summary['realized_r_total']:.4f}")
    col4.metric("Expectancy R", "N/D" if summary["expectancy_r"] is None else f"{summary['expectancy_r']:.4f}")
    col5.metric("Avg Holding", "N/D" if summary["avg_holding_days"] is None else f"{summary['avg_holding_days']:.1f}")
    col6, col7, col8, col9 = st.columns(4)
    col6.metric("Wins", int(summary["wins"]))
    col7.metric("Losses", int(summary["losses"]))
    col8.metric("Best R", "N/D" if summary["best_trade_r"] is None else f"{summary['best_trade_r']:.4f}")
    col9.metric("Worst R", "N/D" if summary["worst_trade_r"] is None else f"{summary['worst_trade_r']:.4f}")


def render_month_tab(closed_df: pd.DataFrame) -> None:
    st.subheader("Mese")
    st.caption("Trade chiusi attribuiti alla BD del trade.")
    if closed_df.empty:
        st.warning("Nessun trade chiuso disponibile.")
        return
    available_months = sorted(closed_df["bd"].dropna().dt.strftime("%Y-%m").unique().tolist(), reverse=True)
    selected_month = st.selectbox("Mese BD", options=available_months, index=0, key="month_tab_month")
    scoped = closed_df.loc[closed_df["bd"].dt.strftime("%Y-%m") == selected_month].copy()
    render_closed_trade_summary(_summarize_closed_trades(scoped), prefix=f"{selected_month} ")
    st.dataframe(
        scoped[[c for c in ["bd", "close_date", "ticker", "realized_r_total", "outcome", "holding_days", "r_multiplier", "close_reason"] if c in scoped.columns]],
        width="stretch",
        hide_index=True,
    )


def render_year_tab(closed_df: pd.DataFrame) -> None:
    st.subheader("Anno")
    st.caption("Trade chiusi attribuiti alla BD del trade.")
    if closed_df.empty:
        st.warning("Nessun trade chiuso disponibile.")
        return
    available_years = sorted(closed_df["bd"].dropna().dt.year.unique().tolist(), reverse=True)
    selected_year = st.selectbox("Anno BD", options=available_years, index=0, key="year_tab_year")
    scoped = closed_df.loc[closed_df["bd"].dt.year == int(selected_year)].copy()
    render_closed_trade_summary(_summarize_closed_trades(scoped), prefix=f"{selected_year} ")
    st.dataframe(
        scoped[[c for c in ["bd", "close_date", "ticker", "realized_r_total", "outcome", "holding_days", "r_multiplier", "close_reason"] if c in scoped.columns]],
        width="stretch",
        hide_index=True,
    )


def render_consuntivo_tab(closed_df: pd.DataFrame) -> None:
    st.subheader("Consuntivo")
    st.caption("Aggregato mensile dei trade chiusi, per mese di BD.")
    if closed_df.empty:
        st.warning("Nessun trade chiuso disponibile.")
        return
    working = closed_df.copy()
    working["month"] = working["bd"].dt.strftime("%Y-%m")
    rows = []
    for month, group in working.groupby("month", dropna=False):
        summary = _summarize_closed_trades(group)
        rows.append({
            "month": month,
            **summary,
        })
    cons_df = pd.DataFrame(rows).sort_values("month", ascending=False).reset_index(drop=True)
    st.dataframe(cons_df, width="stretch", hide_index=True)


def render_balance_tab(state: pd.DataFrame) -> None:
    st.subheader("Balance")
    st.caption("Curva portfolio in R sulla timeline BD.")
    if state.empty:
        st.warning("Nessun portfolio_state_daily.csv disponibile.")
        return
    working = state.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()
    for col in ["equity_mtm_r", "realized_r_cum", "unrealized_r", "drawdown_mtm_r", "open_positions_count"]:
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce")
    latest = working.sort_values("date").iloc[-1]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Ultima BD", latest["date"].strftime("%Y-%m-%d") if pd.notna(latest["date"]) else "N/D")
    col2.metric("Equity MTM R", f"{latest.get('equity_mtm_r', 0.0):.4f}")
    col3.metric("Realized R", f"{latest.get('realized_r_cum', 0.0):.4f}")
    col4.metric("Drawdown MTM R", f"{latest.get('drawdown_mtm_r', 0.0):.4f}")
    st.line_chart(working.set_index("date")[[c for c in ["equity_mtm_r", "realized_r_cum", "drawdown_mtm_r"] if c in working.columns]])
    st.dataframe(working.tail(30), width="stretch", hide_index=True)


def render_entry_context_tab(closed_df: pd.DataFrame) -> None:
    st.subheader("Entry Context")
    st.caption("Trade chiusi, classificazione causale del contesto su screen date.")
    if closed_df.empty:
        st.warning("Nessun trade chiuso disponibile.")
        return
    metric_option = st.selectbox(
        "Metrica contesto",
        options=[opt for opt in ["recommended", "context_score", "reference_etf_percentile", "ema21_slope_pct_5"] if opt in closed_df.columns],
        index=0,
        key="entry_context_metric",
    )
    scoped = closed_df.loc[closed_df[metric_option].notna()].copy()
    if scoped.empty:
        st.info("Nessun dato disponibile per la metrica selezionata.")
        return
    if metric_option == "recommended":
        scoped["bucket"] = scoped[metric_option].astype(str)
    else:
        scoped["bucket"] = pd.qcut(pd.to_numeric(scoped[metric_option], errors="coerce"), q=min(4, scoped[metric_option].nunique()), duplicates="drop").astype(str)
    rows = []
    for bucket, group in scoped.groupby("bucket", dropna=False):
        summary = _summarize_closed_trades(group)
        rows.append({"bucket": bucket, **summary})
    bucket_df = pd.DataFrame(rows).sort_values("bucket").reset_index(drop=True)
    st.dataframe(bucket_df, width="stretch", hide_index=True)
    st.dataframe(
        scoped[[c for c in ["screen_date", "bd", "close_date", "ticker", metric_option, "realized_r_total", "outcome", "close_reason"] if c in scoped.columns]],
        width="stretch",
        hide_index=True,
    )

def main() -> None:
    configure_page()
    state, positions, actions, momentum = load_live_data()
    render_header(state)

    closed_df = build_closed_positions_summary(positions, actions, state)

    overview_tab, market_tab, first_screen_tab, second_screen_tab, portfolio_tab, month_tab, year_tab, consuntivo_tab, balance_tab, entry_context_tab, operations_tab, trade_console_tab = st.tabs(
        ["Overview", "Market", "First Screen", "Second Screen", "Portfolio", "Mese", "Anno", "Consuntivo", "Balance", "Entry Context", "Operazioni", "Trade Console"]
    )
    with overview_tab:
        render_overview_tab(state)
    with market_tab:
        render_market_tab(state, momentum)
    with first_screen_tab:
        render_first_screen_tab()
    with second_screen_tab:
        render_second_screen_tab()
    with portfolio_tab:
        render_portfolio_tab(state, positions, actions)
    with month_tab:
        render_month_tab(closed_df)
    with year_tab:
        render_year_tab(closed_df)
    with consuntivo_tab:
        render_consuntivo_tab(closed_df)
    with balance_tab:
        render_balance_tab(state)
    with entry_context_tab:
        render_entry_context_tab(closed_df)
    with operations_tab:
        render_operations_tab(actions)
    with trade_console_tab:
        render_trade_console_tab()


if __name__ == "__main__":
    main()
