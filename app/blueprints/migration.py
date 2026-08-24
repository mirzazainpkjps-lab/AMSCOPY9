from __future__ import annotations
import io, json
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from openpyxl import Workbook
from models import MigrationRun, MigrationRow
from app.services.legacy_migration import TEMPLATES, template_workbook, validate_upload, import_run

bp=Blueprint('legacy_migration',__name__,url_prefix='/legacy-migration')
def allowed(): return current_user.role in {'admin','root'} or bool(getattr(current_user,'can_import_export',False))
def guard():
    if not allowed(): abort(403)
@bp.route('/')
@login_required
def dashboard():
    guard(); runs=MigrationRun.query.order_by(MigrationRun.created_at.desc()).limit(50).all()
    completed={r.template_type for r in runs if r.status=='COMPLETED'}
    return render_template('legacy_migration.html',templates=TEMPLATES,runs=runs,completed=completed)
@bp.route('/template/<kind>')
@login_required
def download(kind):
    guard()
    if kind not in TEMPLATES: abort(404)
    buf=io.BytesIO(); template_workbook(kind).save(buf); buf.seek(0)
    return send_file(buf,as_attachment=True,download_name=TEMPLATES[kind]['file'],mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
@bp.route('/upload',methods=['POST'])
@login_required
def upload():
    guard(); kind=(request.form.get('template_type') or '').upper(); file=request.files.get('file')
    if not file or not file.filename.lower().endswith('.xlsx'): flash('Choose an official .xlsx template.', 'danger'); return redirect(url_for('.dashboard'))
    try:
        run,_=validate_upload(kind,file.read(),file.filename,current_user.username)
        flash(f'Validation complete for run #{run.id}. Review the preview before importing.', 'success')
        return redirect(url_for('.preview',run_id=run.id))
    except ValueError as exc: flash(str(exc),'danger'); return redirect(url_for('.dashboard'))
@bp.route('/run/<int:run_id>')
@login_required
def preview(run_id):
    guard(); run=MigrationRun.query.get_or_404(run_id); rows=MigrationRow.query.filter_by(run_id=run.id).order_by(MigrationRow.source_sheet,MigrationRow.source_row).all()
    return render_template('legacy_migration_preview.html',run=run,rows=rows,summary=json.loads(run.summary_json or '{}'))
@bp.route('/run/<int:run_id>/dry-run',methods=['POST'])
@login_required
def dry_run(run_id):
    guard(); run=MigrationRun.query.get_or_404(run_id); flash('Dry run is non-destructive: '+(run.summary_json or '{}')+'. No production records were changed.','info'); return redirect(url_for('.preview',run_id=run.id))
@bp.route('/run/<int:run_id>/import',methods=['POST'])
@login_required
def do_import(run_id):
    guard(); run=MigrationRun.query.get_or_404(run_id)
    try: flash(f'Import completed: {import_run(run)} records created.','success')
    except ValueError as exc: flash(str(exc),'warning')
    return redirect(url_for('.preview',run_id=run.id))
@bp.route('/run/<int:run_id>/errors.xlsx')
@login_required
def errors(run_id):
    guard(); run=MigrationRun.query.get_or_404(run_id); wb=Workbook(); ws=wb.active; ws.title='MIGRATION_ERRORS'; ws.append(['Source File','Sheet','Excel Row','Column','Problem','Suggested Action','Status'])
    for row in run.rows:
        for e in json.loads(row.error_json or '[]'): ws.append([run.filename,e.get('sheet'),e.get('row'),e.get('column'),e.get('problem'),e.get('suggested_action'),e.get('status')])
    buf=io.BytesIO(); wb.save(buf); buf.seek(0); return send_file(buf,as_attachment=True,download_name=f'migration_run_{run.id}_errors.xlsx')
