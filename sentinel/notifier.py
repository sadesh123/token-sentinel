import subprocess
import logging
from pathlib import Path

PROMPT_STATUS_FILE = Path.home() / ".token-sentinel" / "prompt-status.txt"
STALE_AFTER_MINUTES = 10

logger = logging.getLogger("sentinel.notifier")


def send_toast(title: str, body: str) -> None:
    """Fire a Windows toast notification from WSL via powershell.exe."""
    # Single-quote escaping for PowerShell: '' inside single-quoted strings
    t = title.replace("'", "''")
    b = body.replace("'", "''")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
        "$tmpl=[Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$xml=[xml]$tmpl.GetXml();"
        f"$xml.GetElementsByTagName('text')[0].AppendChild($xml.CreateTextNode('{t}'))|Out-Null;"
        f"$xml.GetElementsByTagName('text')[1].AppendChild($xml.CreateTextNode('{b}'))|Out-Null;"
        "$doc=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$doc.LoadXml($xml.OuterXml);"
        "$mgr=[Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier('Token Sentinel');"
        "$mgr.Show((New-Object Windows.UI.Notifications.ToastNotification($doc)))"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            timeout=8,
        )
        if result.returncode != 0:
            logger.warning(f"Toast failed: {result.stderr.decode(errors='replace')[:200]}")
    except Exception as e:
        logger.warning(f"Toast error: {e}")


def update_prompt_status(today_cost: float, sessions_today: int, budget_usd: float) -> None:
    """Write a single-line status to ~/.token-sentinel/prompt-status.txt."""
    def fmt_usd(v: float) -> str:
        if v >= 10:
            return f"${v:.2f}"
        if v >= 0.01:
            return f"${v:.3f}"
        return f"${v:.4f}"

    pct = int((today_cost / budget_usd) * 100) if budget_usd > 0 else 0
    line = f"{fmt_usd(today_cost)} | {sessions_today} sess | {pct}%"

    PROMPT_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_STATUS_FILE.write_text(line)
