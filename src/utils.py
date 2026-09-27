def send_critical_failure_alert(message: str) -> None:
    """Logs and dispatches critical failure alerts."""
    logging.error(f"\033[91;1m[CRITICAL ALERT]:/033[0m {message}")
