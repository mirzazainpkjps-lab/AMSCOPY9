"""Lightweight validation checks for ERP ledger consistency repairs."""

import argparse
import json
import sqlite3


def rows(con, sql, params=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="instance/ahmed_cement.db")
    parser.add_argument("--bill-no", default="MB NO.7783")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    bill_no = args.bill_no
    report = {
        "bill_no": bill_no,
        "sales": rows(
            con,
            """
            SELECT id, client_name, category, amount, paid_amount, discount,
                   manual_bill_no, auto_bill_no, is_void
            FROM direct_sale
            WHERE manual_bill_no = ? OR auto_bill_no = ?
            """,
            (bill_no, bill_no),
        ),
        "active_entries": rows(
            con,
            """
            SELECT id, date, type, material, booked_material, qty, bill_no,
                   nimbus_no, client_category, is_void, source_module, source_id
            FROM entry
            WHERE bill_no = ? AND COALESCE(is_void, 0) = 0
            ORDER BY id
            """,
            (bill_no,),
        ),
        "active_pending": rows(
            con,
            """
            SELECT id, client_code, client_name, bill_no, amount, reason,
                   is_paid, is_void, source_module, source_id
            FROM pending_bill
            WHERE bill_no = ? AND COALESCE(is_void, 0) = 0
            ORDER BY id
            """,
            (bill_no,),
        ),
        "stock_mismatches": rows(
            con,
            """
            WITH ledger AS (
                SELECT material,
                       SUM(CASE
                             WHEN UPPER(COALESCE(type,'')) = 'IN' THEN COALESCE(qty,0)
                             WHEN UPPER(COALESCE(type,'')) = 'OUT' THEN -COALESCE(qty,0)
                             ELSE 0
                           END) AS expected_total
                FROM entry
                WHERE COALESCE(is_void, 0) = 0
                GROUP BY material
            )
            SELECT m.id, m.name, COALESCE(m.total,0) AS stored_total,
                   COALESCE(l.expected_total,0) AS expected_total
            FROM material m
            LEFT JOIN ledger l ON l.material = m.name
            WHERE ABS(COALESCE(m.total,0) - COALESCE(l.expected_total,0)) > 0.01
            ORDER BY m.name
            LIMIT 25
            """,
        ),
        "active_direct_sale_pending_duplicates": rows(
            con,
            """
            SELECT source_module, source_id, bill_no, COUNT(*) AS row_count
            FROM pending_bill
            WHERE COALESCE(is_void,0)=0
              AND LOWER(COALESCE(reason,'')) LIKE 'direct sale%'
            GROUP BY source_module, source_id, bill_no
            HAVING COUNT(*) > 1
            LIMIT 25
            """,
        ),
    }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
