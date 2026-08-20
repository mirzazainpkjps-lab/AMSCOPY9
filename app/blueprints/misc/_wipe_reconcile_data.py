"""wipe — split from misc.py."""
from ._common import *  # noqa

@bp.route('/reconcile_data', methods=['POST'])
@login_required
def reconcile_data():
    if current_user.role != 'admin':
        flash('Unauthorized', 'danger')
        return redirect(url_for('settings'))

    apply_fixes = str(request.form.get('apply_fixes', '')).strip().lower() in ['1', 'true', 'on', 'yes']
    try:
        report = _run_reconciliation(apply_fixes=apply_fixes)
        db.session.commit()
        if apply_fixes and report.get('bill_normalized_count', 0) > 0:
            try:
                from app.services.import_artifacts import reports_dir as _import_reports_dir
                reports_dir = str(_import_reports_dir())
                os.makedirs(reports_dir, exist_ok=True)
                ts = pk_now().strftime('%Y%m%d_%H%M%S')
                path = os.path.join(reports_dir, f"bill_normalization_audit_{ts}.md")
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write("# Bill Normalization Audit Report\n\n")
                    fh.write(f"- Generated at: {report.get('ran_at')}\n")
                    fh.write(f"- Total normalized fields: {report.get('bill_normalized_count', 0)}\n\n")
                    fh.write("## Sample Changes\n\n")
                    for row in (report.get('bill_normalized_sample') or []):
                        fh.write(f"- {row.get('entity')}#{row.get('id')} `{row.get('field')}`: `{row.get('from')}` -> `{row.get('to')}`\n")
                report['bill_audit_report_path'] = path
            except Exception:
                pass
        session['recon_report'] = report
        flash(
            f"Reconciliation {report['mode']} complete. Broken refs: {report['broken_refs_count']}, "
            f"DS mismatches: {report['direct_sale_mismatch_count']}, "
            f"DS waive mismatches: {report.get('direct_sale_waive_mismatch_count', 0)}, "
            f"Booking mismatches: {report['booking_mismatch_count']}, "
            f"Payment mismatches: {report['payment_mismatch_count']}, "
            f"Bill normalized: {report.get('bill_normalized_count', 0)}, "
            f"Fixes: {report['fixes_applied']}",
            'success'
        )
    except Exception as e:
        db.session.rollback()
        flash(f'Reconciliation failed: {str(e)}', 'danger')
    return redirect(url_for('settings'))

