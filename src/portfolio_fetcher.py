"""
ASTRA Portfolio Fetcher & Google Sheets Synchronization
========================================================

This module handles ASTRA's Google Sheets integration for portfolio,
market-analysis, raw-data, wallet, and SIP telemetry.

Responsibilities:
    - Synchronize Market Scan results with Google Sheets.
    - Synchronize raw market-analysis data.
    - Synchronize live Angel One wallet and holdings data.
    - Preserve manually maintained holding information such as buy price
      and quantity where applicable.
    - Synchronize the current SIP state into the dedicated `SIP` tab.
    - Append SIP decision telemetry to the `SIP_Decisions` tab without
      destructively overwriting historical decision records.
    - Retrieve recorded cost prices from the Wallet_and_Holdings tab.

Google Sheets is used as ASTRA's human-readable dashboard and telemetry
layer. Persistent system state and authoritative trading/SIP state remain
outside this module.

The module is intentionally designed to perform non-destructive updates
where historical information must be preserved, particularly for:
    - Wallet_and_Holdings
    - SIP_Decisions

Configuration architecture:
    - settings.json -> non-secret runtime Google Sheets behavior.
    - .env / caller -> Google service-account credentials and spreadsheet ID.
    - This module never copies secrets into settings.json.

Author:
    ASTRA Project

Version:
    V3.x
"""

import os
import json
import logging
import datetime as dt
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

from utils import get_setting


logger = logging.getLogger("ASTRA_PORTFOLIO_FETCHER")


# ============================================================================
# RUNTIME CONFIGURATION
# ============================================================================
#
# Google service-account credentials and the spreadsheet ID are infrastructure
# values and therefore remain outside settings.json.
#
# Non-secret dashboard behavior, such as worksheet names, may be overridden
# through settings.json.
#
# Missing settings intentionally fall back to the existing ASTRA worksheet
# names so this module remains backward compatible.
# ============================================================================

DEFAULT_MAIN_TAB = "Market Scan"
DEFAULT_RAW_TAB = "Raw Data"
DEFAULT_WALLET_TAB = "Wallet_and_Holdings"
DEFAULT_SIP_TAB = "SIP"
DEFAULT_SIP_DECISIONS_TAB = "SIP_Decisions"


def _setting_str(path: str, default: str) -> str:
    """
    Read a non-secret string setting with a safe fallback.
    """

    value = get_setting(
        path,
        default,
    )

    if value is None:
        return default

    value = str(value).strip()

    return value or default


def _resolve_tab_name(
    path: str,
    supplied: Optional[str],
    default: str,
) -> str:
    """
    Resolve a worksheet name.

    Explicit function arguments take priority over settings.json.
    """

    if supplied is not None and str(supplied).strip():
        return str(supplied).strip()

    return _setting_str(
        path,
        default,
    )


# ============================================================================
# INTERNAL HELPERS
# ============================================================================

def _serialize_sheet_value(
    value: Any,
) -> Any:
    """
    Convert Python/SIP-engine values into Google-Sheets-safe values.

    Handles:
        - None
        - datetime/date/time
        - Enum
        - dataclasses
        - dict/list/tuple/set
        - primitive values
    """

    if value is None:
        return ""

    if isinstance(value, Enum):
        return value.value

    if isinstance(
        value,
        (
            dt.datetime,
            dt.date,
            dt.time,
        ),
    ):
        return value.isoformat()

    if is_dataclass(value):
        return _serialize_sheet_value(
            asdict(value)
        )

    if isinstance(value, dict):
        try:
            return json.dumps(
                {
                    str(key): _serialize_sheet_value(
                        item
                    )
                    for key, item in value.items()
                },
                ensure_ascii=False,
                default=str,
            )

        except Exception:
            return str(value)

    if isinstance(
        value,
        (
            list,
            tuple,
            set,
        ),
    ):
        try:
            return ", ".join(
                str(
                    _serialize_sheet_value(
                        item
                    )
                )
                for item in value
            )

        except Exception:
            return str(value)

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"

    if isinstance(
        value,
        (
            str,
            int,
            float,
        ),
    ):
        return value

    return str(value)


def _normalize_records(
    records: Optional[list],
) -> List[Dict[str, Any]]:
    """
    Normalize a collection of dictionaries/dataclasses into dictionaries.

    This keeps the Google Sheets layer independent from the exact internal
    SIP engine representation.
    """

    if not records:
        return []

    normalized = []

    for record in records:

        try:

            if is_dataclass(record):
                record = asdict(record)

            elif (
                hasattr(
                    record,
                    "to_dict",
                )
                and callable(record.to_dict)
            ):
                record = record.to_dict()

            elif not isinstance(
                record,
                dict,
            ):
                record = vars(record)

            if not isinstance(
                record,
                dict,
            ):
                logger.warning(
                    "Skipping unsupported sheet record type: %s",
                    type(record).__name__,
                )

                continue

            normalized.append(record)

        except Exception as exc:

            logger.warning(
                "Could not normalize sheet record: %s",
                exc,
            )

    return normalized


def _records_to_rows(
    records: List[Dict[str, Any]],
    preferred_headers: Optional[List[str]] = None,
) -> List[List[Any]]:
    """
    Convert dictionaries into a rectangular Google Sheets table.

    preferred_headers are used first, followed by any additional fields
    encountered in the records.
    """

    if not records:
        return []

    headers: List[str] = []

    if preferred_headers:
        headers.extend(
            preferred_headers
        )

    for record in records:

        for key in record.keys():

            key = str(key)

            if key not in headers:
                headers.append(key)

    rows: List[List[Any]] = [
        headers
    ]

    for record in records:

        row = []

        for header in headers:

            row.append(
                _serialize_sheet_value(
                    record.get(
                        header,
                        "",
                    )
                )
            )

        rows.append(row)

    return rows


def _get_or_create_worksheet(
    spreadsheet,
    title: str,
    rows: int = 100,
    cols: int = 20,
):
    """
    Return an existing worksheet or create it.
    """

    try:

        return spreadsheet.worksheet(
            title
        )

    except gspread.exceptions.WorksheetNotFound:

        return spreadsheet.add_worksheet(
            title=title,
            rows=max(
                rows,
                1,
            ),
            cols=max(
                cols,
                1,
            ),
        )


def _ensure_worksheet_capacity(
    worksheet,
    required_rows: int,
    required_cols: int,
) -> None:
    """
    Expand a worksheet when required.

    gspread generally allows updates beyond the currently visible range only
    after resizing, so make the operation explicit.
    """

    try:

        if worksheet.row_count < required_rows:

            worksheet.resize(
                rows=required_rows,
                cols=worksheet.col_count,
            )

        if worksheet.col_count < required_cols:

            worksheet.resize(
                rows=worksheet.row_count,
                cols=required_cols,
            )

    except Exception as exc:

        logger.warning(
            "Could not resize worksheet '%s': %s",
            worksheet.title,
            exc,
        )


def _make_decision_signature(
    row: List[Any],
) -> str:
    """
    Create a deterministic representation of a decision row.

    Used to avoid repeatedly appending the exact same decision telemetry
    during repeated ASTRA cycles.
    """

    try:

        return json.dumps(
            [
                _serialize_sheet_value(
                    value
                )
                for value in row
            ],
            ensure_ascii=False,
            sort_keys=False,
            default=str,
        )

    except Exception:

        return str(row)


# ============================================================================
# GOOGLE SHEETS SYNCHRONIZATION
# ============================================================================

def sync_dashboard_data(
    credentials_path: str,
    spreadsheet_id: str,
    processed_data: list,
    raw_data: list,
    real_portfolio: dict = None,
    main_tab_name: Optional[str] = None,
    raw_tab_name: Optional[str] = None,
    wallet_tab_name: Optional[str] = None,
    sip_data: Optional[list] = None,
    sip_decisions: Optional[list] = None,
    sip_tab_name: Optional[str] = None,
    sip_decisions_tab_name: Optional[str] = None,
):
    """
    Synchronize ASTRA dashboard data into Google Sheets.

    Existing tabs:
        - Market Scan
        - Raw Data
        - Wallet_and_Holdings

    SIP tabs:
        - SIP
        - SIP_Decisions

    SIP behavior:
        SIP
            Current-state dashboard. It is refreshed on each synchronization
            when sip_data is supplied.

        SIP_Decisions
            Historical decision telemetry. New decisions are appended while
            exact duplicate rows are ignored.

    SQLite remains the authoritative ASTRA state store. Google Sheets is
    treated as a human-readable dashboard and telemetry/history layer.
    """

    # ------------------------------------------------------------------------
    # Validate Google Sheets infrastructure parameters.
    # ------------------------------------------------------------------------

    if (
        not credentials_path
        or not os.path.exists(
            credentials_path
        )
    ):
        logger.error(
            "Credentials file missing: %s",
            credentials_path,
        )

        return

    if not spreadsheet_id:

        logger.error(
            "Spreadsheet ID missing."
        )

        return

    # ------------------------------------------------------------------------
    # Resolve non-secret worksheet names from settings.json.
    #
    # Explicit function arguments still win.
    # ------------------------------------------------------------------------

    main_tab_name = _resolve_tab_name(
        "GOOGLE_SHEETS.MAIN_TAB_NAME",
        main_tab_name,
        DEFAULT_MAIN_TAB,
    )

    raw_tab_name = _resolve_tab_name(
        "GOOGLE_SHEETS.RAW_TAB_NAME",
        raw_tab_name,
        DEFAULT_RAW_TAB,
    )

    wallet_tab_name = _resolve_tab_name(
        "GOOGLE_SHEETS.WALLET_TAB_NAME",
        wallet_tab_name,
        DEFAULT_WALLET_TAB,
    )

    sip_tab_name = _resolve_tab_name(
        "GOOGLE_SHEETS.SIP_TAB_NAME",
        sip_tab_name,
        DEFAULT_SIP_TAB,
    )

    sip_decisions_tab_name = _resolve_tab_name(
        "GOOGLE_SHEETS.SIP_DECISIONS_TAB_NAME",
        sip_decisions_tab_name,
        DEFAULT_SIP_DECISIONS_TAB,
    )

    # ------------------------------------------------------------------------
    # Google Sheets authentication.
    # ------------------------------------------------------------------------

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets"
    ]

    try:

        creds = Credentials.from_service_account_file(
            credentials_path,
            scopes=scopes,
        )

        client = gspread.authorize(
            creds
        )

        sheet = client.open_by_key(
            spreadsheet_id
        )

    except Exception as exc:

        logger.error(
            "Failed to authorize Google Sheets API: %s",
            exc,
        )

        return

    # ========================================================================
    # 1. MARKET SCAN
    # ========================================================================

    if (
        processed_data
        and len(processed_data) > 1
    ):

        try:

            worksheet = _get_or_create_worksheet(
                sheet,
                main_tab_name,
                rows=max(
                    100,
                    len(processed_data) + 10,
                ),
                cols=max(
                    10,
                    max(
                        len(row)
                        for row in processed_data
                        if isinstance(
                            row,
                            (
                                list,
                                tuple,
                            ),
                        )
                    ),
                ),
            )

            worksheet.clear()

            _ensure_worksheet_capacity(
                worksheet,
                required_rows=len(
                    processed_data
                ),
                required_cols=max(
                    len(row)
                    for row in processed_data
                    if isinstance(
                        row,
                        (
                            list,
                            tuple,
                        ),
                    )
                ),
            )

            worksheet.update(
                range_name="A1",
                values=processed_data,
            )

            logger.info(
                "Updated tab: '%s'",
                main_tab_name,
            )

        except Exception as exc:

            logger.error(
                "Failed updating tab '%s': %s",
                main_tab_name,
                exc,
            )

    # ========================================================================
    # 2. RAW DATA
    # ========================================================================

    if raw_data:

        try:

            raw_worksheet = _get_or_create_worksheet(
                sheet,
                raw_tab_name,
                rows=max(
                    200,
                    len(raw_data) + 10,
                ),
                cols=20,
            )

            raw_worksheet.clear()

            headers = list(
                raw_data[0].keys()
            )

            rows = [
                headers
            ]

            for item in raw_data:

                row = []

                for header in headers:

                    value = item.get(
                        header,
                        "",
                    )

                    if isinstance(
                        value,
                        list,
                    ):

                        value = (
                            ", ".join(
                                str(v)
                                for v in value
                            )
                            if value
                            else "NEUTRAL"
                        )

                    row.append(
                        _serialize_sheet_value(
                            value
                        )
                    )

                rows.append(row)

            _ensure_worksheet_capacity(
                raw_worksheet,
                required_rows=len(
                    rows
                ),
                required_cols=len(
                    headers
                ),
            )

            raw_worksheet.update(
                range_name="A1",
                values=rows,
            )

            logger.info(
                "Updated tab: '%s' with %d records.",
                raw_tab_name,
                len(raw_data),
            )

        except Exception as exc:

            logger.error(
                "Failed updating tab '%s': %s",
                raw_tab_name,
                exc,
            )

    # ========================================================================
    # 3. WALLET AND HOLDINGS
    # ========================================================================

    if real_portfolio is not None:

        try:

            wallet_ws = _get_or_create_worksheet(
                sheet,
                wallet_tab_name,
                rows=100,
                cols=9,
            )

            # ----------------------------------------------------------------
            # Preserve existing static entries:
            #     - Quantity
            #     - Buy Price
            #
            # These values are deliberately read before the sheet is cleared.
            # ----------------------------------------------------------------

            existing_map = {}

            try:

                all_vals = (
                    wallet_ws.get_all_values()
                )

                if len(all_vals) >= 7:

                    headers = [
                        str(header)
                        .strip()
                        .upper()
                        for header in all_vals[5]
                    ]

                    for row in all_vals[6:]:

                        if not row or not row[0]:
                            continue

                        row_dict = {}

                        for idx, header in enumerate(
                            headers
                        ):

                            if idx < len(row):
                                row_dict[header] = row[idx]

                        ticker_key = str(
                            row_dict.get(
                                "TICKER",
                                "",
                            )
                        ).strip().upper()

                        if ticker_key:
                            existing_map[
                                ticker_key
                            ] = row_dict

            except Exception as read_err:

                logger.warning(
                    "Could not parse existing holdings to preserve "
                    "buy prices: %s",
                    read_err,
                )

            wallet_ws.clear()

            available_cash = real_portfolio.get(
                "available_cash",
                0.0,
            )

            holdings = real_portfolio.get(
                "holdings",
                [],
            )

            wallet_rows = [
                [
                    "REAL BROKER PORTFOLIO (ANGEL ONE)",
                    "",
                ],
                [
                    "Available Cash / RMS Net (INR)",
                    available_cash,
                ],
                [
                    "Total Active Holdings",
                    len(holdings),
                ],
                [],
                [
                    "ACTIVE HOLDINGS",
                ],
                [
                    "TICKER",
                    "QUANTITY",
                    "BUY PRICE (INR)",
                    "CURRENT PRICE (INR)",
                    "TOTAL INVESTED (INR)",
                    "CURRENT VALUE (INR)",
                    "P&L AMOUNT (INR)",
                    "P&L (%)",
                ],
            ]

            if holdings:

                start_row = 7

                for idx, holding in enumerate(
                    holdings,
                    start=start_row,
                ):

                    ticker = str(
                        holding.get(
                            "ticker",
                            "",
                        )
                    ).strip().upper()

                    try:

                        curr_price = float(
                            holding.get(
                                "current_price",
                                0.0,
                            )
                        )

                    except (
                        TypeError,
                        ValueError,
                    ):

                        curr_price = 0.0

                    # --------------------------------------------------------
                    # Preserve manually recorded quantity and buy price when
                    # the ticker already exists in the dashboard.
                    # --------------------------------------------------------

                    if ticker in existing_map:

                        previous = existing_map[
                            ticker
                        ]

                        try:

                            qty = float(
                                str(
                                    previous.get(
                                        "QUANTITY",
                                        holding.get(
                                            "qty",
                                            1,
                                        ),
                                    )
                                )
                                .replace(
                                    ",",
                                    "",
                                )
                                .strip()
                            )

                        except (
                            TypeError,
                            ValueError,
                        ):

                            try:
                                qty = float(
                                    holding.get(
                                        "qty",
                                        1,
                                    )
                                )

                            except (
                                TypeError,
                                ValueError,
                            ):

                                qty = 1.0

                        try:

                            avg_price = float(
                                str(
                                    previous.get(
                                        "BUY PRICE (INR)",
                                        holding.get(
                                            "avg_price",
                                            curr_price,
                                        ),
                                    )
                                )
                                .replace(
                                    "₹",
                                    "",
                                )
                                .replace(
                                    ",",
                                    "",
                                )
                                .strip()
                            )

                        except (
                            TypeError,
                            ValueError,
                        ):

                            try:

                                avg_price = float(
                                    holding.get(
                                        "avg_price",
                                        curr_price,
                                    )
                                )

                            except (
                                TypeError,
                                ValueError,
                            ):

                                avg_price = curr_price

                    else:

                        try:

                            qty = float(
                                holding.get(
                                    "qty",
                                    1,
                                )
                            )

                        except (
                            TypeError,
                            ValueError,
                        ):

                            qty = 1.0

                        try:

                            avg_price = float(
                                holding.get(
                                    "avg_price",
                                    curr_price,
                                )
                            )

                        except (
                            TypeError,
                            ValueError,
                        ):

                            avg_price = curr_price

                    # --------------------------------------------------------
                    # Google Sheets formulas.
                    # --------------------------------------------------------

                    invested_formula = (
                        f"=B{idx}*C{idx}"
                    )

                    current_value_formula = (
                        f"=B{idx}*D{idx}"
                    )

                    pnl_amount_formula = (
                        f"=F{idx}-E{idx}"
                    )

                    pnl_pct_formula = (
                        f"=IF(E{idx}>0,"
                        f"((F{idx}-E{idx})/E{idx}),0)"
                    )

                    wallet_rows.append(
                        [
                            ticker,
                            qty,
                            avg_price,
                            curr_price,
                            invested_formula,
                            current_value_formula,
                            pnl_amount_formula,
                            pnl_pct_formula,
                        ]
                    )

            else:

                wallet_rows.append(
                    [
                        "No active holdings in Angel One account.",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ]
                )

            _ensure_worksheet_capacity(
                wallet_ws,
                required_rows=len(
                    wallet_rows
                ),
                required_cols=8,
            )

            wallet_ws.update(
                range_name="A1",
                values=wallet_rows,
                value_input_option="USER_ENTERED",
            )

            logger.info(
                "Updated tab: '%s' with real broker metrics.",
                wallet_tab_name,
            )

        except Exception as exc:

            logger.error(
                "Failed updating tab '%s': %s",
                wallet_tab_name,
                exc,
            )

    # ========================================================================
    # 4. SIP CURRENT STATE
    # ========================================================================

    if sip_data is not None:

        try:

            sip_records = _normalize_records(
                sip_data
            )

            sip_ws = _get_or_create_worksheet(
                sheet,
                sip_tab_name,
                rows=max(
                    100,
                    len(sip_records) + 10,
                ),
                cols=20,
            )

            # SIP is a CURRENT STATE dashboard.
            # Unlike SIP_Decisions, it is intentionally refreshed.

            sip_ws.clear()

            if sip_records:

                preferred_headers = [
                    "target_id",
                    "asset",
                    "amount",
                    "frequency",
                    "start_at",
                    "expires_at",
                    "status",
                    "created_at",
                    "next_execution_at",
                    "completed_contributions",
                    "total_invested",
                    "last_contribution_at",
                    "last_execution_price",
                    "last_execution_quantity",
                    "last_order_id",
                    "pnl",
                    "pnl_pct",
                ]

                sip_rows = _records_to_rows(
                    sip_records,
                    preferred_headers=preferred_headers,
                )

            else:

                sip_rows = [
                    [
                        "target_id",
                        "asset",
                        "amount",
                        "frequency",
                        "start_at",
                        "expires_at",
                        "status",
                        "created_at",
                        "next_execution_at",
                        "completed_contributions",
                        "total_invested",
                        "last_contribution_at",
                        "last_execution_price",
                        "last_execution_quantity",
                        "last_order_id",
                        "pnl",
                        "pnl_pct",
                    ],
                    [
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "NO ACTIVE SIP TARGETS",
                    ],
                ]

            _ensure_worksheet_capacity(
                sip_ws,
                required_rows=len(
                    sip_rows
                ),
                required_cols=max(
                    len(row)
                    for row in sip_rows
                ),
            )

            sip_ws.update(
                range_name="A1",
                values=sip_rows,
                value_input_option="USER_ENTERED",
            )

            logger.info(
                "Updated SIP tab '%s' with %d target records.",
                sip_tab_name,
                len(sip_records),
            )

        except Exception as exc:

            logger.error(
                "Failed updating SIP tab '%s': %s",
                sip_tab_name,
                exc,
            )

    # ========================================================================
    # 5. SIP DECISION HISTORY
    # ========================================================================

    if sip_decisions:

        try:

            decision_records = _normalize_records(
                sip_decisions
            )

            if decision_records:

                decision_ws = _get_or_create_worksheet(
                    sheet,
                    sip_decisions_tab_name,
                    rows=max(
                        200,
                        len(decision_records) + 20,
                    ),
                    cols=30,
                )

                preferred_headers = [
                    "timestamp",
                    "target_id",
                    "asset",
                    "decision",
                    "price",
                    "quantity",
                    "allocation",
                    "rsi",
                    "macd",
                    "macd_signal",
                    "macd_histogram",
                    "trend",
                    "market_condition",
                    "risk_status",
                    "capital_check",
                    "wallet_check",
                    "sip_rule",
                    "reason",
                    "confidence",
                    "metadata",
                ]

                new_rows = _records_to_rows(
                    decision_records,
                    preferred_headers=preferred_headers,
                )

                # ------------------------------------------------------------
                # SIP_Decisions is APPEND-ONLY.
                #
                # Existing decision history is retained.
                # Exact duplicate rows are ignored.
                # ------------------------------------------------------------

                existing_values = []

                try:

                    existing_values = (
                        decision_ws.get_all_values()
                    )

                except Exception as read_err:

                    logger.warning(
                        "Could not read existing SIP decision "
                        "history: %s",
                        read_err,
                    )

                if not existing_values:

                    _ensure_worksheet_capacity(
                        decision_ws,
                        required_rows=len(
                            new_rows
                        ),
                        required_cols=len(
                            new_rows[0]
                        ),
                    )

                    decision_ws.update(
                        range_name="A1",
                        values=new_rows,
                        value_input_option="USER_ENTERED",
                    )

                    logger.info(
                        "Created SIP decision history with %d records.",
                        len(decision_records),
                    )

                else:

                    existing_signatures = set()

                    # Existing header is not part of duplicate checking.

                    for existing_row in existing_values[1:]:

                        existing_signatures.add(
                            _make_decision_signature(
                                existing_row
                            )
                        )

                    rows_to_append = []

                    for row in new_rows[1:]:

                        signature = (
                            _make_decision_signature(
                                row
                            )
                        )

                        if signature in existing_signatures:
                            continue

                        existing_signatures.add(
                            signature
                        )

                        rows_to_append.append(
                            row
                        )

                    if rows_to_append:

                        _ensure_worksheet_capacity(
                            decision_ws,
                            required_rows=(
                                len(existing_values)
                                + len(rows_to_append)
                            ),
                            required_cols=max(
                                len(existing_values[0]),
                                len(new_rows[0]),
                            ),
                        )

                        decision_ws.append_rows(
                            rows_to_append,
                            value_input_option="USER_ENTERED",
                        )

                        logger.info(
                            "Appended %d new SIP decisions to '%s'.",
                            len(rows_to_append),
                            sip_decisions_tab_name,
                        )

                    else:

                        logger.info(
                            "No new SIP decisions to append to '%s'.",
                            sip_decisions_tab_name,
                        )

        except Exception as exc:

            logger.error(
                "Failed updating SIP decision tab '%s': %s",
                sip_decisions_tab_name,
                exc,
            )


# ============================================================================
# COST-PRICE LOOKUP
# ============================================================================

def get_cost_price_from_sheet(
    credentials_path: str,
    spreadsheet_id: str,
    ticker: str,
    tab_name: str = DEFAULT_WALLET_TAB,
) -> float:
    """
    Read the designated Google Sheets tab and return the recorded cost
    price for a ticker.

    Returns:
        float:
            Cost price if found, otherwise 0.0.
    """

    try:

        # When the caller uses the normal Wallet_and_Holdings default,
        # allow settings.json to override the actual worksheet name.
        if (
            not tab_name
            or tab_name == DEFAULT_WALLET_TAB
        ):

            tab_name = _setting_str(
                "GOOGLE_SHEETS.WALLET_TAB_NAME",
                DEFAULT_WALLET_TAB,
            )

        gc = gspread.service_account(
            filename=credentials_path
        )

        sh = gc.open_by_key(
            spreadsheet_id
        )

        worksheet = sh.worksheet(
            tab_name
        )

        records = worksheet.get_all_records()

        requested_ticker = (
            ticker.strip().upper()
        )

        for row in records:

            row_ticker = str(
                row.get(
                    "TICKER",
                    row.get(
                        "Ticker",
                        "",
                    ),
                )
            ).strip().upper()

            if row_ticker != requested_ticker:
                continue

            value = (
                row.get(
                    "BUY PRICE (INR)"
                )
                or row.get(
                    "Cost Price"
                )
                or row.get(
                    "Buy Price"
                )
                or row.get(
                    "Avg Price"
                )
            )

            if value:

                try:

                    return float(
                        str(value)
                        .replace(
                            "₹",
                            "",
                        )
                        .replace(
                            ",",
                            "",
                        )
                        .strip()
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    logger.warning(
                        "Invalid cost price '%s' for %s.",
                        value,
                        ticker,
                    )

    except Exception as exc:

        logger.error(
            "Error fetching cost price from Google Sheet "
            "for %s: %s",
            ticker,
            exc,
        )

    return 0.0
