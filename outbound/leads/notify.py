"""Review-notification email and queue helpers for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S12). Pure move.
"""

import argparse
import json
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from sheets_helper import get_client, open_sheet

from outbound.leads.config import NOTIFICATION_QUEUE_COLUMNS, SEARCH_TASKS_DIR
from outbound.leads.reviewtab import get_or_create_worksheet
from outbound.leads.runs import save_run, source_sheet_url


def export_pending_search_tasks(
    computation: dict[str, Any], computation_file: Path, args: argparse.Namespace
) -> tuple[Path, list[dict[str, Any]]]:
    tasks = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    tasks = tasks[: args.max_search_tasks]
    export_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_file = SEARCH_TASKS_DIR / f"{export_id}_search_tasks.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "computation_file": str(computation_file),
        "task_count": len(tasks),
        "tasks": tasks,
        "search_tasks": tasks,
    }
    task_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    computation.setdefault("search_task_exports", []).append(
        {
            "created_at": payload["created_at"],
            "file": str(task_file),
            "task_count": len(tasks),
        }
    )
    save_run(computation, computation_file)
    return task_file, tasks


def send_review_email(
    run: dict[str, Any], run_file: Path, review_tab: str, to_email: str
) -> dict[str, Any]:
    if not to_email:
        return {"sent": False, "reason": "No notification email configured."}
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("SMTP_FROM", user)
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not host or not user or not password or not from_email:
        return {
            "sent": False,
            "reason": "SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and SMTP_FROM/SMTP_USER are required.",
        }

    subject = f"Lead review ready: {run.get('source', {}).get('selected_count', 0)} leads"
    body = "\n".join(
        [
            "A new lead review queue is ready.",
            "",
            f"Run ID: {run.get('run_id', '')}",
            f"Review tab: {review_tab}",
            f"Selected leads: {run.get('source', {}).get('selected_count', 0)}",
            f"Run file: {run_file}",
            "",
            "Open the sheet, review the rows, tick Approved for leads to process, then start the approved-leads processing workflow.",
        ]
    )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = to_email
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)
    return {"sent": True, "to": to_email}


def review_email_subject(run: dict[str, Any]) -> str:
    return f"Lead review ready: {run.get('source', {}).get('selected_count', 0)} leads"


def review_email_body(run: dict[str, Any], run_file: Path, review_tab: str) -> str:
    return "\n".join(
        [
            "A new lead review queue is ready.",
            "",
            f"Run ID: {run.get('run_id', '')}",
            f"Review tab: {review_tab}",
            f"Selected leads: {run.get('source', {}).get('selected_count', 0)}",
            f"Run file: {run_file}",
            "",
            "Open the sheet, review the rows, tick Approved for leads to process, then start the approved-leads processing workflow.",
        ]
    )


def enqueue_review_notification(
    credentials_path: Path,
    sheet_url: str,
    queue_tab: str,
    run: dict[str, Any],
    run_file: Path,
    review_tab: str,
    to_email: str,
) -> dict[str, Any]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_or_create_worksheet(
        spreadsheet, queue_tab, rows=1000, cols=len(NOTIFICATION_QUEUE_COLUMNS)
    )
    headers = worksheet.row_values(1)
    if headers[: len(NOTIFICATION_QUEUE_COLUMNS)] != NOTIFICATION_QUEUE_COLUMNS:
        worksheet.update(
            range_name="A1",
            values=[NOTIFICATION_QUEUE_COLUMNS],
            value_input_option="USER_ENTERED",
        )
        try:
            worksheet.freeze(rows=1)
        except Exception:
            pass
    row = {
        "Created At": datetime.now().isoformat(timespec="seconds"),
        "Status": "Pending",
        "To": to_email,
        "Subject": review_email_subject(run),
        "Body": review_email_body(run, run_file, review_tab),
        "Run ID": run.get("run_id", ""),
        "Run File": str(run_file),
        "Sent At": "",
        "Error": "",
    }
    worksheet.append_row(
        [row.get(header, "") for header in NOTIFICATION_QUEUE_COLUMNS],
        value_input_option="USER_ENTERED",
    )
    return {"queued": True, "tab": queue_tab, "to": to_email}


def notify_review_with_fallback(
    run: dict[str, Any], run_file: Path, args: argparse.Namespace
) -> dict[str, Any]:
    try:
        notification = send_review_email(run, run_file, args.review_tab, args.notify_email)
    except Exception as exc:
        notification = {"sent": False, "reason": str(exc)}
    if not notification.get("sent") and args.queue_notification:
        try:
            queued = enqueue_review_notification(
                Path(args.credentials),
                source_sheet_url(args),
                args.notification_queue_tab,
                run,
                run_file,
                args.review_tab,
                args.notify_email,
            )
            notification["fallback_queue"] = queued
        except Exception as exc:
            notification["fallback_queue"] = {"queued": False, "reason": str(exc)}
    return notification
