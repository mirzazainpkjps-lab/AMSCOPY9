from ._common import *  # noqa

@bp.route('/add_client', methods=['POST'])
@login_required
def add_client():
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip()
    if not name:
        flash('Client name is required', 'danger')
        return redirect(url_for('clients'))
    if not code:
        code = generate_client_code()
    if Client.query.filter_by(code=code).first():
        flash(f'Client code "{code}" already exists', 'danger')
        return redirect(url_for('clients'))
    category = request.form.get('category', 'General').strip() or 'General'
    opening_balance = _to_float_or_zero(request.form.get('opening_balance', 0))
    opening_balance_date = _resolve_opening_balance_date(request.form.get('opening_balance_date'))
    page_entries_raw = request.form.getlist('page_entry')
    page_entries_clean = [str(x).strip() for x in page_entries_raw if str(x).strip()]
    page_notes_value = ' | '.join(page_entries_clean) if page_entries_clean else (request.form.get('page_notes', '') or '').strip()

    new_c = Client(name=name,
                   code=code,
                   phone=request.form.get('phone', ''),
                   address=request.form.get('address', ''),
                   category=category,
                   book_no='',
                   financial_book_no='',
                   financial_page='',
                   cement_book_no='',
                   cement_page='',
                   steel_book_no='',
                   steel_page=page_notes_value,
                   location_url=(request.form.get('location_url', '') or '').strip(),
                   page_notes=page_notes_value,
                   opening_balance=opening_balance,
                   opening_balance_date=opening_balance_date)
    db.session.add(new_c)
    db.session.commit()
    flash('Client Registered', 'success')
    return redirect(url_for('clients'))

