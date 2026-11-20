"""LINEの案件案内から、明示された項目の候補を取り出す。"""
import re
import unicodedata
from datetime import date


MAKER_MAX_LENGTH = 1000


FIELDS = {
    'case_no': ('案件番号', '案件No.', '案件No', '案件ナンバー'),
    'name': ('案件名', '案件名称'),
    'start_date': ('開始日', '開始予定日'),
    'due_date': ('納期', '納品期限', '納品予定日'),
    'planned_rate_pct': ('予定利益率', '予定利益率(%)', '見込利益率', '利益率(%)', '利益率', '予定利益', '利益'),
    'maker': ('メーカー', 'メーカー名'),
    'purchase_limit_tax_in': ('税込仕入限度額', '税込仕入限度額(円)', '仕入限度額(税込)', '仕入上限額(税込)', '税込仕入上限額'),
}


def normalized(value):
    return unicodedata.normalize('NFKC', value).strip()


LABELS = {label.casefold(): key for key, labels in FIELDS.items() for label in labels}
LABEL_PATTERN = '|'.join(re.escape(label) for label in sorted(LABELS, key=len, reverse=True))
MARKER = re.compile(
    rf'(?:^[ \t]*(?:[・●■◆▶※*—-][ \t]*)?|[ \t、;]+(?=(?:{LABEL_PATTERN})[ \t]*[:=])|[ \t、;]+(?=[【\[]))'
    rf'(?:[【\[](?P<bracketed>{LABEL_PATTERN})[】\]](?:[ \t]*[:=])?'
    rf'|(?P<label>{LABEL_PATTERN})(?:[ \t]*[:=]|[ \t]+|$))',
    re.IGNORECASE,
)
EMPTY = {'', '-', '―', '—', '?', '未定', '不明', '後日', 'なし', '未設定', 'n/a', 'tbd'}


DATE_NOTES = re.compile(
    r'(?:[ \t]*\([ \t]*(?:[月火水木金土日](?:曜(?:日)?)?|[0-9]+[ \t]*(?:日間|週間|か月間))[ \t]*\))+$'
)


def parse_date(value):
    value = DATE_NOTES.sub('', value)
    groups = re.fullmatch(r'([0-9]{4})年[ \t]*([0-9]{1,2})月[ \t]*([0-9]{1,2})日', value)
    if groups is None:
        groups = re.fullmatch(r'([0-9]{4})(?P<sep>[-/.])([0-9]{1,2})(?P=sep)([0-9]{1,2})', value)
        if groups is None:
            raise ValueError
        year, _, month, day = groups.groups()
    else:
        year, month, day = groups.groups()
    return date(int(year), int(month), int(day)).isoformat()


def parse_value(key, raw):
    value = normalized(raw)
    if value.casefold() in EMPTY:
        return None
    if key in ('start_date', 'due_date'):
        return parse_date(value)
    if key == 'planned_rate_pct':
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?[ \t]*%?', value):
            raise ValueError
        rate = float(value.rstrip('%'))
        if rate > 100:
            raise ValueError
        return rate
    if key == 'purchase_limit_tax_in':
        if not re.fullmatch(r'(?:¥[ \t]*)?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:[ \t]*円)?', value):
            raise ValueError
        amount = int(value.removeprefix('¥').removesuffix('円').replace(',', ''))
        if amount > 10**12:
            raise ValueError
        return amount
    if key == 'case_no':
        if len(raw) > 50 or not re.fullmatch(r'[\w./-]+', value) or not any(ch.isalnum() for ch in value):
            raise ValueError
    elif len(raw) > (MAKER_MAX_LENGTH if key == 'maker' else 100):
        raise ValueError
    return raw


def parse_line_notice(text):
    """返り値は登録フォーム用の候補と、形式不正・値の競合の警告。"""
    fields = dict.fromkeys(FIELDS)
    candidates = {key: [] for key in FIELDS}
    for line in text.splitlines():
        # 貼り付けた文章に Markdown の改行記号が付いている場合も扱う。
        line = line.rstrip().removesuffix('\\')
        # ラベル照合には正規化した文字を使い、入力欄の文字列には原文を使う。
        parts = [unicodedata.normalize('NFKC', char) for char in line]
        offsets = [i for i, part in enumerate(parts) for _ in part] + [len(line)]
        markers = list(MARKER.finditer(''.join(parts)))
        for index, match in enumerate(markers):
            key = LABELS[(match['label'] or match['bracketed']).casefold()]
            end = markers[index + 1].start() if index + 1 < len(markers) else len(offsets) - 1
            candidates[key].append(line[offsets[match.end()]:offsets[end]].strip())

    warnings = []
    for key, values in candidates.items():
        try:
            parsed = {parse_value(key, value) for value in values}
        except ValueError:
            warnings.append(f'{FIELDS[key][0]}の形式を確認してください。')
            continue
        parsed.discard(None)
        if len(parsed) > 1:
            warnings.append(f'{FIELDS[key][0]}に異なる値があるため、確認してください。')
        elif parsed:
            fields[key] = parsed.pop()
    return fields, warnings

