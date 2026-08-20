# Driver / Delivery-Person Payments — Financial Architecture Audit & Design

## 1. Source-of-truth audit (answers to §24)

| Question | Finding (before this change) |
| --- | --- |
| Authoritative model for financial transactions? | `AccountTransaction` (immutable, voided never deleted). Party documents (`Payment`, `SupplierPayment`) are *source rows*; each syncs exactly one linked `AccountTransaction` via `app/services/accounting.py`. |
| Authoritative model for account balances? | `Account.balance_minor` (paisa) mirrored to `Account.balance`. Recomputable via `payments_crud.ledger_balance()` = `opening_balance + Σ money-in − Σ money-out` over non-void `AccountTransaction`. |
| Authoritative model for driver balances? | None stored. `DeliveryPerson.opening_balance` + derived rows from `SaleDeliveryPerson.rent_amount` (debit) and `DeliveryPersonPayment` (credit), projected by `financial_ledgers._delivery_person_rows()`. **Calculated, not stored — good.** |
| Balances calculated or stored? | Account = stored + reconcilable. Driver = purely calculated. |
| Could one payment update two balances independently? | **Yes — the bug.** `DeliveryPersonPayment` had *no* account link and created *no* `AccountTransaction`. Driver ledger moved; cash/bank never did. |
| Duplicate transactions possible? | Yes — no idempotency key on driver payments (client/supplier payments already had `idempotency_key` + partial unique index). |
| Payment without an account? | Yes — driver payments had no `payment_account_id` column at all. |
| Account transaction without its source? | No — every synced tx carries `source_type`/`source_id` + `[SRC:Kind:id]` note marker. |
| Driver ledger entry without a financial transaction? | Yes (all of them). Fixed for new payments; legacy rows preserved and *reported*, never silently rewritten. |

## 2. Accounting model actually used → CASE B (payable)

`SaleDeliveryPerson.rent_amount` records the service **when the sale happens** (driver becomes a
creditor), and `DeliveryPersonPayment` settles it later. The driver ledger therefore represents
**amounts owed to the driver**, exactly like the Supplier ledger. This meaning is preserved:
drivers are **payables**, not immediate generic expenses.

```
Delivery service rendered   → Driver payable  +rent      (debit,  no cash effect)
Payment to driver           → Driver payable  −amount    (credit) AND Cash/Bank −amount
Waive-off                   → Driver payable  −amount    (credit) AND Loss (no cash effect)
```

## 3. Chosen architecture — OPTION B, "one transaction, multiple entry points"

The Supplier pattern already implements exactly the requested design, so drivers now reuse it
rather than inventing a parallel system:

```
 Driver ▸ Ledger ▸ Pay Driver          Accounts ▸ Transaction ▸ Driver Payment       Delivery Rents ▸ Pay
              │                                        │                                    │
              └────────────────────────┬───────────────┴────────────────────────────────────┘
                                       ▼
                      app/services/driver_payments.py   (SINGLE CORE)
                      save_driver_payment / delete_ / restore_
                                       │
                    ┌──────────────────┼─────────────────────┐
                    ▼                  ▼                     ▼
        DeliveryPersonPayment   AccountTransaction     AccountingAuditLog
        (party/source row)      type='Driver Payment'  (before/after JSON)
                    │            source_type=          
                    │            'DeliveryPersonPayment'
                    ▼                  ▼
            Driver ledger        Account balance, Account ledger,
            (calculated)         Cash Flow, Daily breakdown, Audit trail
```

`DeliveryPersonPayment` is the **source document** (who/why/which rent allocation);
`AccountTransaction` is the **single authoritative financial movement**. They are 1:1 and
kept consistent by `_sync_delivery_person_payment_accounting()` — the same
create/void/recreate discipline used for client and supplier payments, so an edit applies the
net delta once and can never double-count.

## 4. What changed

* `DeliveryPersonPayment` gained: `payment_account_id` (FK), `method`, `amount_paid_minor`,
  `waive_off_minor`, `reference`, `idempotency_key` (partial unique index), `revision`,
  `updated_by`, `updated_at`. Additive columns only — no data wipe, legacy rows keep working.
* New `app/services/driver_payments.py`: validation, insufficient-balance check, reconciled-period
  guard, atomic linkage, audit event, idempotent replay.
* New `_sync_delivery_person_payment_accounting()` in `app/services/accounting.py`:
  money-out `AccountTransaction` (`Driver Payment`) for the paid part; non-cash `Loss` row for the
  waive-off part (matches how client waive-offs are handled).
* Entry points refactored to delegate: driver ledger settlement, delivery-rent payment,
  and the new Accounts ▸ Pay ▸ *Driver Service Payment* target.
* Reports updated to include `Driver Payment`: cash-flow rows/totals, account-ledger type filter,
  audit-trail type filter, expenditure KPIs.
* `financial_integrity_audit()` extended + `reconcile_driver_payments()` added: compares each
  driver payment against its linked account transaction and flags legacy unlinked rows. It
  **reports**, it does not silently overwrite balances.
