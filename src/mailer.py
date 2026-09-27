import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from logzero import logger

load_dotenv()

def send_email_alert(subject: str, body: str) -> bool:
    """Dispatches email notification using SMTP configuration from .env."""
    server_host = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", 587))
    sender_email = os.getenv("SMTP_SENDER_EMAIL")
    password = os.getenv("SMTP_PASSWORD")
    receiver_email = os.getenv("SMTP_RECEIVER_EMAIL")

    if not all([sender_email, password, receiver_email]):
        logger.error("SMTP credentials missing in .env")
        return False

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg["Subject"] = f"[A.S.T.R.A. Advisory] {subject}"
    msg.attach(MIMEText(body, "plain"))

    try:
        server = smtplib.SMTP(server_host, port)
        server.starttls()
        server.login(sender_email, password)
        server.send_message(msg)
        server.quit()
        logger.info(f"Email alert sent successfully: {subject}")
        return True
    except Exception as e:
        logger.exception(f"Failed to send email alert: {e}")
        return False

def send_eod_email_report(start_cash: float, end_cash: float, transactions: list) -> bool:
    """Generates and dispatches EOD financial summary."""
    pnl = end_cash - start_cash
    pnl_pct = (pnl / start_cash * 100) if start_cash > 0 else 0.0
    status = "PROFIT" if pnl >= 0 else "LOSS"

    lines = [
        "==========================================",
        "       ASTRA DAILY EOD SUMMARY REPORT     ",
        "==========================================",
        f"Start Balance  : ₹{start_cash:,.2f}",
        f"End Balance    : ₹{end_cash:,.2f}",
        f"Net P/L        : ₹{pnl:,.2f} ({pnl_pct:+.2f}%) [{status}]",
        "==========================================\n",
        "TRANSACTIONS EXECUTED TODAY:"
    ]

    if not transactions:
        lines.append("• No trades or transactions executed today.")
    else:
        for tx in transactions:
            lines.append(f"• {tx.get('time', 'N/A')} | {tx.get('action', '')} {tx.get('ticker', '')} | Qty: {tx.get('qty', 0)} @ ₹{tx.get('price', 0.0)} | Total: ₹{tx.get('total', 0.0)}")

    body = "\n".join(lines)
    return send_email_alert(subject=f"EOD Performance Summary - Net PnL: {pnl_pct:+.2f}%", body=body)
