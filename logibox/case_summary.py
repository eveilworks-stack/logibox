"""Read-only case progress derived from recorded money movements and issued PDFs.

No persisted flags or schema changes: legacy amounts and documents remain intact.
"""
import datetime


def occurred(value, today):
    try:
        return bool(value) and datetime.date.fromisoformat(value) <= today
    except (ValueError, TypeError):
        return False


def state(label, complete=False, **values):
    return dict(label=label, complete=complete, **values)


def payment(amount, paid, date, today):
    valid_amount = isinstance(amount, int) and amount > 0
    valid_paid = isinstance(paid, int) and paid > 0
    dated = occurred(date, today)
    confirmed = paid if valid_paid and dated else 0
    if valid_amount and confirmed == amount:
        label = '支払済み'
    elif valid_amount and confirmed > amount:
        label = '要確認（支払額超過）'
    elif (valid_paid and not dated) or (date and not valid_paid) or not valid_amount:
        label = '要確認（金額・支払日）'
    else:
        label = '一部支払' if confirmed else '未払い'
    return state(label, valid_amount and confirmed == amount,
                 confirmed=confirmed, remaining=max(0, (amount or 0)-confirmed))


def haak_paid(haak, today=None):
    amount = haak.get('confirmed_amount_tax_in')
    actual = haak.get('actual_paid_amount')
    return bool(haak.get('confirmed_checked')) and payment(amount,
        amount if actual is None else actual, haak.get('paid_date'),
        today or datetime.date.today())['complete']


def enrich(view, today=None):
    today = today or datetime.date.today()
    for p in view['purchases']:
        p['progress'] = payment(p['amount_tax_in'], p['paid_amount'], p['paid_date'], today)
    for doc in view['documents']:
        snapshot = doc['snapshot']
        complete = (doc['pdf_available'] and occurred(doc['issued_date'], today)
                    and isinstance(doc['amount_tax_in'], int) and doc['amount_tax_in'] > 0
                    and bool(snapshot.get('doc_no')))
        doc['progress'] = state('発行済み' if complete else '要確認（発行記録）', complete)
    h = view['haak']
    amount = h.get('confirmed_amount_tax_in')
    # Existing DBs use NULL actual_paid_amount to mean the entire confirmed amount.
    implicit = h.get('actual_paid_amount') is None
    actual = amount if implicit else h['actual_paid_amount']
    progress = payment(amount, actual, h.get('paid_date'), today)
    confirmed = isinstance(amount, int) and amount > 0 and bool(h.get('confirmed_checked'))
    complete = haak_paid(h, today)
    label = ('入金済み' if complete else '確定額の確認待ち' if not confirmed
             else '一部入金' if progress['confirmed'] else '入金待ち')
    if h.get('paid_date') and not complete and not progress['confirmed']:
        label = '要確認（金額・入金日）'
    if isinstance(amount, int) and progress['confirmed'] > amount:
        label = '要確認（入金額超過）'
    invoices = [d for d in view['documents'] if d['doc_type'] == 'haak_invoice']
    view['haak_progress'] = state(label, complete, amount_confirmed=confirmed,
        received=progress['confirmed'], remaining=progress['remaining'], implicit_full=implicit,
        invoice_ids=[d['id'] for d in invoices],
        invoice_mismatch=any(d['amount_tax_in'] != amount for d in invoices))
    for sub in view['subs']:
        data = sub['funding']
        cash = sum(x['amount'] for x in data['cash_history']
                   if x['amount'] > 0 and occurred(x['received_on'], today))
        legacy_cash = 0
        if not data['cash_history'] and occurred(sub.get('principal_received_date'), today):
            legacy_cash = max(0, sub['principal_tax_in']-sub['carry_reserved'])
        ledger = sum(x['amount'] for x in data['ledger_incoming']
                     if x['amount'] > 0 and occurred(x['event_date'], today))
        legacy = sum(x['carry_over'] for x in data['incoming']
                     if x.get('status') == 'paid' and occurred(x.get('paid_at'), today))
        total = cash + legacy_cash + ledger + legacy
        required = sub['principal_tax_in']
        complete = required > 0 and total == required and not data['cash_planned']
        sub['progress'] = state('元金充足済み' if complete else '要確認（担当元金）' if required <= 0
            else '要確認（元金超過）' if total > required
            else '要確認（未確認の入金予定あり）' if total == required and data['cash_planned']
            else '入金・充当待ち', complete, cash=cash+legacy_cash, legacy_cash=legacy_cash,
            allocated=ledger+legacy, confirmed=total, remaining=max(0, required-total),
            planned=data['cash_planned'])
    for line in view.get('settlement_history', []):
        paid = line['status'] == 'paid' and occurred(line.get('paid_at'), today)
        line['progress'] = state('支払済み' if paid else '要確認（支払日）'
            if line['status'] == 'paid' else {'not_requested':'依頼前', 'requested':'依頼済み',
                'received':'請求書受領済み', 'mismatch':'金額相違'}.get(line['status'], '要確認'), paid)
    ps = view['purchases']
    view['progress'] = dict(
        purchase=state('支払済み' if ps and all(p['progress']['complete'] for p in ps)
                       else '支払確認待ち' if ps else '未登録', bool(ps) and all(p['progress']['complete'] for p in ps)),
        haak=view['haak_progress'])
    return view
