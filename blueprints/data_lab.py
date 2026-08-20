from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from models import db, Client, PendingBill, Entry, ReconBasket
try:
    import pandas as pd
except ImportError:
    pd = None
from difflib import SequenceMatcher
from datetime import datetime
from zoneinfo import ZoneInfo
import io

# Module configuration
MODULE_CONFIG = {
    'name': 'Data Lab Module',
    'description': 'Data analysis and reconciliation module',
    'url_prefix': '/data_lab',
    'enabled': True
}

bp = Blueprint('data_lab', __name__)
PK_TZ = ZoneInfo('Asia/Karachi')


def pk_now():
    return datetime.now(PK_TZ).replace(tzinfo=None)


def ensure_pandas_installed():
    if pd is None:
        flash(
            'The Data Lab module requires pandas and openpyxl. Install them to use this feature.',
            'danger'
        )
        return False
    return True


def read_table(file_storage):
    if not file_storage:
        return None
    filename = file_storage.filename.lower()
    try:
        data = file_storage.read()
        file_storage.stream.seek(0)
        if filename.endswith('.csv'):
            return pd.read_csv(io.BytesIO(data))
        else:
            # try excel
            return pd.read_excel(io.BytesIO(data), engine='openpyxl')
    except Exception:
        file_storage.stream.seek(0)
        try:
            return pd.read_csv(file_storage)
        except Exception:
            return None


def name_score(a, b):
    if not a or not b:
        return 0
    return int(SequenceMatcher(None, str(a).lower(), str(b).lower()).ratio() * 100)


@bp.route('/', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        if not ensure_pandas_installed():
            return render_template('data_lab.html')

        index_file = request.files.get('index_file')
        finance_file = request.files.get('finance_file')
        dispatch_file = request.files.get('dispatch_file')

        ledger_df = read_table(index_file) if index_file else None
        fin_df = read_table(finance_file) if finance_file else None
        inv_df = read_table(dispatch_file) if dispatch_file else None

        ledger_map = {}
        if ledger_df is not None:
            # try to detect columns
            cols = [c.lower() for c in ledger_df.columns]
            name_col = None
            code_col = None
            for c in ledger_df.columns:
                lc = c.lower()
                if 'code' in lc:
                    code_col = c
                if 'name' in lc:
                    name_col = c
            if name_col and code_col:
                for _, r in ledger_df.iterrows():
                    try:
                        ledger_map[str(r[code_col]).strip()] = str(r[name_col]).strip()
                    except Exception:
                        continue

        # normalize df column names
        def norm(df):
            if df is None:
                return None
            df = df.copy()
            df.columns = [c.strip() for c in df.columns]
            return df

        fin_df = norm(fin_df)
        inv_df = norm(inv_df)

        # Build quick lookup by bill_no
        fin_by_bill = {}
        if fin_df is not None and 'bill_no' in [c.lower() for c in fin_df.columns]:
            for _, r in fin_df.iterrows():
                bill = str(r[[c for c in fin_df.columns if c.lower() == 'bill_no'][0]]).strip()
                fin_by_bill.setdefault(bill, []).append(r)

        inv_rows = []
        if inv_df is not None:
            for _, r in inv_df.iterrows():
                inv_rows.append(r)

        # Triangulate
        for r in inv_rows:
            # try extract bill_no and client
            bill_col = [c for c in inv_df.columns if c.lower() == 'bill_no']
            bill = ''
            if bill_col:
                bill = str(r[bill_col[0]]).strip()
            inv_client_col = [c for c in inv_df.columns if 'client' in c.lower()]
            inv_client = ''
            if inv_client_col:
                inv_client = str(r[inv_client_col[0]]).strip()
            material_col = [c for c in inv_df.columns if 'material' in c.lower() or 'item' in c.lower()]
            material = ''
            if material_col:
                material = str(r[material_col[0]]).strip()
            qty_col = [c for c in inv_df.columns if 'qty' in c.lower() or 'quantity' in c.lower()]
            qty = 0
            if qty_col:
                try:
                    qty = float(r[qty_col[0]])
                except Exception:
                    qty = 0

            if not bill or bill == 'nan' or bill.strip() == '':
                # BLUE: unbilled dispatch
                basket = ReconBasket(bill_no='', inv_date=None, inv_client=inv_client, inv_material=material, inv_qty=qty, status='BLUE', match_score=0)
                db.session.add(basket)
                continue

            fin_list = fin_by_bill.get(bill, [])
            if fin_list:
                # match against first finance row
                fin_r = fin_list[0]
                fin_client_col = [c for c in fin_df.columns if 'client' in c.lower()]
                fin_client = ''
                if fin_client_col:
                    fin_client = str(fin_r[fin_client_col[0]]).strip()
                score = name_score(fin_client, inv_client)
                if score >= 90:
                    # GREEN: auto-save to DB (create Entry if not exists)
                    now = pk_now()
                    entry = Entry(
                        date=now.strftime('%Y-%m-%d'),
                        time=now.strftime('%H:%M:%S'),
                        type='OUT',
                        material=material,
                        client=fin_client or inv_client,
                        client_code=None,
                        qty=qty,
                        bill_no=bill,
                        created_by='import'
                    )
                    db.session.add(entry)
                    # ensure pending bill exists
                    pending = PendingBill.query.filter_by(bill_no=bill).first()
                    if not pending:
                        pending = PendingBill(
                            bill_no=bill,
                            client_name=fin_client or inv_client,
                            client_code=None,
                            amount=0,
                            created_at=now.strftime('%Y-%m-%d %H:%M'),
                            created_by='import'
                        )
                        db.session.add(pending)
                    # do not add to basket (auto-applied)
                else:
                    # YELLOW: conflict
                    basket = ReconBasket(bill_no=bill, fin_client=fin_client, inv_client=inv_client, inv_material=material, inv_qty=qty, status='YELLOW', match_score=score)
                    db.session.add(basket)
            else:
                # RED: waiting - exists in dispatch but not in finance
                basket = ReconBasket(bill_no=bill, inv_client=inv_client, inv_material=material, inv_qty=qty, status='RED', match_score=0)
                db.session.add(basket)

        # Now check finance-only bills (in finance but not in dispatch)
        if fin_df is not None:
            for _, r in fin_df.iterrows():
                bill = str(r[[c for c in fin_df.columns if c.lower() == 'bill_no'][0]]).strip()
                if inv_df is None or bill not in [str(x[[c for c in inv_df.columns if c.lower() == 'bill_no'][0]]).strip() for _, x in inv_df.iterrows()]:
                    # RED entry (finance only)
                    client_col = [c for c in fin_df.columns if 'client' in c.lower()]
                    fin_client = ''
                    if client_col:
                        fin_client = str(r[client_col[0]]).strip()
                    basket = ReconBasket(bill_no=bill, fin_client=fin_client, status='RED', match_score=0)
                    db.session.add(basket)

        db.session.commit()
        flash('Files processed. Review the Recon Basket.', 'success')
        return redirect(url_for('data_lab.view_basket'))

    return render_template('data_lab.html')


@bp.route('/basket')
@login_required
def view_basket():
    groups = {}
    for status in ['GREEN', 'YELLOW', 'RED', 'BLUE']:
        groups[status] = ReconBasket.query.filter_by(status=status).all()
    # also include any other statuses
    others = ReconBasket.query.filter(~ReconBasket.status.in_(['GREEN', 'YELLOW', 'RED', 'BLUE'])).all()
    return render_template('basket_view.html', groups=groups, others=others)


@bp.route('/correct_bill', methods=['POST'])
@login_required
def correct_bill():
    bill_no = request.form.get('bill_no')
    client_code = request.form.get('client_code')
    client = Client.query.filter_by(code=client_code).first()
    if not bill_no or not client:
        flash('Missing bill or client', 'danger')
        return redirect(url_for('data_lab.view_basket'))
    # update PendingBill and Entry
    PendingBill.query.filter_by(bill_no=bill_no).update({'client_name': client.name, 'client_code': client.code})
    Entry.query.filter_by(bill_no=bill_no).update({'client': client.name, 'client_code': client.code})
    # remove basket entries for that bill
    ReconBasket.query.filter_by(bill_no=bill_no).delete()
    db.session.commit()
    flash('Bill corrected across records.', 'success')
    return redirect(url_for('data_lab.view_basket'))


@bp.route('/legacy_import', methods=['POST'])
@login_required
def legacy_import():
    # In legacy import mode we assume finance is 100% correct and overwrite dispatch typos.
    bill_no = request.form.get('bill_no')
    if not bill_no:
        flash('Provide bill_no for legacy import', 'danger')
        return redirect(url_for('data_lab.view_basket'))
    pending = PendingBill.query.filter_by(bill_no=bill_no).first()
    if not pending:
        flash('Pending bill not found', 'danger')
        return redirect(url_for('data_lab.view_basket'))
    # find baskets and entries with that bill and overwrite
    ReconBasket.query.filter_by(bill_no=bill_no).delete()
    Entry.query.filter_by(bill_no=bill_no).update({'client': pending.client_name, 'client_code': pending.client_code})
    db.session.commit()
    flash('Legacy import applied.', 'success')
    return redirect(url_for('data_lab.view_basket'))
