import os
import logging
import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger("ASTRA_PORTFOLIO_FETCHER")


def sync_dashboard_data(
    credentials_path: str,
    spreadsheet_id: str,
    processed_data: list,
    raw_data: list,
    real_portfolio: dict = None,
    main_tab_name: str = "Market Scan",
    raw_tab_name: str = "Raw Data",
    wallet_tab_name: str = "Wallet_and_Holdings"
):
    """Syncs market scan analysis, raw data metrics, and live Angel One broker holdings into Google Sheets."""
    if not credentials_path or not os.path.exists(credentials_path):
        logger.error(f"Credentials file missing: {credentials_path}")
        return

    if not spreadsheet_id:
        logger.error("Spreadsheet ID missing.")
        return

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    
    try:
        creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(spreadsheet_id)
    except Exception as e:
        logger.error(f"Failed to authorize Google Sheets API: {e}")
        return

    # 1. Update 'Market Scan' Tab
    if processed_data and len(processed_data) > 1:
        try:
            try:
                worksheet = sheet.worksheet(main_tab_name)
            except gspread.exceptions.WorksheetNotFound:
                worksheet = sheet.add_worksheet(title=main_tab_name, rows=100, cols=10)

            worksheet.clear()
            worksheet.update(range_name="A1", values=processed_data)
            logger.info(f"Updated tab: '{main_tab_name}'")
        except Exception as e:
            logger.error(f"Failed updating tab '{main_tab_name}': {e}")

    # 2. Update 'Raw Data' Tab
    if raw_data:
        try:
            try:
                raw_worksheet = sheet.worksheet(raw_tab_name)
            except gspread.exceptions.WorksheetNotFound:
                raw_worksheet = sheet.add_worksheet(title=raw_tab_name, rows=200, cols=15)

            raw_worksheet.clear()
            headers = list(raw_data[0].keys())
            rows = [headers]

            for item in raw_data:
                row = []
                for h in headers:
                    val = item.get(h, "")
                    if isinstance(val, list):
                        val = ", ".join(str(v) for v in val) if val else "NEUTRAL"
                    row.append(val)
                rows.append(row)

            raw_worksheet.update(range_name="A1", values=rows)
            logger.info(f"Updated tab: '{raw_tab_name}' with {len(raw_data)} records.")
        except Exception as e:
            logger.error(f"Failed updating tab '{raw_tab_name}': {e}")

    # 3. Update 'Wallet_and_Holdings' Tab
    if real_portfolio is not None:
        try:
            try:
                wallet_ws = sheet.worksheet(wallet_tab_name)
            except gspread.exceptions.WorksheetNotFound:
                wallet_ws = sheet.add_worksheet(title=wallet_tab_name, rows=100, cols=10)

            wallet_ws.clear()

            available_cash = real_portfolio.get("available_cash", 0.0)
            holdings = real_portfolio.get("holdings", [])

            wallet_rows = [
                ["REAL BROKER PORTFOLIO (ANGEL ONE)", ""],
                ["Available Cash / RMS Net (INR)", available_cash],
                ["Total Active Holdings", len(holdings)],
                [],
                ["ACTIVE HOLDINGS"],
                [
                    "TICKER",
                    "QUANTITY",
                    "BUY PRICE (INR)",
                    "CURRENT PRICE (INR)",
                    "TOTAL INVESTED (INR)",
                    "CURRENT VALUE (INR)",
                    "P&L AMOUNT (INR)",
                    "P&L (%)",
                    "STATUS"
                ]
            ]

            if holdings:
                start_row = 7
                for idx, h in enumerate(holdings, start=start_row):
                    ticker = h.get("ticker", "")
                    qty = h.get("qty", 0)
                    avg_price = h.get("avg_price", 0.0)
                    curr_price = h.get("current_price", 0.0)

                    invested_formula = f"=B{idx}*C{idx}"
                    current_val_formula = f"=B{idx}*D{idx}"
                    pnl_amt_formula = f"=F{idx}-E{idx}"
                    pnl_pct_formula = f"=IF(E{idx}>0, ((F{idx}-E{idx})/E{idx})*100, 0)"

                    wallet_rows.append([
                        ticker,
                        qty,
                        avg_price,
                        curr_price,
                        invested_formula,
                        current_val_formula,
                        pnl_amt_formula,
                        pnl_pct_formula,
                        "HOLDING"
                    ])
            else:
                wallet_rows.append(["No active holdings in Angel One account.", "", "", "", "", "", "", "", ""])

            wallet_ws.update(range_name="A1", values=wallet_rows, value_input_option="USER_ENTERED")
            logger.info(f"Updated tab: '{wallet_tab_name}' with real broker metrics.")

        except Exception as e:
            logger.error(f"Failed updating tab '{wallet_tab_name}': {e}")


def get_cost_price_from_sheet(credentials_path: str, spreadsheet_id: str, ticker: str, tab_name: str = "Wallet_and_Holdings") -> float:
    """
    Reads the designated tab in Google Sheets to fetch the recorded cost price for a ticker.
    Returns 0.0 if not found or on error.
    """
    try:
        gc = gspread.service_account(filename=credentials_path)
        sh = gc.open_by_key(spreadsheet_id)
        worksheet = sh.worksheet(tab_name)
        
        records = worksheet.get_all_records()
        for row in records:
            # Check matching ticker column
            if str(row.get("Ticker", "")).strip().upper() == ticker.strip().upper():
                val = row.get("Cost Price") or row.get("Buy Price") or row.get("Avg Price")
                if val:
                    return float(str(val).replace("₹", "").replace(",", "").strip())
    except Exception as e:
        print(f"Error fetching cost price from Google Sheet for {ticker}: {e}")
    
    return 0.0
