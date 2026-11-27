"""都度精算の元金成分を、利益・手動返金・一時預かりから分離して集計する。"""

def totals(c, cid, sid, exclude=None):
    # During a legacy DB migration, the additive column may not exist yet.
    columns={r[1] for r in c.execute('PRAGMA table_info(profit_payments)')}
    if 'principal_amount' not in columns:
        return dict(paid=0, planned=0)
    row=c.execute('''SELECT
        COALESCE(SUM(CASE WHEN paid_date IS NOT NULL THEN principal_amount ELSE 0 END),0) AS paid,
        COALESCE(SUM(CASE WHEN paid_date IS NULL THEN principal_amount ELSE 0 END),0) AS planned
        FROM profit_payments WHERE case_id=? AND sub_id=? AND cancelled=0 AND id<>?''',
        (cid,sid,exclude or 0)).fetchone()
    return dict(row)


def available(c, sub, exclude=None):
    import principal_balance
    import principal_funding
    value=principal_balance.values(c,sub,allow_monthly=True,exclude_payment=exclude)
    amount=value['available']
    line=principal_funding.source_line(c,sub['case_id'],sub['sub_id'])
    # Unassigned legacy custody is not ordinary principal available for refund.
    if line and line['carry_over'] and not line['carry_to_case_id'] and not c.execute(
            'SELECT 1 FROM custody_funds WHERE line_id=?',(line['id'],)).fetchone():
        amount=max(0,amount-line['carry_over'])
    return max(0,amount)
