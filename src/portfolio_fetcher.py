import os
import logging
import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

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
    """
    Syncs market scan analysis, raw data metrics, and live Angel One broker holdings 
    into Google Sheets while preserving existing data during off-market hours or empty scans.
    """
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

    # 1. Update 'Market Scan' Tab (Only if new processed data exists)
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
    else:
        logger.info(f"Skipped updating tab '{main_tab_name}' (Preserved existing data).")

    # 2. Update 'Raw Data' Tab (Only if new raw metrics exist)
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
    else:
        logger.info(f"Skipped updating tab '{raw_tab_name}' (Preserved existing data).")

    # 3. Update 'Wallet_and_Holdings' Tab (Always update live broker capital/positions)
    if real_portfolio is not None:
        try:
            try:
                wallet_ws = sheet.worksheet(wallet_tab_name)
            except gspread.exceptions.WorksheetNotFound:
                wallet_ws = sheet.add_worksheet(title=wallet_tab_name, rows=100, cols=10)

            wallet_ws.clear()

            available_cash = real_portfolio.get("available_cash", 0.0)
            holdings = real_portfolio.get("holdings", [])
            invested_value = sum(item["qty"] * item["avg_price"] for item in holdings)

            wallet_rows = [
                ["REAL BROKER PORTFOLIO (ANGEL ONE)", ""],
                ["Available Cash / RMS Net (INR)", available_cash],
                ["Total Invested Capital (INR)", invested_value],
                [],
                ["ACTIVE HOLDINGS"],
                ["TICKER", "QUANTITY", "AVG PRICE (INR)", "CURRENT PRICE (INR)", "P&L (INR)"]
            ]

            if holdings:
                for h in holdings:
                    wallet_rows.append([
                        h.get("ticker", ""),
                        h.get("qty", 0),
                        h.get("avg_price", 0.0),
                        h.get("current_price", 0.0),
                        h.get("pnl", 0.0)
                    ])
            else:
                wallet_rows.append(["No active holdings in Angel One account.", "", "", "", ""])

            wallet_ws.update(range_name="A1", values=wallet_rows)
            logger.info(f"Updated tab: '{wallet_tab_name}' with real broker metrics.")

        except Exception as e:
            logger.error(f"Failed updating tab '{wallet_tab_name}': {e}")
