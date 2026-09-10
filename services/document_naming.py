"""Display names are independent of immutable storage object IDs."""
import re
from datetime import date


def clean_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', str(value or ''))
    return re.sub(r'\s+', ' ', value).strip(' .-')


def start_year(value):
    text = str(value or '').translate(str.maketrans('๐๑๒๓๔๕๖๗๘๙', '0123456789'))
    iso = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', text)
    local = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', text)
    if iso:
        year, month, day = map(int, iso.groups())
    elif local:
        day, month, year = map(int, local.groups())
    else:
        return None
    year = year - 543 if year >= 2400 else year
    try:
        date(year, month, day)
    except ValueError:
        return None
    return str(year + 543)


def make_display_filename(plate, doc_type, coverage_start=None, coverage_end=None,
                          policy_type=None, address=None, name=None, risk_address=None):
    # coverage_end and contact address intentionally cannot substitute for missing evidence.
    pt = str(policy_type or '').strip().upper()
    prb = doc_type in {'prb', 'motor_prb'} or (doc_type == 'main' and pt == 'P')
    if not prb and (pt in {'FIRE', 'ASSET', 'IAR', 'BURGLAR'} or doc_type == 'fire'):
        ident = clean_component(risk_address)
        stem = ident
    elif not prb and pt in {'PA', 'TA', '3RD', 'PUBLIC', 'MISC', 'GOLF', 'MARINE'}:
        ident = clean_component(name)
        stem = ident
    else:
        ident = clean_component(re.sub(r'\s+', '', str(plate or '')))
        year = start_year(coverage_start)
        if not ident or not year:
            return 'รอตรวจข้อมูล.pdf'
        stem = f"{ident}-{'พรบ.' if prb else 'กธ'}-{year}"
    if not ident:
        return 'รอตรวจข้อมูล.pdf'
    if doc_type == 'endorsement':
        stem += '-สลักหลัง'
    return stem + '.pdf'
