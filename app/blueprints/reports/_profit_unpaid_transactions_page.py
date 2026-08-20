"""Current payables report.

The legacy page rendered one PendingBill row per bill.  This route intentionally
uses the shared financial-ledger projection and paginates only after clients
have been consolidated, so payments and partial payments cannot be hidden by a
row-level filter.
"""
from ._common import *  # noqa

import csv
from datetime import datetime
from io import StringIO

from app.services.financial_ledgers import (
    build_client_financial_ledger,
    build_current_payables,
)


def _payable_filters():
    status = (request.args.get("status") or "outstanding").strip().lower()
    if status in {"unpaid", "due", "debit"}:
        status = "outstanding"
    if status not in {"outstanding", "all", "settled", "credit"}:
        status = "outstanding"

    client_id = (request.args.get("client_id") or "").strip()
    client_filter = (request.args.get("client") or request.args.get("client_name") or "").strip()
    if client_id.isdigit():
        selected = db.session.get(Client, int(client_id))
        if selected:
            client_filter = selected.code or selected.name
    operator = (request.args.get("amount_operator") or request.args.get("amount_op") or "").strip().lower()
    amount_min = (request.args.get("amount_min") or request.args.get("min_amount") or "").strip()
    amount_max = (request.args.get("amount_max") or request.args.get("max_amount") or "").strip()
    exact_amount = (request.args.get("exact_amount") or request.args.get("amount_exact") or "").strip()

    return {
        "client_id": client_id,
        "client": client_filter,
        "amount_operator": operator,
        "amount_min": amount_min,
        "amount_max": amount_max,
        "exact_amount": exact_amount,
        "start_date": (request.args.get("start_date") or request.args.get("date_from") or "").strip(),
        "end_date": (request.args.get("end_date") or request.args.get("date_to") or "").strip(),
        "status": status,
    }


def _payable_report(filters, *, page=1, per_page=25):
    return build_current_payables(
        client_filter=filters.get("client") or "",
        amount_operator=filters.get("amount_operator") or "",
        amount_min=filters.get("amount_min") or None,
        amount_max=filters.get("amount_max") or None,
        exact_amount=filters.get("exact_amount") or None,
        start_date=filters.get("start_date") or None,
        end_date=filters.get("end_date") or None,
        status=filters.get("status") or "outstanding",
        page=page,
        per_page=per_page,
    )


def _payable_row_json(row):
    return {
        "id": row.get("id"),
        "client_id": row.get("client_id"),
        "client_name": row.get("client_name") or row.get("name") or "",
        "client_code": row.get("client_code") or row.get("code") or "",
        "outstanding": float(row.get("outstanding") or 0),
        "balance": float(row.get("balance") or 0),
        "last_transaction_date": row["last_transaction_date"].isoformat() if row.get("last_transaction_date") else None,
        "last_payment_date": row["last_payment_date"].isoformat() if row.get("last_payment_date") else None,
        "status": row.get("status") or "Outstanding",
    }


@bp.route("/unpaid_transactions")
@bp.route("/current_payables")
@login_required
def unpaid_transactions_page():
    filters = _payable_filters()
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    per_page = request.args.get("per_page", 25, type=int) or 25
    per_page = min(max(per_page, 10), 100)
    report = _payable_report(filters, page=page, per_page=per_page)
    clients = Client.query.filter(Client.is_active == True).order_by(Client.name.asc(), Client.id.asc()).all()
    selected_client = None
    if filters["client_id"].isdigit():
        selected_client = db.session.get(Client, int(filters["client_id"]))
    elif filters["client"]:
        selected_client = get_client_by_input(filters["client"])

    return render_template(
        "unpaid_transactions.html",
        rows=report["rows"],
        transactions=report["rows"],  # compatibility for small extensions/templates
        clients=clients,
        selected_client=selected_client,
        filters=filters,
        total_outstanding=report["total_outstanding"],
        total_records=report["total"],
        page=report["page"],
        per_page=report["per_page"],
        total_pages=report["pages"],
        date_filter_semantics=(
            "Date filters use each client's last contributing transaction date; "
            "the displayed amount remains the complete current balance."
        ),
    )


@bp.route("/api/current_payables")
@login_required
def current_payables_api():
    filters = _payable_filters()
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    per_page = min(max(request.args.get("per_page", 25, type=int) or 25, 1), 200)
    report = _payable_report(filters, page=page, per_page=per_page)
    response = jsonify({
        "ok": True,
        "rows": [_payable_row_json(row) for row in report["rows"]],
        "total_outstanding": report["total_outstanding"],
        "total_records": report["total"],
        "page": report["page"],
        "per_page": report["per_page"],
        "total_pages": report["pages"],
        "filters": report["filters"],
        "date_filter_semantics": "last_transaction_date",
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@bp.route("/api/current_payables/<int:client_id>")
@login_required
def current_payable_detail_api(client_id):
    client = db.session.get(Client, client_id)
    if not client:
        return jsonify({"ok": False, "error": "Client not found"}), 404
    ledger = build_client_financial_ledger(client)
    rows = []
    for row in ledger["rows"]:
        rows.append({
            "date": row["date"].isoformat() if row.get("date") and row["date"] != datetime.min else None,
            "type": row.get("type"),
            "reference": row.get("reference") or row.get("ref"),
            "description": row.get("description"),
            "debit": row.get("debit", 0),
            "credit": row.get("credit", 0),
            "balance": row.get("balance", 0),
            "source_type": row.get("source_type"),
            "source_id": row.get("source_id"),
            "note": row.get("note") or "",
            "account": row.get("account") or "",
        })
    return jsonify({
        "ok": True,
        "client": {"id": client.id, "name": client.name, "code": client.code},
        "opening_balance": float(client.opening_balance or 0),
        "total_debit": ledger["total_debit"],
        "total_credit": ledger["total_credit"],
        "closing_balance": ledger["closing_balance"],
        "status": ledger["status"],
        "rows": rows,
    })


@bp.route("/export_current_payables")
@bp.route("/export_unpaid_transactions")
@login_required
def export_current_payables():
    """Export the complete filtered grouped dataset, never just the visible page."""
    filters = _payable_filters()
    report = _payable_report(filters, page=1, per_page=200)
    # build_current_payables caps a page at 200; use all_rows so large exports
    # are still complete and independent of pagination.
    rows = report["all_rows"]
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "Client", "Account/Code", "Outstanding", "Last Transaction",
        "Last Payment", "Status",
    ])
    for row in rows:
        writer.writerow([
            row.get("client_name") or row.get("name") or "",
            row.get("client_code") or row.get("code") or "",
            f"{float(row.get('outstanding') or 0):.2f}",
            row["last_transaction_date"].strftime("%Y-%m-%d %H:%M") if row.get("last_transaction_date") else "",
            row["last_payment_date"].strftime("%Y-%m-%d %H:%M") if row.get("last_payment_date") else "",
            row.get("status") or "",
        ])
    writer.writerow([])
    writer.writerow(["TOTAL OUTSTANDING", "", f"{report['total_outstanding']:.2f}"])
    response = Response(output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = "attachment; filename=current-payables.csv"
    return response
