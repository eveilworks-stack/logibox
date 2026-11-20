"""金額計算モジュール(整数・Decimalのみ。floatは使わない)。
端数の扱い(TOMOSを引き継ぐ):
  下請け利益 = 円未満切り上げ / 手数料 = 円未満切り捨て / 自社の利益 = 残り
  ハーク請求額(予定) = 円未満切り捨て / 消費税 = 請求書ごと・税率ごとに1回、切り捨て
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def _pct(x):
    return Decimal(str(x)) / Decimal(100)


def floor_yen(v):
    return int(Decimal(v).to_integral_value(rounding=ROUND_FLOOR))


def ceil_yen(v):
    return int(Decimal(v).to_integral_value(rounding=ROUND_CEILING))


def haak_planned_amount(purchase_tax_in, planned_rate_pct):
    """ハーク請求の見込額(税込) = 仕入額 ×(1+予定利益率)、円未満切り捨て。"""
    return floor_yen(Decimal(purchase_tax_in) * (1 + _pct(planned_rate_pct)))


def sub_fee(principal, fee_rate_pct=2):
    """自社の手数料利益 = 担当元金 × 手数料率、円未満切り捨て。"""
    return floor_yen(Decimal(principal) * _pct(fee_rate_pct))


def sub_profit(principal, planned_rate_pct, fee_rate_pct=2):
    """下請け利益 = 担当元金 ×(予定利益率 − 手数料率)、円未満切り上げ。確定額が変わっても動かない。"""
    return ceil_yen(Decimal(principal) * (_pct(planned_rate_pct) - _pct(fee_rate_pct)))


def tax_split(tax_in, rate_pct=10):
    """税込合計 → (税抜, 消費税)。消費税 = 税込 × r/(100+r)、1回だけ切り捨て。"""
    tax = floor_yen(Decimal(tax_in) * Decimal(rate_pct) / (100 + Decimal(rate_pct)))
    return tax_in - tax, tax


def case_profit(haak_confirmed_tax_in, purchase_tax_in, subs, planned_rate_pct):
    """subs: [{'principal':int,'fee_rate':2}, ...]。全体利益・下請け利益合計・自社利益を返す。"""
    total = haak_confirmed_tax_in - purchase_tax_in
    sub_total = sum(sub_profit(s['principal'], planned_rate_pct, s.get('fee_rate', 2)) for s in subs)
    fee_total = sum(sub_fee(s['principal'], s.get('fee_rate', 2)) for s in subs)
    return {'total_profit': total, 'sub_profit_total': sub_total,
            'fee_total': fee_total, 'own_profit': total - sub_total}


def sub_payout(principal, profit, carry_over=0):
    """下請けへの振込額 = 元金 + 下請け利益 − 次案件への充当。"""
    return principal + profit - carry_over
