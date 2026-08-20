"""profit — split from reports.py."""
from ._common import *  # noqa

@bp.route('/financial_details')
@login_required
def financial_details():
    type_filter = request.args.get('type', 'cash')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    client_query = request.args.get('client', '').strip()
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)

    if not start_date: start_date = pk_today().strftime('%Y-%m-%d')
    if not end_date: end_date = pk_today().strftime('%Y-%m-%d')

    # Resolve client code to name if applicable
    if client_query and (
        client_query.lower().startswith('tmpc-') or
        client_query.lower().startswith('fbmcl-') or
        client_query.lower().startswith('fbm-') or
        client_query[0].isdigit()
    ):
         c = Client.query.filter(Client.code.ilike(f'%{client_query}%')).first()
         if c:
             client_query = c.name

    transactions = []

    if type_filter == 'cash':
        # 1. Payments
        q_pay = Payment.query.filter(func.date(Payment.date_posted) >= start_date,
                                   func.date(Payment.date_posted) <= end_date, Payment.is_void == False)
        if client_query:
            q_pay = q_pay.filter(Payment.client_name.ilike(f'%{client_query}%'))
        if min_price is not None: q_pay = q_pay.filter(Payment.amount >= min_price)
        if max_price is not None: q_pay = q_pay.filter(Payment.amount <= max_price)

        for p in q_pay.all():
            transactions.append({
                'date': p.date_posted,
                'client': p.client_name,
                'amount': p.amount,
                'type': 'Payment',
                'ref': p.manual_bill_no or p.auto_bill_no or f'PAY-{p.id}'
            })

        # 2. Booking Advances
        q_book = Booking.query.filter(func.date(Booking.date_posted) >= start_date,
                                    func.date(Booking.date_posted) <= end_date,
                                    Booking.paid_amount > 0, Booking.is_void == False)
        if client_query:
            q_book = q_book.filter(Booking.client_name.ilike(f'%{client_query}%'))
        if min_price is not None: q_book = q_book.filter(Booking.paid_amount >= min_price)
        if max_price is not None: q_book = q_book.filter(Booking.paid_amount <= max_price)

        for b in q_book.all():
            transactions.append({
                'date': b.date_posted,
                'client': b.client_name,
                'amount': b.paid_amount,
                'type': 'Booking Advance',
                'ref': b.manual_bill_no or b.auto_bill_no or f'BK-{b.id}'
            })

        # 3. Direct Sale Cash
        q_sale = DirectSale.query.filter(func.date(DirectSale.date_posted) >= start_date,
                                       func.date(DirectSale.date_posted) <= end_date,
                                       DirectSale.paid_amount > 0, DirectSale.is_void == False)
        if client_query:
            q_sale = q_sale.filter(DirectSale.client_name.ilike(f'%{client_query}%'))
        if min_price is not None: q_sale = q_sale.filter(DirectSale.paid_amount >= min_price)
        if max_price is not None: q_sale = q_sale.filter(DirectSale.paid_amount <= max_price)

        for s in q_sale.all():
            transactions.append({
                'date': s.date_posted,
                'client': s.client_name,
                'amount': s.paid_amount,
                'type': 'Direct Sale',
                'ref': s.manual_bill_no or s.auto_bill_no or f'DS-{s.id}'
            })

    elif type_filter == 'credit':
        # 1. Booking Credit
        q_book = Booking.query.filter(func.date(Booking.date_posted) >= start_date,
                                    func.date(Booking.date_posted) <= end_date,
                                    (Booking.amount - Booking.paid_amount) > 0, Booking.is_void == False)
        if client_query:
            q_book = q_book.filter(Booking.client_name.ilike(f'%{client_query}%'))

        for b in q_book.all():
            credit = b.amount - b.paid_amount
            if min_price is not None and credit < min_price: continue
            if max_price is not None and credit > max_price: continue
            transactions.append({
                'date': b.date_posted,
                'client': b.client_name,
                'amount': credit,
                'type': 'Booking Credit',
                'ref': b.manual_bill_no or b.auto_bill_no or f'BK-{b.id}'
            })

        # 2. Direct Sale Credit
        q_sale = DirectSale.query.filter(func.date(DirectSale.date_posted) >= start_date,
                                       func.date(DirectSale.date_posted) <= end_date,
                                       (DirectSale.amount - DirectSale.paid_amount) > 0, DirectSale.is_void == False)
        if client_query:
            q_sale = q_sale.filter(DirectSale.client_name.ilike(f'%{client_query}%'))

        for s in q_sale.all():
            credit = s.amount - s.paid_amount
            if min_price is not None and credit < min_price: continue
            if max_price is not None and credit > max_price: continue
            transactions.append({
                'date': s.date_posted,
                'client': s.client_name,
                'amount': credit,
                'type': 'Direct Sale Credit',
                'ref': s.manual_bill_no or s.auto_bill_no or f'DS-{s.id}'
            })

    transactions.sort(key=lambda x: x['date'], reverse=True)

    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()

    return render_template('financial_details.html',
                           transactions=transactions,
                           type=type_filter,
                           start_date=start_date,
                           end_date=end_date,
                           client=client_query,
                           min_price=min_price,
                           max_price=max_price,
                           clients=clients,
                           materials=materials)

