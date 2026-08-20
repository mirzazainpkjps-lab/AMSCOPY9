"""wipe — split from misc.py."""
from ._common import *  # noqa

@bp.route('/delete_selected_data', methods=['POST'])
@login_required
def delete_selected_data():
    global _RESET_CONTEXT
    if current_user.role not in ['admin', 'root']:
        flash('Only tenant admin or root can erase tenant data from settings.', 'danger')
        return redirect(url_for('index'))

    hard_delete_override = request.form.get('hard_delete_override') == '1'
    required_confirm = "DELETE ALL DATA" if hard_delete_override else "DELETE SELECTED"
    if request.form.get('confirm_text') != required_confirm:
        if hard_delete_override:
            flash('Incorrect confirmation text. Type DELETE ALL DATA for hard cleanup.', 'danger')
        else:
            flash('Incorrect confirmation text', 'danger')
        return redirect(url_for('settings'))

    targets = request.form.getlist('delete_targets')
    # Centralized wipe registry: map a user-facing dataset to all related
    # dataset keys used by the wipe engine. This ensures dependent accounting
    # and ledger tables are removed together with the primary dataset.
    WIPE_REGISTRY = {
        'clients': ['recon_basket', 'bookings', 'pending_bills', 'payments', 'direct_sales', 'entry', 'fbm_rental_clients', 'accounts', 'account_transactions', 'accounts_domain_wipe'],
        'suppliers': ['grn', 'supplier_payments', 'payments', 'invoices', 'accounts_domain_wipe'],
        'direct_sales': ['direct_sales', 'entry', 'invoice', 'invoices', 'delivery_person_payments', 'pending_bills', 'payments', 'accounts_domain_wipe'],
        'accounts': ['accounts', 'account_transactions', 'payments', 'supplier_payments', 'grn', 'direct_sales', 'fbm_rentals'],
        'payments': ['payments', 'supplier_payments', 'delivery_person_payments', 'pending_bills', 'account_transactions', 'accounts_domain_wipe'],
        'invoices': ['invoices', 'direct_sales', 'entry', 'pending_bills', 'accounts_domain_wipe'],
        'bookings': ['bookings', 'booking_item', 'pending_bills', 'accounts_domain_wipe'],
        'pending_bills': ['pending_bills', 'follow_up_contact', 'follow_up_reminder', 'accounts_domain_wipe'],
        'supplier_payments': ['supplier_payments', 'supplier', 'accounts_domain_wipe'],
        # Keep registry extensible for future datasets
    }
    # Expand selected targets to include all dependent datasets from the registry.
    expanded = set(targets or [])
    for t in list(expanded):
        for dep in WIPE_REGISTRY.get(t, []):
            expanded.add(dep)
    # Replace targets with the expanded set for the rest of the wipe flow.
    targets = list(expanded)
    if not targets:
        flash('No datasets selected for deletion', 'warning')
        return redirect(url_for('settings'))

    history_row = None
    backup_filename = None
    backup_path = None
    if _WIPE_BACKUP_ENABLED:
        try:
            backup_filename, backup_path = None, None
            history_row = TenantWipeBackupHistory(
                tenant_name='single_store',
                performed_by=getattr(current_user, 'username', None),
                performed_by_role=getattr(current_user, 'role', None),
                targets=', '.join(sorted(set(targets))),
                backup_filename=backup_filename,
                backup_path=backup_path,
                wipe_status='pending',
                note='Snapshot recorded before wipe.'
            )
            db.session.add(history_row)
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('Pre-wipe history logging failed. Wipe blocked.', 'danger')
            return redirect(url_for('settings'))

    def _tq(model):
        return model.query

    def _purge_driver_payment_ledger(payment_query):
        """Remove the linked account rows with their driver payments.

        A driver payment owns exactly one authoritative AccountTransaction.
        Deleting the source without the ledger row would leave an orphan money-out
        entry and permanently skew the account balance, so both go together and
        the balance effect is reversed first.
        """
        from app.services.accounting import _void_account_tx
        ids = [row[0] for row in payment_query.with_entities(DeliveryPersonPayment.id).all()]
        if not ids:
            return
        linked = AccountTransaction.query.filter(
            AccountTransaction.source_type == 'DeliveryPersonPayment',
            AccountTransaction.source_id.in_(ids),
        ).all()
        for tx in linked:
            _void_account_tx(tx)  # restore the account balance before removal
            db.session.delete(tx)
        db.session.flush()

    forbidden_targets = {
        'clients',
        'materials',
        'pending_bills',
        'dispatching',
        'receiving',
        'direct_sales',
        'material_returns',
        'payments',
        'bookings',
        'invoices',
        'delivery_person_payments',
        'fbm_rental_clients',
        'fbm_rentals',
    }
    blocked = sorted(set(targets).intersection(forbidden_targets))
    if blocked and not hard_delete_override:
        flash(f'Hard delete blocked for: {", ".join(blocked)}. Use suspend/void workflows instead.', 'danger')
        return redirect(url_for('settings'))
    if blocked and hard_delete_override:
        flash(f'Hard cleanup override enabled for: {", ".join(blocked)}', 'warning')

    try:
        backup_info = _create_pre_wipe_safety_backups(targets)
    except Exception as e:
        logging.getLogger('app').exception('Pre-wipe safety backup failed.')
        flash(f'Pre-wipe backup failed. Wipe blocked: {e}', 'danger')
        return redirect(url_for('settings'))

    _RESET_CONTEXT = 'granular_wipe'
    try:
        deleted_info = []
        # Keep core config intact: Users/Roles, login sessions, and Settings.
        # Wipe transactional/master data only. Never delete User rows.

        full_set = {
            'clients', 'suppliers', 'supplier_payments', 'pending_bills',
            'dispatching', 'receiving', 'grn', 'materials', 'material_categories',
            'direct_sales', 'material_returns', 'delivery_rents', 'delivery_persons',
            'invoices', 'payments', 'bookings', 'notifications'
        }
        is_full_wipe = full_set.issubset(set(targets))

        if is_full_wipe:
            # Full transactional reset (everything except users/settings/staff email).
            _tq(FollowUpContact).delete()
            _tq(FollowUpReminder).delete()
            _tq(StaffEmail).delete()
            _tq(PendingBill).delete()
            _tq(Entry).delete()
            _tq(DeliveryItem).delete()
            _tq(Delivery).delete()
            _tq(GRNItem).delete()
            _tq(GRN).delete()
            _tq(DirectSaleItem).delete()
            _tq(DirectSale).delete()
            _tq(MaterialReturnItem).delete()
            _tq(MaterialReturn).delete()
            _tq(WaiveOff).delete()
            _tq(Payment).delete()
            _tq(SupplierPayment).delete()
            _tq(BookingItem).delete()
            _tq(Booking).delete()
            _tq(Invoice).delete()
            _tq(ReconBasket).delete()
            _tq(Supplier).delete()
            _tq(DeliveryRent).delete()
            _tq(DeliveryPerson).delete()
            _tq(Material).delete()
            _tq(MaterialCategory).delete()
            _tq(Client).delete()
            _tq(BillCounter).delete()
            db.session.add(BillCounter(namespace=AUTO_BILL_NS_DEFAULT, count=1000))
            deleted_info.append('Full Wipe (All Transactions)')
            if history_row:
                history_row.wipe_status = 'completed'
                history_row.note = f'Completed full wipe. Targets: {", ".join(sorted(set(targets)))}'
            db.session.commit()
            _complete_intentional_wipe_workflow(targets, deleted_info, backup_info, 'full')
            flash(f'Data Wiped: {", ".join(deleted_info)}', 'danger')
            return redirect(url_for('settings'))

        if 'pending_bills' in targets:
            # Bulk delete does not trigger ORM cascades, so clear dependent follow-up tables first.
            _tq(FollowUpContact).delete()
            _tq(FollowUpReminder).delete()
            _tq(PendingBill).delete()
            deleted_info.append('Pending Bills + Follow-ups')

        if 'notifications' in targets:
            _tq(FollowUpContact).delete()
            _tq(FollowUpReminder).delete()
            _tq(StaffEmail).delete()
            deleted_info.append('Notification Data (Follow-ups + Staff Emails)')

        if 'dispatching' in targets:
            _tq(Entry).filter_by(type='OUT').delete()
            _tq(DeliveryItem).delete()
            _tq(Delivery).delete()
            deleted_info.append('Dispatching Entries')

        if 'receiving' in targets:
            _tq(Entry).filter_by(type='IN').delete()
            deleted_info.append('Receiving Entries')

        if 'grn' in targets:
            _tq(GRNItem).delete()
            _tq(GRN).delete()
            deleted_info.append('GRN Records')

        if 'supplier_payments' in targets:
            _tq(SupplierPayment).delete()
            deleted_info.append('Supplier Payments')

        if 'suppliers' in targets:
            _tq(GRN).update({'supplier_id': None}, synchronize_session=False)
            _tq(SupplierPayment).delete()
            _tq(Supplier).delete()
            deleted_info.append('Suppliers')

        if 'direct_sales' in targets:
            linked_invoice_ids = [
                row[0] for row in _tq(DirectSale).with_entities(DirectSale.invoice_id)
                .filter(DirectSale.invoice_id.isnot(None)).distinct().all()
            ]
            _dpp_sale_scope = _tq(DeliveryPersonPayment).filter(
                or_(
                    DeliveryPersonPayment.sale_id.isnot(None),
                    DeliveryPersonPayment.allocation_id.isnot(None)
                )
            )
            _purge_driver_payment_ledger(_dpp_sale_scope)
            _dpp_sale_scope.delete(synchronize_session=False)
            _tq(DeliveryRent).delete()
            _tq(SaleDeliveryPerson).delete()
            _tq(DirectSaleItem).delete()
            _tq(DirectSale).delete()
            _tq(Entry).filter(Entry.nimbus_no == 'Direct Sale').delete(synchronize_session=False)
            _tq(PendingBill).filter(
                func.lower(func.coalesce(PendingBill.reason, '')).like('direct sale%')
            ).delete(synchronize_session=False)
            if linked_invoice_ids:
                _tq(Invoice).filter(Invoice.id.in_(linked_invoice_ids)).delete(synchronize_session=False)
            deleted_info.append('Direct Sales')

        if 'material_returns' in targets:
            _tq(MaterialReturnItem).delete()
            _tq(MaterialReturn).delete()
            _tq(Entry).filter(Entry.nimbus_no == 'Material Return').delete(synchronize_session=False)
            _tq(Payment).filter(Payment.note.like('[MATERIAL_RETURN:%')).delete(synchronize_session=False)
            deleted_info.append('Material Returns')

        if 'payments' in targets:
            _tq(WaiveOff).delete()
            _tq(Payment).delete()
            _tq(MaterialReturn).update({'payment_id': None}, synchronize_session=False)
            _tq(PendingBill).filter(
                func.lower(func.coalesce(PendingBill.reason, '')).like('payment received%')
            ).delete(synchronize_session=False)
            deleted_info.append('Payments')

        if 'delivery_rents' in targets:
            _purge_driver_payment_ledger(_tq(DeliveryPersonPayment))
            _tq(DeliveryPersonPayment).delete()
            _tq(DeliveryRent).delete()
            _tq(SaleDeliveryPerson).delete()
            deleted_info.append('Delivery Rents')

        if 'delivery_persons' in targets:
            _purge_driver_payment_ledger(_tq(DeliveryPersonPayment))
            _tq(DeliveryPersonPayment).delete()
            _tq(SaleDeliveryPerson).delete()
            _tq(DeliveryPerson).delete()
            deleted_info.append('Delivery Persons')

        if 'bookings' in targets:
            _tq(BookingItem).delete()
            _tq(Booking).delete()
            _tq(PendingBill).filter(
                func.lower(func.coalesce(PendingBill.reason, '')).like('booking:%')
            ).delete(synchronize_session=False)
            deleted_info.append('Bookings')

        if 'invoices' in targets:
            _tq(DirectSale).update({'invoice_id': None}, synchronize_session=False)
            _tq(Entry).update({'invoice_id': None}, synchronize_session=False)
            _tq(Invoice).delete()
            deleted_info.append('Invoices')

        if 'materials' in targets:
            _tq(Material).delete()
            deleted_info.append('Materials')

        if 'material_categories' in targets:
            _tq(Material).update({'category_id': None}, synchronize_session=False)
            _tq(MaterialCategory).delete()
            deleted_info.append('Material Categories')

        if 'clients' in targets:
            _tq(Client).delete()
            _tq(ReconBasket).delete()
            deleted_info.append('Clients + Reconciliation Basket')

        # NEW TARGETS: Cash Management & Financial Accounts
        if 'cash_reconciliation_audit' in targets:
            _tq(CashFlowReconciliationAudit).delete()
            deleted_info.append('Cash Reconciliation Audit Trail')

        if 'cash_reconciliation_data' in targets:
            _tq(CashFlowDifferenceAdjustment).delete()
            deleted_info.append('Cash Reconciliation Data')

        if 'account_transactions' in targets:
            _tq(AccountTransaction).delete()
            deleted_info.append('Account Transactions')

        if 'cash_drawer_entries' in targets:
            _tq(FbmCashDrawerEntry).delete()
            deleted_info.append('Cash Drawer Entries')

        if 'cash_drawer_categories' in targets:
            _tq(FbmCashDrawerEntry).update({'category': None}, synchronize_session=False)
            _tq(FbmCashDrawerCategory).delete()
            deleted_info.append('Cash Drawer Categories')

        if 'delivery_person_payments' in targets:
            _purge_driver_payment_ledger(_tq(DeliveryPersonPayment))
            _tq(DeliveryPersonPayment).delete()
            deleted_info.append('Driver Payments')

        if 'direct_sale_drafts' in targets:
            _tq(DirectSaleDraft).delete()
            deleted_info.append('Unsaved Sales Drafts')

        # NEW TARGETS: Rental Management (FBM)
        if 'fbm_rentals' in targets:
            _tq(FBMRental).delete()
            deleted_info.append('Rental Agreements')

        if 'fbm_rental_clients' in targets:
            _tq(FBMRental).filter(FBMRental.client_id.isnot(None)).delete(synchronize_session=False)
            _tq(FBMClient).delete()
            deleted_info.append('Rental Customers')

        if 'fbm_rental_items' in targets:
            _tq(FBMRental).filter(FBMRental.item_id.isnot(None)).delete(synchronize_session=False)
            _tq(FBMRentalItem).delete()
            deleted_info.append('Rental Inventory Items')

        # NEW TARGETS: Account Management
        if 'account_categories' in targets:
            _tq(Account).update({'category': 'cash'}, synchronize_session=False)
            _tq(AccountCategory).delete()
            deleted_info.append('Account Categories')

        if 'accounts' in targets:
            _tq(Payment).update({'payment_account_id': None}, synchronize_session=False)
            _tq(SupplierPayment).update({'payment_account_id': None}, synchronize_session=False)
            _tq(GRN).update({'payment_account_id': None}, synchronize_session=False)
            _tq(DirectSale).update({'payment_account_id': None}, synchronize_session=False)
            _tq(FBMRental).update({'payment_account_id': None}, synchronize_session=False)
            # Note: AccountTransaction uses from_account_id / to_account_id, no account_id column.
            # Instead of deleting account definitions outright, reset financial state.
            # Keep account rows (structure) but zero balances so dashboard shows baseline.
            try:
                _tq(AccountTransaction).delete()
            except Exception:
                pass
            try:
                _tq(FbmCashDrawerEntry).delete()
            except Exception:
                pass
            try:
                _tq(CashFlowDifferenceAdjustment).delete()
            except Exception:
                pass
            try:
                _tq(CashFlowReconciliationAudit).delete()
            except Exception:
                pass
            # Nullify payment-account links but preserve the account records.
            _tq(Payment).update({'payment_account_id': None}, synchronize_session=False)
            _tq(SupplierPayment).update({'payment_account_id': None}, synchronize_session=False)
            # Reset balances on all accounts to zero.
            _tq(Account).update({'balance': 0}, synchronize_session=False)
            deleted_info.append('Financial Accounts (transactions cleared, balances reset)')

        # ACCOUNTS_DOMAIN_WIPE: additional domain that aggressively clears
        # all accounting snapshots, rollups and cash/bank state after wipes.
        if 'accounts_domain_wipe' in targets or is_full_wipe:
            # Delete transaction-level data and snapshot tables.
            try:
                _tq(AccountTransaction).delete()
            except Exception:
                pass
            try:
                _tq(FbmCashDrawerEntry).delete()
            except Exception:
                pass
            try:
                _tq(FbmCashDrawerCategory).delete()
            except Exception:
                pass
            try:
                _tq(CashFlowDifferenceAdjustment).delete()
            except Exception:
                pass
            try:
                _tq(CashFlowReconciliationAudit).delete()
            except Exception:
                pass
            # Clear supplier and payment links to accounts
            _tq(SupplierPayment).update({'payment_account_id': None}, synchronize_session=False)
            _tq(Payment).update({'payment_account_id': None}, synchronize_session=False)
            # Reset all known account balances to zero
            _tq(Account).update({'balance': 0}, synchronize_session=False)
            deleted_info.append('Accounts Domain Wipe (all accounting snapshots cleared, balances reset)')

        # Always clean orphan invoices to avoid hidden "bill already exists" residue.
        orphan_invoice_count = _tq(Invoice).filter(
            ~exists().where(DirectSale.invoice_id == Invoice.id),
            ~exists().where(Entry.invoice_id == Invoice.id)
        ).delete(synchronize_session=False)
        if orphan_invoice_count:
            deleted_info.append(f'Orphan Invoices ({orphan_invoice_count})')

        if history_row:
            history_row.wipe_status = 'completed'
            history_row.note = f'Completed selective wipe. Targets: {", ".join(sorted(set(targets)))}'
        db.session.commit()
        _complete_intentional_wipe_workflow(targets, deleted_info, backup_info, 'selective')
        flash(f'Data Wiped: {", ".join(deleted_info)}', 'danger')
    except Exception as e:
        db.session.rollback()
        if history_row:
            try:
                history_row.wipe_status = 'failed'
                history_row.note = f'Wipe failed after snapshot: {e}'
                db.session.commit()
            except Exception:
                db.session.rollback()
        flash(f'Wipe failed: {str(e)}', 'danger')
    finally:
        _RESET_CONTEXT = None

    return redirect(url_for('settings'))

