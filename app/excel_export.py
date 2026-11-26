import calendar
import io
import os
import re
import urllib.parse
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.utils.units import pixels_to_EMU
from PIL import Image as PILImage, ImageOps

from . import config, fx
from .db import db, settings
from .domain import currency, entry_view, fail, receipt_media_type

EXCEL_MEDIA_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

KIND_FILTER_VALUES = ('all', 'public', 'advance', 'income', 'operating', 'capital', 'expense')
STATUS_FILTER_VALUES = ('all', 'all_with_voided', 'draft', 'pending', 'partial', 'reimbursed', 'posted', 'voided')

STATUS_LABELS = {
    'draft': '草稿',
    'pending': '待报销',
    'partial': '部分报销',
    'reimbursed': '已报销',
    'posted': '已入账',
    'voided': '已作废',
}

SOURCE_LABELS = {
    'web': 'Web',
    'telegram': 'Telegram',
    'feishu': '飞书',
    'ai': 'AI',
    'migration': '迁移',
}

KIND_LABELS = {
    'all': '全部类型',
    'public': '公账支出',
    'advance': '成员垫付',
    'income': '全部收入',
    'operating': '营业收入',
    'capital': '公户转入',
    'expense': '全部支出',
}

# Vanora-inspired light architectural spreadsheet palette
FONT_FAMILY = 'Microsoft YaHei'
COLOR_INK = '18181B'
COLOR_MUTED = '52525B'
COLOR_HEADER_BG = '1E293B'
COLOR_HEADER_FG = 'FFFFFF'
COLOR_SECTION_BG = 'F1F5F9'
COLOR_ZEBRA_BG = 'FAFAFA'
COLOR_BORDER = 'E2E8F0'
COLOR_BORDER_STRONG = 'CBD5E1'
COLOR_LINK = '1D4ED8'
COLOR_WARN_BG = 'FEF3C7'
COLOR_WARN_FG = '92400E'

STATUS_STYLE = {
    'posted': ('ECFDF5', '065F46'),
    'reimbursed': ('EFF6FF', '1E40AF'),
    'pending': ('FFFBEB', '92400E'),
    'partial': ('F5F3FF', '5B21B6'),
    'draft': ('F4F4F5', '52525B'),
    'voided': ('FEF2F2', '991B1B'),
}

MAX_IMAGE_PIXELS = 40_000_000
THUMB_MAX_SIZE = (148, 92)
GALLERY_MAX_SIZE = (520, 360)
MAX_EXPORT_IDS = 2000


def safe_excel_text(value):
    text = str(value or '')
    if not text:
        return ''
    # Prevent spreadsheet formula injection while preserving readable plain text.
    if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + text
    return text


def _set_text(cell, value):
    cell.value = safe_excel_text(value)
    cell.data_type = 's'
    return cell


def _number_format(decimals):
    decimals = int(decimals or 0)
    if decimals <= 0:
        return '#,##0'
    return '#,##0.' + ('0' * decimals)


def _set_money(cell, value, decimals):
    if value is None or value == '':
        cell.value = None
        return cell
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    cell.value = dec
    cell.number_format = _number_format(decimals)
    return cell


def _minor_to_decimal(units, decimals):
    return Decimal(units) / (Decimal(10) ** int(decimals))


def _display_status(entry):
    if entry['status'] == 'pending' and entry['reimbursed_minor'] > 0:
        return 'partial'
    return entry['status']


def _entry_type_label(entry):
    if entry['kind'] == 'income':
        return '公户转入' if entry.get('income_type') == 'capital' else '营业收入'
    if entry['expense_type'] == 'public':
        return '公账支出'
    return '成员垫付'


def _fee_mode_label(entry):
    mode = entry.get('fee_mode') or 'none'
    if mode == 'rate' and entry.get('fee_rate'):
        return f'按比例 {entry["fee_rate"]}%'
    if mode == 'fixed' and (entry.get('fee_minor') or 0) > 0:
        return '手动金额'
    return '无'


def _format_timestamp(ts):
    if not ts:
        return ''
    return str(ts).replace('T', ' ')[:19]


def _parse_iso_date(value, label):
    if value in (None, ''):
        return ''
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        fail(f'{label}格式不正确，请使用 YYYY-MM-DD')
    if parsed.year < 2000 or parsed.year > 2100:
        fail(f'{label}超出允许范围')
    return parsed.isoformat()


def _parse_bool(value, default=True):
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in ('1', 'true', 'yes', 'on'):
        return True
    if lowered in ('0', 'false', 'no', 'off'):
        return False
    fail('布尔参数格式不正确')


def _parse_ids(raw):
    if raw in (None, '', []):
        return []
    parts = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(',')
    ids = []
    seen = set()
    for part in parts:
        text = str(part).strip()
        if not text:
            continue
        if not text.isdigit():
            fail('账目编号列表格式不正确')
        num = int(text)
        if num <= 0:
            fail('账目编号列表格式不正确')
        if num not in seen:
            seen.add(num)
            ids.append(num)
    if len(ids) > MAX_EXPORT_IDS:
        fail(f'单次最多选择导出 {MAX_EXPORT_IDS} 笔指定账目')
    return ids


def parse_export_filters(
    conn,
    *,
    kind='all',
    status=None,
    currency_code='all',
    payer_id=None,
    date_from='',
    date_to='',
    text='',
    ids=None,
    base=None,
    include_voided=None,
    include_receipts=True,
):
    conf = settings(conn)

    kind_val = (kind or 'all').strip().lower()
    if kind_val not in KIND_FILTER_VALUES:
        fail('筛选类型不正确')

    parsed_ids = _parse_ids(ids)

    raw_status = (status or '').strip().lower()
    if not raw_status:
        if include_voided is not None and _parse_bool(include_voided, False):
            status_val = 'all_with_voided'
        elif parsed_ids:
            status_val = 'all_with_voided'
        else:
            status_val = 'all'
    else:
        if raw_status not in STATUS_FILTER_VALUES:
            fail('筛选状态不正确')
        status_val = raw_status

    cur_val = (currency_code or 'all').strip().upper()
    if cur_val == 'ALL':
        cur_val = 'all'
    else:
        currency(conf, cur_val)

    payer_val = None
    payer_name = ''
    if payer_id not in (None, '', 'all', 'ALL'):
        try:
            payer_val = int(str(payer_id).strip())
        except ValueError:
            fail('筛选成员格式不正确')
        member = conn.execute('SELECT id, name FROM users WHERE id=?', (payer_val,)).fetchone()
        if not member:
            fail('筛选成员不存在')
        payer_name = member['name']

    from_val = _parse_iso_date(date_from, '开始日期')
    to_val = _parse_iso_date(date_to, '结束日期')
    if from_val and to_val and from_val > to_val:
        fail('开始日期不能晚于结束日期')

    text_val = str(text or '').strip()
    if len(text_val) > 120:
        fail('搜索关键词最多 120 个字符')

    base_val = (base or conf['reference_currency']).strip().upper()
    currency(conf, base_val)

    receipts_flag = _parse_bool(include_receipts, True)

    return {
        'kind': kind_val,
        'status': status_val,
        'currency': cur_val,
        'payer_id': payer_val,
        'payer_name': payer_name,
        'date_from': from_val,
        'date_to': to_val,
        'text': text_val,
        'ids': parsed_ids,
        'base': base_val,
        'include_receipts': receipts_flag,
    }


def _matches_filters(entry, filters):
    if filters['ids'] and entry['id'] not in filters['ids']:
        return False

    kind = filters['kind']
    if kind == 'income' and entry['kind'] != 'income':
        return False
    if kind == 'operating' and not (entry['kind'] == 'income' and (entry.get('income_type') or 'operating') == 'operating'):
        return False
    if kind == 'capital' and not (entry['kind'] == 'income' and entry.get('income_type') == 'capital'):
        return False
    if kind == 'expense' and entry['kind'] != 'expense':
        return False
    if kind == 'public' and not (entry['kind'] == 'expense' and entry['expense_type'] == 'public'):
        return False
    if kind == 'advance' and not (entry['kind'] == 'expense' and entry['expense_type'] == 'advance'):
        return False

    if filters['currency'] != 'all' and entry['currency'] != filters['currency']:
        return False
    if filters['payer_id'] is not None and entry['payer_id'] != filters['payer_id']:
        return False

    status = filters['status']
    if status == 'all':
        if entry['status'] == 'voided':
            return False
    elif status == 'all_with_voided':
        pass
    elif status == 'partial':
        if _display_status(entry) != 'partial':
            return False
    elif entry['status'] != status:
        return False

    if filters['date_from'] and entry['date'] < filters['date_from']:
        return False
    if filters['date_to'] and entry['date'] > filters['date_to']:
        return False

    if filters['text']:
        needle = filters['text'].lower()
        haystack = ' '.join([
            f'#{entry["id"]}',
            entry.get('description') or '',
            entry.get('note') or '',
            entry.get('payer_name') or '',
            entry.get('review_note') or '',
        ]).lower()
        if needle not in haystack:
            return False

    return True


def _build_scope_label(filters):
    parts = []
    if filters['ids']:
        parts.append(f'指定 {len(filters["ids"])} 笔账目')
    if filters['date_from'] and filters['date_to']:
        parts.append(f'{filters["date_from"]} 至 {filters["date_to"]}')
    elif filters['date_from']:
        parts.append(f'{filters["date_from"]} 起')
    elif filters['date_to']:
        parts.append(f'截至 {filters["date_to"]}')
    if filters['kind'] != 'all':
        parts.append(KIND_LABELS.get(filters['kind'], filters['kind']))
    if filters['currency'] != 'all':
        parts.append(f'币种 {filters["currency"]}')
    if filters['payer_id'] is not None:
        parts.append(f'成员 {filters["payer_name"]}')
    if filters['status'] == 'all_with_voided' and not filters['ids']:
        parts.append('含已作废')
    elif filters['status'] not in ('all', 'all_with_voided'):
        parts.append(f'状态 {STATUS_LABELS.get(filters["status"], filters["status"])}')
    if filters['text']:
        parts.append(f'关键词“{filters["text"]}”')
    if not parts:
        return '全部有效账目（不含已作废）'
    return ' · '.join(parts)


def _is_filtered(filters):
    return bool(
        filters['ids']
        or filters['kind'] != 'all'
        or filters['currency'] != 'all'
        or filters['payer_id'] is not None
        or filters['status'] not in ('all', 'all_with_voided')
        or filters['date_from']
        or filters['date_to']
        or filters['text']
    )


def _safe_receipt_path(filename):
    if not filename:
        return None
    receipts_dir = (config.DATA / 'receipts').resolve()
    candidate = (receipts_dir / filename).resolve()
    if candidate.parent != receipts_dir or not candidate.is_file():
        return None
    return candidate


def _prepare_embedded_image(path, max_size):
    """Resize and EXIF-orient a PNG/JPG receipt into compact RGB JPEG bytes for Excel embedding."""
    try:
        with PILImage.open(path) as img:
            if img.width <= 0 or img.height <= 0 or (img.width * img.height) > MAX_IMAGE_PIXELS:
                return None
            img = ImageOps.exif_transpose(img)
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                rgba = img.convert('RGBA')
                bg = PILImage.new('RGB', rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail(max_size, PILImage.Resampling.LANCZOS)
            width, height = img.size
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=84, optimize=True)
            buf.seek(0)
            return buf, width, height
    except Exception:
        return None


def _attach_image_in_cell(ws, img_buf, width_px, height_px, row_idx, col_idx, pad_x=6, pad_y=6):
    xl_img = XLImage(img_buf)
    xl_img.width = width_px
    xl_img.height = height_px
    marker = AnchorMarker(
        col=col_idx - 1,
        colOff=pixels_to_EMU(pad_x),
        row=row_idx - 1,
        rowOff=pixels_to_EMU(pad_y),
    )
    size = XDRPositiveSize2D(cx=pixels_to_EMU(width_px), cy=pixels_to_EMU(height_px))
    xl_img.anchor = OneCellAnchor(_from=marker, ext=size)
    ws.add_image(xl_img)


def _display_width(text):
    total = 0
    for ch in str(text or ''):
        total += 2 if ord(ch) > 127 else 1
    return total


def _thin_border(color=COLOR_BORDER):
    side = Side(style='thin', color=color)
    return Border(left=side, right=side, top=side, bottom=side)


def _header_border():
    thin = Side(style='thin', color=COLOR_HEADER_BG)
    bottom = Side(style='medium', color=COLOR_INK)
    return Border(left=thin, right=thin, top=thin, bottom=bottom)


def _style_table_header(ws, row_idx, headers):
    fill = PatternFill('solid', fgColor=COLOR_HEADER_BG)
    font = Font(name=FONT_FAMILY, size=10, bold=True, color=COLOR_HEADER_FG)
    align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    border = _header_border()
    ws.row_dimensions[row_idx].height = 28
    for col_idx, title in enumerate(headers, start=1):
        cell = ws.cell(row=row_idx, column=col_idx, value=title)
        cell.fill = fill
        cell.font = font
        cell.alignment = align
        cell.border = border


def _style_section_banner(ws, row_idx, col_span, title):
    ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=col_span)
    fill = PatternFill('solid', fgColor=COLOR_SECTION_BG)
    font = Font(name=FONT_FAMILY, size=11, bold=True, color=COLOR_INK)
    border = Border(bottom=Side(style='thin', color=COLOR_BORDER_STRONG))
    ws.row_dimensions[row_idx].height = 24
    for col_idx in range(1, col_span + 1):
        cell = ws.cell(row=row_idx, column=col_idx)
        cell.fill = fill
        cell.border = border
    cell = ws.cell(row=row_idx, column=1, value=title)
    cell.font = font
    cell.alignment = Alignment(horizontal='left', vertical='center')


def _apply_print_setup(ws, landscape=True):
    ws.views.sheetView[0].showGridLines = True
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE if landscape else ws.ORIENTATION_PORTRAIT
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0


def _auto_fit_columns(ws, min_widths=None, max_widths=None, skip_cols=None):
    min_widths = min_widths or {}
    max_widths = max_widths or {}
    skip_cols = set(skip_cols or ())
    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.column in skip_cols or cell.value is None:
                continue
            # Skip merged title rows spanning multiple columns.
            if any(
                rng.min_row <= cell.row <= rng.max_row
                and rng.min_col <= cell.column <= rng.max_col
                and rng.min_col != rng.max_col
                for rng in ws.merged_cells.ranges
            ):
                continue
            text = str(cell.value)
            longest_line = max((_display_width(line) for line in text.splitlines()), default=0)
            widths[cell.column] = max(widths.get(cell.column, 0), longest_line + 4)
    for col_idx in range(1, ws.max_column + 1):
        if col_idx in skip_cols:
            continue
        low = min_widths.get(col_idx, 10)
        high = max_widths.get(col_idx, 42)
        computed = widths.get(col_idx, low)
        ws.column_dimensions[get_column_letter(col_idx)].width = max(low, min(high, computed))


def _currency_breakdown(entries, payments, conf):
    cur_meta = {c['code']: c for c in conf['currencies']}
    codes = []
    seen = set()
    for item in list(entries) + list(payments):
        code = item['currency']
        if code not in seen:
            seen.add(code)
            codes.append(code)

    stats = {
        code: {
            'code': code,
            'decimals': cur_meta.get(code, {}).get('decimals', 2),
            'operating_income': Decimal(0),
            'capital_income': Decimal(0),
            'income': Decimal(0),
            'public': Decimal(0),
            'advance_total': Decimal(0),
            'paid_actual': Decimal(0),
            'advance_due': Decimal(0),
            'net_cash': Decimal(0),
            'net_after_due': Decimal(0),
            'income_fees': Decimal(0),
            'public_fees': Decimal(0),
            'advance_fees': Decimal(0),
            'total_fees': Decimal(0),
            'entry_count': 0,
        }
        for code in codes
    }

    for e in entries:
        bucket = stats[e['currency']]
        bucket['decimals'] = e['decimals']
        bucket['entry_count'] += 1
        amt = _minor_to_decimal(e['amount_minor'], e['decimals'])
        fee = _minor_to_decimal(e.get('fee_minor') or 0, e['decimals'])
        if e['status'] == 'voided' or e['status'] == 'draft':
            continue
        if e['kind'] == 'income' and e['status'] == 'posted':
            if e.get('income_type') == 'capital':
                bucket['capital_income'] += amt
            else:
                bucket['operating_income'] += amt
            bucket['income'] += amt
            bucket['income_fees'] += fee
            bucket['net_cash'] += amt
        elif e['kind'] == 'expense' and e['expense_type'] == 'public' and e['status'] == 'posted':
            bucket['public'] += amt
            bucket['public_fees'] += fee
            bucket['net_cash'] -= amt
        elif e['kind'] == 'expense' and e['expense_type'] == 'advance':
            bucket['advance_total'] += amt
            bucket['advance_fees'] += fee
            if e['status'] == 'pending':
                bucket['advance_due'] += _minor_to_decimal(e['amount_minor'] - e['reimbursed_minor'], e['decimals'])

    for p in payments:
        bucket = stats[p['currency']]
        bucket['decimals'] = p['decimals']
        amt = _minor_to_decimal(p['amount_minor'], p['decimals'])
        bucket['paid_actual'] += amt
        bucket['net_cash'] -= amt

    for item in stats.values():
        item['net_after_due'] = item['net_cash'] - item['advance_due']
        item['operating_profit'] = item['operating_income'] - item['public'] - item['paid_actual']
        item['operating_profit_after_due'] = item['operating_profit'] - item['advance_due']
        item['total_fees'] = item['income_fees'] + item['public_fees'] + item['advance_fees']

    return [stats[c] for c in codes]


def _sanitize_filename_part(text, fallback='team-ledger'):
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', '-', str(text or '')).strip(' .-')
    return cleaned[:40] or fallback


def _build_filenames(team_name, filters):
    if filters['date_from'] and filters['date_to']:
        date_tag = f'{filters["date_from"]}_{filters["date_to"]}'
    elif filters['date_from']:
        date_tag = f'{filters["date_from"]}_onwards'
    elif filters['date_to']:
        date_tag = f'until_{filters["date_to"]}'
    else:
        date_tag = config.today()

    if filters['ids']:
        suffix = f'selected-{len(filters["ids"])}-{date_tag}'
    elif filters['kind'] != 'all':
        suffix = f'{filters["kind"]}-{date_tag}'
    else:
        suffix = date_tag

    ascii_filename = f'team-ledger-{suffix}.xlsx'
    safe_team = _sanitize_filename_part(team_name, 'Vanora财务管理')
    display_filename = f'{safe_team}-财务报表-{date_tag}.xlsx'
    return display_filename, ascii_filename


def content_disposition_header(display_filename, ascii_filename):
    encoded = urllib.parse.quote(display_filename, safe='')
    return f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{encoded}'


def parse_export_shortcut(raw_text):
    """Parse concise bot export presets or date ranges into build_excel_export kwargs."""
    text = str(raw_text or '').strip()
    include_receipts = True
    stripped = re.sub(r'(?:^|\s)(?:无收据|不含收据|no[-_]?receipts?)(?=\s|$)', ' ', text, flags=re.IGNORECASE).strip()
    if stripped != text:
        include_receipts = False
        text = stripped

    today_date = date.fromisoformat(config.today())
    low = text.lower()
    if not low or low in ('all', '全部', '全部有效'):
        return {'include_receipts': include_receipts}
    if low in ('all_voided', '含作废', '全部含作废', 'with_voided'):
        return {'status': 'all_with_voided', 'include_receipts': include_receipts}
    if low in ('pending', '待报销', '待报销项目'):
        return {'kind': 'advance', 'status': 'pending', 'include_receipts': include_receipts}
    if low in ('public', '公账', '公账支出'):
        return {'kind': 'public', 'include_receipts': include_receipts}
    if low in ('advance', '垫付', '成员垫付'):
        return {'kind': 'advance', 'include_receipts': include_receipts}
    if low in ('income', '收入', '团队收入', '全部收入'):
        return {'kind': 'income', 'include_receipts': include_receipts}
    if low in ('operating', '营业收入', '营收'):
        return {'kind': 'operating', 'include_receipts': include_receipts}
    if low in ('capital', '公户转入', '转入', '股东出资', '股东出资转入', '出资'):
        return {'kind': 'capital', 'include_receipts': include_receipts}
    if low in ('today', '今日', '今天'):
        iso = today_date.isoformat()
        return {'date_from': iso, 'date_to': iso, 'include_receipts': include_receipts}
    if low in ('month', '本月'):
        first = today_date.replace(day=1).isoformat()
        return {'date_from': first, 'date_to': today_date.isoformat(), 'include_receipts': include_receipts}
    if low in ('last_month', '上月', '上个月'):
        year = today_date.year - 1 if today_date.month == 1 else today_date.year
        month = 12 if today_date.month == 1 else today_date.month - 1
        last_day = calendar.monthrange(year, month)[1]
        return {
            'date_from': date(year, month, 1).isoformat(),
            'date_to': date(year, month, last_day).isoformat(),
            'include_receipts': include_receipts,
        }

    m_month = re.fullmatch(r'(\d{4})-(\d{2})', text)
    if m_month:
        year, month = int(m_month[1]), int(m_month[2])
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            fail('月份超出允许范围，请使用 YYYY-MM 格式')
        last_day = calendar.monthrange(year, month)[1]
        return {
            'date_from': date(year, month, 1).isoformat(),
            'date_to': date(year, month, last_day).isoformat(),
            'include_receipts': include_receipts,
        }

    m_day = re.fullmatch(r'\d{4}-\d{2}-\d{2}', text)
    if m_day:
        iso = _parse_iso_date(text, '日期')
        return {'date_from': iso, 'date_to': iso, 'include_receipts': include_receipts}

    m_range = re.fullmatch(r'(\d{4}-\d{2}-\d{2})\s*(?:~|至|到|,|\s+)\s*(\d{4}-\d{2}-\d{2})', text)
    if m_range:
        start = _parse_iso_date(m_range[1], '开始日期')
        end = _parse_iso_date(m_range[2], '结束日期')
        if start > end:
            fail('开始日期不能晚于结束日期')
        return {'date_from': start, 'date_to': end, 'include_receipts': include_receipts}

    fail('导出范围格式不正确。支持：本月、上月、待报销、全部、全部含作废、2026-11 或 2026-11-01 2026-11-22')


def format_export_caption(result, team_name=''):
    prefix = f'📊 {team_name} · Excel 财务报表' if team_name else '📊 Excel 财务报表'
    receipt_note = (
        f'收据 {result["receipt_count"]} 份（内嵌图片 {result["embedded_receipt_count"]} 张'
        + (f' · PDF {result["pdf_receipt_count"]} 份' if result['pdf_receipt_count'] else '')
        + '）'
        if result['receipt_count']
        else '无收据'
    )
    return (
        f'{prefix}\n'
        f'范围：{result["scope_label"]}\n'
        f'共 {result["entry_count"]} 笔账目 · {result["payment_count"]} 条付款 · {receipt_note}'
    )[:1000]


def build_excel_export(
    *,
    kind='all',
    status=None,
    currency_code='all',
    payer_id=None,
    date_from='',
    date_to='',
    text='',
    ids=None,
    base=None,
    include_voided=None,
    include_receipts=True,
):
    with db() as conn:
        conf = settings(conn)
        filters = parse_export_filters(
            conn,
            kind=kind,
            status=status,
            currency_code=currency_code,
            payer_id=payer_id,
            date_from=date_from,
            date_to=date_to,
            text=text,
            ids=ids,
            base=base,
            include_voided=include_voided,
            include_receipts=include_receipts,
        )
        raw_rows = conn.execute(
            'SELECT e.*, u.name AS payer_name FROM entries e '
            'JOIN users u ON u.id = e.payer_id '
            'ORDER BY e.date, e.id'
        ).fetchall()
        raw_by_id = {r['id']: dict(r) for r in raw_rows}
        all_views = [entry_view(conn, r['id']) for r in raw_rows]

    entries = [e for e in all_views if _matches_filters(e, filters)]
    entry_ids = {e['id'] for e in entries}
    raw_entries = [raw_by_id[e['id']] for e in entries]

    payments_flat = []
    raw_payments = []
    for entry in entries:
        for p in entry['payments']:
            payments_flat.append((entry, p))
            raw_payments.append({
                'id': p['id'],
                'entry_id': entry['id'],
                'amount_minor': p['amount_minor'],
                'currency': p['currency'],
                'decimals': p['decimals'],
                'entry_amount_minor': p['entry_amount_minor'],
                'fx_rate': p['fx_rate'],
                'fx_date': p['fx_date'],
            })
    payments_flat.sort(key=lambda pair: (pair[1]['created_at'], pair[1]['id']))

    # Fetch / read reference FX rates outside the database lock.
    fx_codes = fx.required_codes(raw_entries, raw_payments, filters['base'])
    fx_rates = fx.rates_for(filters['base'], fx_codes)
    report_data = fx.calculate(raw_entries, conf, fx_rates, payments=raw_payments)

    base_code = filters['base']
    base_decimals = currency(conf, base_code)['decimals']
    base_quantum = Decimal(1).scaleb(-base_decimals)
    public_url = os.getenv('PUBLIC_URL', '').strip().rstrip('/')
    exported_at = _format_timestamp(config.now())
    scope_label = _build_scope_label(filters)
    is_filtered = _is_filtered(filters)

    # Pre-inspect receipts on disk for exported entries.
    receipt_info = {}
    receipt_entries = []
    embedded_count = 0
    pdf_count = 0
    # Keep BytesIO buffers alive until Workbook.save completes.
    image_buffers = []

    for entry in entries:
        raw = raw_by_id[entry['id']]
        stored_name = raw.get('receipt') or ''
        orig_name = raw.get('receipt_name') or stored_name
        if not stored_name:
            continue
        media_type = receipt_media_type(stored_name)
        path = _safe_receipt_path(stored_name)
        info = {
            'entry': entry,
            'media_type': media_type,
            'orig_name': orig_name,
            'path': path,
            'thumb': None,
            'gallery': None,
            'gallery_row': None,
        }
        if media_type == 'application/pdf':
            pdf_count += 1
        elif media_type in ('image/png', 'image/jpeg') and filters['include_receipts'] and path is not None:
            thumb = _prepare_embedded_image(path, THUMB_MAX_SIZE)
            gallery = _prepare_embedded_image(path, GALLERY_MAX_SIZE)
            if thumb:
                info['thumb'] = thumb
                image_buffers.append(thumb[0])
                embedded_count += 1
            if gallery:
                info['gallery'] = gallery
                image_buffers.append(gallery[0])
        receipt_info[entry['id']] = info
        receipt_entries.append(info)

    wb = Workbook()

    # =========================================================================
    # Sheet 1: 资金汇总
    # =========================================================================
    ws_summary = wb.active
    ws_summary.title = '资金汇总'
    _apply_print_setup(ws_summary, landscape=True)

    team_name = conf.get('team_name') or 'Vanora 财务管理'
    ws_summary.merge_cells('A1:K1')
    title_cell = ws_summary['A1']
    _set_text(title_cell, f'{team_name} · 财务导出报表')
    title_cell.font = Font(name=FONT_FAMILY, size=16, bold=True, color=COLOR_INK)
    title_cell.alignment = Alignment(horizontal='left', vertical='center')
    ws_summary.row_dimensions[1].height = 32

    ws_summary.merge_cells('A2:K2')
    sub_cell = ws_summary['A2']
    _set_text(sub_cell, f'导出范围：{scope_label}    |    导出时间：{exported_at}    |    参考币种：{base_code}')
    sub_cell.font = Font(name=FONT_FAMILY, size=9.5, color=COLOR_MUTED)
    sub_cell.alignment = Alignment(horizontal='left', vertical='center')
    ws_summary.row_dimensions[2].height = 20

    # Metadata block
    meta_rows = [
        ('导出账目笔数', f'{len(entries)} 笔', '关联报销付款', f'{len(payments_flat)} 笔'),
        ('含收据账目', f'{len(receipt_entries)} 笔（图片已内嵌 {embedded_count} 张 / PDF {pdf_count} 份）' if filters['include_receipts'] else f'{len(receipt_entries)} 笔（本次未内嵌图片）',
         '汇总口径', '当前筛选范围统计' if is_filtered else '全账本有效记录统计'),
    ]
    border_thin = _thin_border()
    label_font = Font(name=FONT_FAMILY, size=10, bold=True, color=COLOR_MUTED)
    val_font = Font(name=FONT_FAMILY, size=10, color=COLOR_INK)
    label_fill = PatternFill('solid', fgColor=COLOR_ZEBRA_BG)

    row_cursor = 4
    for l1, v1, l2, v2 in meta_rows:
        ws_summary.row_dimensions[row_cursor].height = 22
        ws_summary.merge_cells(start_row=row_cursor, start_column=2, end_row=row_cursor, end_column=4)
        ws_summary.merge_cells(start_row=row_cursor, start_column=6, end_row=row_cursor, end_column=11)
        c1 = _set_text(ws_summary.cell(row=row_cursor, column=1), l1)
        c1.font = label_font
        c1.fill = label_fill
        c1.alignment = Alignment(horizontal='left', vertical='center')
        c2 = _set_text(ws_summary.cell(row=row_cursor, column=2), v1)
        c2.font = val_font
        c2.alignment = Alignment(horizontal='left', vertical='center')
        c5 = _set_text(ws_summary.cell(row=row_cursor, column=5), l2)
        c5.font = label_font
        c5.fill = label_fill
        c5.alignment = Alignment(horizontal='left', vertical='center')
        c6 = _set_text(ws_summary.cell(row=row_cursor, column=6), v2)
        c6.font = val_font
        c6.alignment = Alignment(horizontal='left', vertical='center')
        for col_i in range(1, 12):
            ws_summary.cell(row=row_cursor, column=col_i).border = border_thin
        row_cursor += 1

    # Section A: Reference-currency summary
    row_cursor += 1
    _style_section_banner(ws_summary, row_cursor, 11, f'一、参考币种公户结余与经营利润汇总（{base_code} · 规划参考口径）')
    row_cursor += 1

    if report_data['complete']:
        if report_data['fx'].get('warning'):
            ws_summary.merge_cells(start_row=row_cursor, start_column=1, end_row=row_cursor, end_column=11)
            warn_cell = _set_text(ws_summary.cell(row=row_cursor, column=1), f'汇率提示：{report_data["fx"]["warning"]}')
            warn_cell.font = Font(name=FONT_FAMILY, size=9.5, color=COLOR_WARN_FG)
            warn_cell.fill = PatternFill('solid', fgColor=COLOR_WARN_BG)
            warn_cell.alignment = Alignment(horizontal='left', vertical='center')
            ws_summary.row_dimensions[row_cursor].height = 22
            row_cursor += 1

        _style_table_header(ws_summary, row_cursor, ['指标项', f'金额（{base_code}）', '计算口径说明', '', '', '', '', '', '', '', ''])
        ws_summary.merge_cells(start_row=row_cursor, start_column=3, end_row=row_cursor, end_column=11)
        row_cursor += 1

        kpi_rows = [
            ('营业收入', report_data['operating_income'], '范围内已入账营业收入实收合计（已扣收入手续费，计入经营利润）'),
            ('公户转入（不计入利润）', report_data['capital_income'], '范围内已入账公户转入实收合计（仅计入公户结余，不与营业收入合并计入利润）'),
            ('公户总入账', report_data['income'], '营业收入 + 公户转入（公户现金流入合计）'),
            ('公账支出', report_data['public_paid'], '范围内已入账公账支出实付合计（含公账手续费，直接扣减公户结余）'),
            ('已报销付款', report_data['paid'], '范围内实际登记的报销付款合计'),
            ('公户结余（可用资金）', report_data['actual_cash'], '营业收入 + 公户转入 − 公账支出 − 已报销付款'),
            ('待报销总额', report_data['due'], '范围内待报销成员垫付的未报销余额（含垫付手续费）'),
            ('扣除全部待报销后结余', report_data['actual_net_after_due'], '公户结余 − 待报销总额（清偿全部垫付后预计公户结余）'),
            ('经营利润（实付口径，不含公户转入）', report_data['operating_net'], '营业收入 − 公账支出 − 已报销付款（不含公户转入）'),
            ('经营净利润（含待报销，不含公户转入）', report_data['operating_net_after_due'], '营业收入 − 公账支出 − 已报销付款 − 待报销总额（不含公户转入）'),
            ('累计手续费', report_data['total_fees'],
             f'收入扣费 {report_data["income_fees"]} · 公账手续费 {report_data["public_fees"]} · 垫付手续费 {report_data["advance_fees"]}（{base_code}，已体现在实际金额中）'),
            ('可安排报销', report_data['payable'], 'min(max(公户结余, 0), 待报销总额)'),
            ('安排后结余', report_data['remaining'], '公户结余 − 可安排报销'),
            ('资金缺口', report_data['shortage'], '覆盖全部待报销所需补足金额'),
        ]
        for idx, (label, amount_str, desc) in enumerate(kpi_rows):
            ws_summary.row_dimensions[row_cursor].height = 22
            ws_summary.merge_cells(start_row=row_cursor, start_column=3, end_row=row_cursor, end_column=11)
            row_fill = PatternFill('solid', fgColor=COLOR_ZEBRA_BG if idx % 2 else 'FFFFFF')
            is_key = label in ('公户结余（可用资金）', '扣除全部待报销后结余', '经营利润（实付口径，不含公户转入）', '经营净利润（含待报销，不含公户转入）', '待报销总额', '资金缺口')
            c_label = _set_text(ws_summary.cell(row=row_cursor, column=1), label)
            c_label.font = Font(name=FONT_FAMILY, size=10, bold=is_key, color=COLOR_INK)
            c_label.alignment = Alignment(horizontal='left', vertical='center')

            c_val = _set_money(ws_summary.cell(row=row_cursor, column=2), amount_str, base_decimals)
            is_neg_alert = (label == '资金缺口' and Decimal(amount_str) > 0) or (label in ('公户结余（可用资金）', '扣除全部待报销后结余', '经营利润（实付口径，不含公户转入）', '经营净利润（含待报销，不含公户转入）') and Decimal(amount_str) < 0)
            c_val.font = Font(name=FONT_FAMILY, size=10, bold=is_key, color='991B1B' if is_neg_alert else COLOR_INK)
            c_val.alignment = Alignment(horizontal='right', vertical='center')

            c_desc = _set_text(ws_summary.cell(row=row_cursor, column=3), desc)
            c_desc.font = Font(name=FONT_FAMILY, size=9.5, color=COLOR_MUTED)
            c_desc.alignment = Alignment(horizontal='left', vertical='center')

            for col_i in range(1, 12):
                cell = ws_summary.cell(row=row_cursor, column=col_i)
                cell.fill = row_fill
                cell.border = border_thin
            row_cursor += 1
    else:
        ws_summary.merge_cells(start_row=row_cursor, start_column=1, end_row=row_cursor, end_column=11)
        missing = '、'.join(report_data['fx'].get('missing') or [])
        warn_msg = (report_data['fx'].get('warning') or '缺少有效汇率，跨币种换算暂不可用。') + (f'（缺少币种：{missing}）' if missing else '')
        warn_cell = _set_text(ws_summary.cell(row=row_cursor, column=1), warn_msg)
        warn_cell.font = Font(name=FONT_FAMILY, size=10, color=COLOR_WARN_FG)
        warn_cell.fill = PatternFill('solid', fgColor=COLOR_WARN_BG)
        warn_cell.alignment = Alignment(horizontal='left', vertical='center')
        ws_summary.row_dimensions[row_cursor].height = 24
        row_cursor += 1

    # Section B: Original-currency breakdown (always exact, no FX dependency)
    row_cursor += 1
    _style_section_banner(ws_summary, row_cursor, 11, '二、按原币种精确统计（公户原币结余与经营利润，未换算）')
    row_cursor += 1
    _style_table_header(
        ws_summary,
        row_cursor,
        ['币种', '营业收入', '公户转入', '公账支出', '已报销实付', '公户原币结余', '剩余待报销', '扣待报后结余', '经营净利润（不含转入）', '累计手续费', '账目笔数'],
    )
    row_cursor += 1

    breakdown = _currency_breakdown(entries, raw_payments, conf)
    if breakdown:
        for idx, item in enumerate(breakdown):
            ws_summary.row_dimensions[row_cursor].height = 22
            row_fill = PatternFill('solid', fgColor=COLOR_ZEBRA_BG if idx % 2 else 'FFFFFF')
            dec = item['decimals']
            c_code = _set_text(ws_summary.cell(row=row_cursor, column=1), item['code'])
            c_code.font = Font(name=FONT_FAMILY, size=10, bold=True, color=COLOR_INK)
            c_code.alignment = Alignment(horizontal='center', vertical='center')

            money_cols = [
                (2, item['operating_income']),
                (3, item['capital_income']),
                (4, item['public']),
                (5, item['paid_actual']),
                (6, item['net_cash']),
                (7, item['advance_due']),
                (8, item['net_after_due']),
                (9, item['operating_profit_after_due']),
                (10, item['total_fees']),
            ]
            for col_i, val in money_cols:
                cell = _set_money(ws_summary.cell(row=row_cursor, column=col_i), val, dec)
                cell.font = Font(name=FONT_FAMILY, size=10, bold=(col_i in (6, 8, 9)), color='991B1B' if (col_i in (6, 8, 9) and val < 0) else COLOR_INK)
                cell.alignment = Alignment(horizontal='right', vertical='center')

            c_cnt = ws_summary.cell(row=row_cursor, column=11, value=item['entry_count'])
            c_cnt.font = val_font
            c_cnt.number_format = '#,##0'
            c_cnt.alignment = Alignment(horizontal='right', vertical='center')

            for col_i in range(1, 12):
                cell = ws_summary.cell(row=row_cursor, column=col_i)
                cell.fill = row_fill
                cell.border = border_thin
            row_cursor += 1
    else:
        ws_summary.merge_cells(start_row=row_cursor, start_column=1, end_row=row_cursor, end_column=11)
        empty_cell = _set_text(ws_summary.cell(row=row_cursor, column=1), '当前导出范围内暂无账目记录')
        empty_cell.font = Font(name=FONT_FAMILY, size=10, color=COLOR_MUTED)
        empty_cell.alignment = Alignment(horizontal='center', vertical='center')
        ws_summary.row_dimensions[row_cursor].height = 24
        row_cursor += 1

    # Section C: FX rates table (if multi-currency rates or manual entry rates were used)
    used_rates = [
        (code, info)
        for code, info in sorted(report_data['fx']['rates'].items())
        if code != base_code
    ]
    manual_fx_count = report_data.get('manual_fx_count') or 0
    if used_rates or manual_fx_count:
        row_cursor += 1
        _style_section_banner(ws_summary, row_cursor, 11, '三、本次报表汇率说明')
        row_cursor += 1
        if used_rates:
            _style_table_header(ws_summary, row_cursor, ['原币种', '换算币种', '参考汇率（1 原币 = ?）', '汇率日期', '汇率来源', '', '', '', '', '', ''])
            ws_summary.merge_cells(start_row=row_cursor, start_column=5, end_row=row_cursor, end_column=11)
            row_cursor += 1
            fx_source = report_data['fx'].get('source') or 'Frankfurter · 每日参考汇率'
            for idx, (code, rate_info) in enumerate(used_rates):
                ws_summary.row_dimensions[row_cursor].height = 21
                ws_summary.merge_cells(start_row=row_cursor, start_column=5, end_row=row_cursor, end_column=11)
                row_fill = PatternFill('solid', fgColor=COLOR_ZEBRA_BG if idx % 2 else 'FFFFFF')
                c1 = _set_text(ws_summary.cell(row=row_cursor, column=1), code)
                c1.alignment = Alignment(horizontal='center', vertical='center')
                c2 = _set_text(ws_summary.cell(row=row_cursor, column=2), base_code)
                c2.alignment = Alignment(horizontal='center', vertical='center')
                c3 = ws_summary.cell(row=row_cursor, column=3, value=Decimal(str(rate_info['rate'])))
                c3.number_format = '0.######'
                c3.alignment = Alignment(horizontal='right', vertical='center')
                c4 = _set_text(ws_summary.cell(row=row_cursor, column=4), rate_info.get('date') or '—')
                c4.alignment = Alignment(horizontal='center', vertical='center')
                c5 = _set_text(ws_summary.cell(row=row_cursor, column=5), fx_source)
                c5.alignment = Alignment(horizontal='left', vertical='center')
                for col_i in range(1, 12):
                    cell = ws_summary.cell(row=row_cursor, column=col_i)
                    cell.font = val_font
                    cell.fill = row_fill
                    cell.border = border_thin
                row_cursor += 1
        if manual_fx_count:
            ws_summary.row_dimensions[row_cursor].height = 22
            ws_summary.merge_cells(start_row=row_cursor, start_column=1, end_row=row_cursor, end_column=11)
            note_cell = _set_text(
                ws_summary.cell(row=row_cursor, column=1),
                f'手动汇率说明：范围内有 {manual_fx_count} 笔外币账目使用了记账时手动指定的汇率，折算汇总与明细优先按指定汇率计算（详见「收支明细」汇率列）。',
            )
            note_cell.font = Font(name=FONT_FAMILY, size=9.5, color=COLOR_MUTED)
            note_cell.fill = PatternFill('solid', fgColor=COLOR_ZEBRA_BG)
            note_cell.alignment = Alignment(horizontal='left', vertical='center')
            for col_i in range(1, 12):
                ws_summary.cell(row=row_cursor, column=col_i).border = border_thin
            row_cursor += 1

    # =========================================================================
    # Sheet 5 pre-allocation (created at the end, but we compute gallery rows first
    # so Sheet 2 can link straight to `#收据凭证!A<row>`)
    # =========================================================================
    has_receipts_sheet = bool(filters['include_receipts'] and receipt_entries)
    if has_receipts_sheet:
        for idx, info in enumerate(receipt_entries, start=2):
            info['gallery_row'] = idx

    # =========================================================================
    # Sheet 2: 收支明细
    # =========================================================================
    ws_entries = wb.create_sheet(title='收支明细')
    _apply_print_setup(ws_entries, landscape=True)

    entry_headers = [
        '编号', '日期', '业务类型', '用途 / 说明', '成员 / 经手人',
        '金额（原币种）', '币种', f'参考换算（{base_code}）', '状态',
        '累计已报销（原币种）', '剩余待报销（原币种）', '来源',
        '收据预览', '凭证文件',
        '原金额（原币种）', '手续费方式', '手续费（原币种）',
        '汇率方式', '指定汇率',
        '备注', '处理 / 付款备注', '最近付款时间', '创建人',
    ]
    _style_table_header(ws_entries, 1, entry_headers)
    ws_entries.freeze_panes = 'A2'

    rate_map = report_data['fx'].get('rates') or {}
    for row_idx, entry in enumerate(entries, start=2):
        row_fill = PatternFill('solid', fgColor=COLOR_ZEBRA_BG if row_idx % 2 == 1 else 'FFFFFF')
        disp_status = _display_status(entry)
        is_advance = entry['kind'] == 'expense' and entry['expense_type'] == 'advance'
        amt_dec = _minor_to_decimal(entry['amount_minor'], entry['decimals'])
        gross_dec = _minor_to_decimal(entry.get('gross_amount_minor') or entry['amount_minor'], entry['decimals'])
        fee_dec = _minor_to_decimal(entry.get('fee_minor') or 0, entry['decimals'])
        paid_dec = _minor_to_decimal(entry['reimbursed_minor'], entry['decimals'])
        rem_dec = _minor_to_decimal(entry['remaining_minor'], entry['decimals'])

        converted_dec = None
        if entry['status'] != 'voided':
            eff_rate, _ = fx.effective_rate(entry, base_code, rate_map)
            if eff_rate is not None:
                converted_dec = (amt_dec * eff_rate).quantize(
                    base_quantum, rounding=ROUND_HALF_UP
                )

        c_id = ws_entries.cell(row=row_idx, column=1, value=entry['id'])
        c_id.number_format = '0'
        c_id.alignment = Alignment(horizontal='center', vertical='center')

        c_date = _set_text(ws_entries.cell(row=row_idx, column=2), entry['date'])
        c_date.alignment = Alignment(horizontal='center', vertical='center')

        c_type = _set_text(ws_entries.cell(row=row_idx, column=3), _entry_type_label(entry))
        c_type.alignment = Alignment(horizontal='center', vertical='center')

        c_desc = _set_text(ws_entries.cell(row=row_idx, column=4), entry['description'])
        c_desc.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)

        c_payer = _set_text(ws_entries.cell(row=row_idx, column=5), entry['payer_name'])
        c_payer.alignment = Alignment(horizontal='left', vertical='center')

        c_amt = _set_money(ws_entries.cell(row=row_idx, column=6), amt_dec, entry['decimals'])
        c_amt.alignment = Alignment(horizontal='right', vertical='center')

        c_cur = _set_text(ws_entries.cell(row=row_idx, column=7), entry['currency'])
        c_cur.alignment = Alignment(horizontal='center', vertical='center')

        c_conv = _set_money(ws_entries.cell(row=row_idx, column=8), converted_dec, base_decimals)
        c_conv.alignment = Alignment(horizontal='right', vertical='center')

        c_status = _set_text(ws_entries.cell(row=row_idx, column=9), STATUS_LABELS.get(disp_status, disp_status))
        c_status.alignment = Alignment(horizontal='center', vertical='center')

        c_paid = _set_money(ws_entries.cell(row=row_idx, column=10), paid_dec if is_advance else None, entry['decimals'])
        c_paid.alignment = Alignment(horizontal='right', vertical='center')

        c_rem = _set_money(
            ws_entries.cell(row=row_idx, column=11),
            rem_dec if (is_advance and entry['status'] in ('pending', 'reimbursed')) else None,
            entry['decimals'],
        )
        c_rem.alignment = Alignment(horizontal='right', vertical='center')

        c_src = _set_text(ws_entries.cell(row=row_idx, column=12), SOURCE_LABELS.get(entry['source'], entry['source']))
        c_src.alignment = Alignment(horizontal='center', vertical='center')

        # Columns 13 & 14: Receipt thumbnail preview & receipt file info/link
        c_preview = ws_entries.cell(row=row_idx, column=13)
        c_preview.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        c_file = ws_entries.cell(row=row_idx, column=14)
        c_file.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)

        row_height = 24
        rinfo = receipt_info.get(entry['id'])
        if rinfo:
            orig_name = rinfo['orig_name']
            if rinfo['thumb'] is not None:
                thumb_buf, tw, th = rinfo['thumb']
                _set_text(c_preview, '')
                _attach_image_in_cell(ws_entries, thumb_buf, tw, th, row_idx, 13, pad_x=6, pad_y=5)
                row_height = max(row_height, round((th + 12) * 0.75, 1))
                _set_text(c_file, orig_name)
                if rinfo['gallery_row']:
                    c_file.hyperlink = f"#'收据凭证'!A{rinfo['gallery_row']}"
            elif rinfo['media_type'] == 'application/pdf':
                _set_text(c_preview, 'PDF 凭证')
                _set_text(c_file, orig_name)
                if public_url:
                    c_file.hyperlink = f'{public_url}/api/entries/{entry["id"]}/receipt'
                elif rinfo['gallery_row']:
                    c_file.hyperlink = f"#'收据凭证'!A{rinfo['gallery_row']}"
            elif not filters['include_receipts']:
                _set_text(c_preview, '已上传（未内嵌）')
                _set_text(c_file, orig_name)
            else:
                _set_text(c_preview, '图片不可预览' if rinfo['path'] else '凭证文件缺失')
                _set_text(c_file, orig_name)
        else:
            _set_text(c_preview, '—')
            _set_text(c_file, '')

        c_gross = _set_money(ws_entries.cell(row=row_idx, column=15), gross_dec, entry['decimals'])
        c_gross.alignment = Alignment(horizontal='right', vertical='center')

        c_fmode = _set_text(ws_entries.cell(row=row_idx, column=16), _fee_mode_label(entry))
        c_fmode.alignment = Alignment(horizontal='center', vertical='center')

        c_fee = _set_money(ws_entries.cell(row=row_idx, column=17), fee_dec, entry['decimals'])
        c_fee.alignment = Alignment(horizontal='right', vertical='center')

        has_manual_fx = bool(entry.get('fx_rate') and entry.get('fx_currency'))
        fx_mode_label = (f'手动指定（{entry["fx_currency"]}）' if has_manual_fx
                         else ('—' if entry['currency'] == base_code else '自动参考'))
        c_fxmode = _set_text(ws_entries.cell(row=row_idx, column=18), fx_mode_label)
        c_fxmode.alignment = Alignment(horizontal='center', vertical='center')

        c_fxrate = ws_entries.cell(row=row_idx, column=19, value=Decimal(str(entry['fx_rate'])) if has_manual_fx else None)
        if has_manual_fx:
            c_fxrate.number_format = '0.00######'
        c_fxrate.alignment = Alignment(horizontal='right', vertical='center')

        c_note = _set_text(ws_entries.cell(row=row_idx, column=20), entry['note'])
        c_note.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)

        c_rnote = _set_text(ws_entries.cell(row=row_idx, column=21), entry['review_note'])
        c_rnote.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)

        c_paid_at = _set_text(ws_entries.cell(row=row_idx, column=22), _format_timestamp(entry['paid_at']))
        c_paid_at.alignment = Alignment(horizontal='center', vertical='center')

        c_creator = _set_text(ws_entries.cell(row=row_idx, column=23), entry['creator_name'])
        c_creator.alignment = Alignment(horizontal='left', vertical='center')

        ws_entries.row_dimensions[row_idx].height = row_height

        for col_i in range(1, len(entry_headers) + 1):
            cell = ws_entries.cell(row=row_idx, column=col_i)
            cell.fill = row_fill
            cell.border = border_thin
            cell.font = Font(
                name=FONT_FAMILY,
                size=10,
                bold=(col_i == 6),
                color=COLOR_LINK if (col_i == 14 and cell.hyperlink) else COLOR_INK,
                underline='single' if (col_i == 14 and cell.hyperlink) else None,
            )

        # Status badge tint
        st_bg, st_fg = STATUS_STYLE.get(disp_status, ('F4F4F5', COLOR_INK))
        c_status.fill = PatternFill('solid', fgColor=st_bg)
        c_status.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=st_fg)

    if entries:
        ws_entries.auto_filter.ref = f'A1:{get_column_letter(len(entry_headers))}{len(entries) + 1}'
    else:
        ws_entries.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(entry_headers))
        empty_cell = _set_text(ws_entries.cell(row=2, column=1), '当前导出范围内暂无账目记录')
        empty_cell.font = Font(name=FONT_FAMILY, size=10, color=COLOR_MUTED)
        empty_cell.alignment = Alignment(horizontal='center', vertical='center')
        ws_entries.row_dimensions[2].height = 28

    # =========================================================================
    # Sheet 3: 逐次报销付款
    # =========================================================================
    ws_payments = wb.create_sheet(title='逐次报销付款')
    _apply_print_setup(ws_payments, landscape=True)

    payment_headers = [
        '付款编号', '账目编号', '账目日期', '用途 / 说明', '垫付成员',
        '付款时间', '操作人', '实付金额', '实付币种', '抵扣原币金额',
        '原账目币种', '锁定汇率', '汇率日期', '付款方式 / 参考号',
    ]
    _style_table_header(ws_payments, 1, payment_headers)
    ws_payments.freeze_panes = 'A2'

    for row_idx, (entry, p) in enumerate(payments_flat, start=2):
        ws_payments.row_dimensions[row_idx].height = 22
        row_fill = PatternFill('solid', fgColor=COLOR_ZEBRA_BG if row_idx % 2 == 1 else 'FFFFFF')
        pay_dec = _minor_to_decimal(p['amount_minor'], p['decimals'])
        entry_dec = _minor_to_decimal(p['entry_amount_minor'], entry['decimals'])

        ws_payments.cell(row=row_idx, column=1, value=p['id']).number_format = '0'
        ws_payments.cell(row=row_idx, column=2, value=entry['id']).number_format = '0'
        _set_text(ws_payments.cell(row=row_idx, column=3), entry['date'])
        _set_text(ws_payments.cell(row=row_idx, column=4), entry['description'])
        _set_text(ws_payments.cell(row=row_idx, column=5), entry['payer_name'])
        _set_text(ws_payments.cell(row=row_idx, column=6), _format_timestamp(p['created_at']))
        _set_text(ws_payments.cell(row=row_idx, column=7), p['name'])
        _set_money(ws_payments.cell(row=row_idx, column=8), pay_dec, p['decimals'])
        _set_text(ws_payments.cell(row=row_idx, column=9), p['currency'])
        _set_money(ws_payments.cell(row=row_idx, column=10), entry_dec, entry['decimals'])
        _set_text(ws_payments.cell(row=row_idx, column=11), entry['currency'])
        rate_cell = ws_payments.cell(row=row_idx, column=12, value=Decimal(str(p['fx_rate'])))
        rate_cell.number_format = '0.######'
        _set_text(ws_payments.cell(row=row_idx, column=13), p['fx_date'] or ('手动汇率' if p['currency'] != entry['currency'] else '—'))
        _set_text(ws_payments.cell(row=row_idx, column=14), p['note'])

        for col_i in range(1, len(payment_headers) + 1):
            cell = ws_payments.cell(row=row_idx, column=col_i)
            cell.fill = row_fill
            cell.border = border_thin
            cell.font = Font(name=FONT_FAMILY, size=10, bold=(col_i == 8), color=COLOR_INK)
            if col_i in (1, 2, 3, 6, 9, 11, 13):
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif col_i in (8, 10, 12):
                cell.alignment = Alignment(horizontal='right', vertical='center')
            else:
                cell.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)

    if payments_flat:
        ws_payments.auto_filter.ref = f'A1:{get_column_letter(len(payment_headers))}{len(payments_flat) + 1}'
    else:
        ws_payments.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(payment_headers))
        empty_cell = _set_text(ws_payments.cell(row=2, column=1), '当前导出范围内暂无报销付款记录')
        empty_cell.font = Font(name=FONT_FAMILY, size=10, color=COLOR_MUTED)
        empty_cell.alignment = Alignment(horizontal='center', vertical='center')
        ws_payments.row_dimensions[2].height = 28

    # =========================================================================
    # Sheet 4: 成员待报销
    # =========================================================================
    ws_pending = wb.create_sheet(title='成员待报销')
    _apply_print_setup(ws_pending, landscape=True)

    pending_entries = [
        e for e in entries
        if e['kind'] == 'expense' and e['expense_type'] == 'advance' and e['status'] == 'pending'
    ]
    suggestion_map = {}
    for m in report_data.get('members') or []:
        for item in m.get('entries') or []:
            suggestion_map[item['id']] = item

    pending_headers = [
        '垫付成员', '账目编号', '发生日期', '用途 / 说明',
        '原金额', '累计已报销', '剩余待报销（原币种）', '币种',
        f'参考换算（{base_code}）', f'建议安排付款（{base_code}）', '安排状态', '收据状态', '其中手续费', '备注',
    ]
    _style_table_header(ws_pending, 1, pending_headers)
    ws_pending.freeze_panes = 'A2'

    pending_sorted = sorted(pending_entries, key=lambda e: (e['payer_name'], e['date'], e['id']))
    for row_idx, entry in enumerate(pending_sorted, start=2):
        ws_pending.row_dimensions[row_idx].height = 22
        row_fill = PatternFill('solid', fgColor=COLOR_ZEBRA_BG if row_idx % 2 == 1 else 'FFFFFF')
        orig_dec = _minor_to_decimal(entry['amount_minor'], entry['decimals'])
        fee_dec = _minor_to_decimal(entry.get('fee_minor') or 0, entry['decimals'])
        paid_dec = _minor_to_decimal(entry['reimbursed_minor'], entry['decimals'])
        rem_dec = _minor_to_decimal(entry['remaining_minor'], entry['decimals'])
        plan_item = suggestion_map.get(entry['id'])

        _set_text(ws_pending.cell(row=row_idx, column=1), entry['payer_name'])
        ws_pending.cell(row=row_idx, column=2, value=entry['id']).number_format = '0'
        _set_text(ws_pending.cell(row=row_idx, column=3), entry['date'])
        _set_text(ws_pending.cell(row=row_idx, column=4), entry['description'])
        _set_money(ws_pending.cell(row=row_idx, column=5), orig_dec, entry['decimals'])
        _set_money(ws_pending.cell(row=row_idx, column=6), paid_dec, entry['decimals'])
        _set_money(ws_pending.cell(row=row_idx, column=7), rem_dec, entry['decimals'])
        _set_text(ws_pending.cell(row=row_idx, column=8), entry['currency'])

        if plan_item:
            _set_money(ws_pending.cell(row=row_idx, column=9), plan_item['settlement'], base_decimals)
            _set_money(ws_pending.cell(row=row_idx, column=10), plan_item['suggested'], base_decimals)
            sug_val = Decimal(plan_item['suggested'])
            plan_status = '建议全额' if (sug_val > 0 and plan_item['fully_covered']) else ('部分安排' if sug_val > 0 else '暂不安排')
            _set_text(ws_pending.cell(row=row_idx, column=11), plan_status)
        else:
            ws_pending.cell(row=row_idx, column=9, value=None)
            ws_pending.cell(row=row_idx, column=10, value=None)
            _set_text(ws_pending.cell(row=row_idx, column=11), '等待汇率')

        rinfo = receipt_info.get(entry['id'])
        if not rinfo:
            r_label = '无凭证'
        elif rinfo['media_type'] == 'application/pdf':
            r_label = 'PDF 凭证'
        else:
            r_label = '图片凭证'
        _set_text(ws_pending.cell(row=row_idx, column=12), r_label)
        _set_money(ws_pending.cell(row=row_idx, column=13), fee_dec, entry['decimals'])
        _set_text(ws_pending.cell(row=row_idx, column=14), entry['note'])

        for col_i in range(1, len(pending_headers) + 1):
            cell = ws_pending.cell(row=row_idx, column=col_i)
            cell.fill = row_fill
            cell.border = border_thin
            cell.font = Font(name=FONT_FAMILY, size=10, bold=(col_i == 7), color=COLOR_INK)
            if col_i in (2, 3, 8, 11, 12):
                cell.alignment = Alignment(horizontal='center', vertical='center')
            elif col_i in (5, 6, 7, 9, 10, 13):
                cell.alignment = Alignment(horizontal='right', vertical='center')
            else:
                cell.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)

    if pending_sorted:
        ws_pending.auto_filter.ref = f'A1:{get_column_letter(len(pending_headers))}{len(pending_sorted) + 1}'
    else:
        ws_pending.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(pending_headers))
        empty_cell = _set_text(ws_pending.cell(row=2, column=1), '当前导出范围内暂无待报销垫付项目')
        empty_cell.font = Font(name=FONT_FAMILY, size=10, color=COLOR_MUTED)
        empty_cell.alignment = Alignment(horizontal='center', vertical='center')
        ws_pending.row_dimensions[2].height = 28

    # =========================================================================
    # Sheet 5: 收据凭证 (large embedded receipt gallery when receipts exist)
    # =========================================================================
    if has_receipts_sheet:
        ws_receipts = wb.create_sheet(title='收据凭证')
        _apply_print_setup(ws_receipts, landscape=True)

        receipt_headers = [
            '账目编号', '日期', '业务类型', '用途 / 说明', '成员 / 经手人',
            '金额（原币种）', '币种', '凭证类型', '原始文件名', '收据大图预览',
        ]
        _style_table_header(ws_receipts, 1, receipt_headers)
        ws_receipts.freeze_panes = 'A2'

        for row_idx, rinfo in enumerate(receipt_entries, start=2):
            entry = rinfo['entry']
            row_fill = PatternFill('solid', fgColor=COLOR_ZEBRA_BG if row_idx % 2 == 1 else 'FFFFFF')
            amt_dec = _minor_to_decimal(entry['amount_minor'], entry['decimals'])

            ws_receipts.cell(row=row_idx, column=1, value=entry['id']).number_format = '0'
            _set_text(ws_receipts.cell(row=row_idx, column=2), entry['date'])
            _set_text(ws_receipts.cell(row=row_idx, column=3), _entry_type_label(entry))
            _set_text(ws_receipts.cell(row=row_idx, column=4), entry['description'])
            _set_text(ws_receipts.cell(row=row_idx, column=5), entry['payer_name'])
            _set_money(ws_receipts.cell(row=row_idx, column=6), amt_dec, entry['decimals'])
            _set_text(ws_receipts.cell(row=row_idx, column=7), entry['currency'])

            media_label = 'PDF 文件' if rinfo['media_type'] == 'application/pdf' else '图片'
            _set_text(ws_receipts.cell(row=row_idx, column=8), media_label)
            c_name = _set_text(ws_receipts.cell(row=row_idx, column=9), rinfo['orig_name'])
            c_big = ws_receipts.cell(row=row_idx, column=10)

            row_height = 28
            if rinfo['gallery'] is not None:
                gal_buf, gw, gh = rinfo['gallery']
                _set_text(c_big, '')
                _attach_image_in_cell(ws_receipts, gal_buf, gw, gh, row_idx, 10, pad_x=8, pad_y=8)
                row_height = max(row_height, round((gh + 18) * 0.75, 1))
            elif rinfo['media_type'] == 'application/pdf':
                if public_url:
                    _set_text(c_big, f'点击在线查看 PDF：{rinfo["orig_name"]}')
                    c_big.hyperlink = f'{public_url}/api/entries/{entry["id"]}/receipt'
                else:
                    _set_text(c_big, f'PDF 凭证（Excel 不支持直接内嵌 PDF 页面，请在网页端打开账目 #{entry["id"]} 查看原件）')
            else:
                _set_text(c_big, '图片无法解码或原文件缺失，请在网页端核对' if rinfo['path'] else '凭证原文件缺失，请检查数据目录或备份')

            ws_receipts.row_dimensions[row_idx].height = row_height

            for col_i in range(1, len(receipt_headers) + 1):
                cell = ws_receipts.cell(row=row_idx, column=col_i)
                cell.fill = row_fill
                cell.border = border_thin
                is_link = bool(cell.hyperlink)
                cell.font = Font(
                    name=FONT_FAMILY,
                    size=10,
                    bold=(col_i == 6),
                    color=COLOR_LINK if is_link else COLOR_INK,
                    underline='single' if is_link else None,
                )
                if col_i in (1, 2, 3, 7, 8):
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                elif col_i == 6:
                    cell.alignment = Alignment(horizontal='right', vertical='center')
                else:
                    cell.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)

        _auto_fit_columns(
            ws_receipts,
            min_widths={1: 10, 2: 13, 3: 12, 4: 20, 5: 14, 6: 15, 7: 9, 8: 11, 9: 22},
            max_widths={4: 34, 9: 34},
            skip_cols={10},
        )
        # Column 10 fits the 520px gallery image cleanly (~76 Excel char units).
        ws_receipts.column_dimensions['J'].width = 76

    # Auto-fit column widths across sheets (keeping image columns fixed to fit thumbnails cleanly).
    _auto_fit_columns(
        ws_summary,
        min_widths={1: 24, 2: 16, 3: 16, 4: 16, 5: 16, 6: 16, 7: 16, 8: 16, 9: 18, 10: 16, 11: 12},
        max_widths={1: 32, 2: 22, 3: 22, 4: 22, 5: 22, 6: 22, 7: 22, 8: 22, 9: 24, 10: 22, 11: 14},
    )
    _auto_fit_columns(
        ws_entries,
        min_widths={1: 9, 2: 13, 3: 12, 4: 22, 5: 14, 6: 16, 7: 9, 8: 16, 9: 12, 10: 16, 11: 16, 12: 10, 14: 18, 15: 16, 16: 14, 17: 16, 18: 16, 19: 14, 20: 20, 21: 20, 22: 20, 23: 13},
        max_widths={4: 36, 14: 28, 20: 36, 21: 32},
        skip_cols={13},
    )
    ws_entries.column_dimensions['M'].width = 24 if filters['include_receipts'] else 16

    _auto_fit_columns(
        ws_payments,
        min_widths={1: 10, 2: 10, 3: 13, 4: 20, 5: 14, 6: 20, 7: 14, 8: 15, 9: 10, 10: 15, 11: 11, 12: 12, 13: 13, 14: 24},
        max_widths={4: 34, 14: 38},
    )
    _auto_fit_columns(
        ws_pending,
        min_widths={1: 14, 2: 10, 3: 13, 4: 22, 5: 14, 6: 14, 7: 16, 8: 9, 9: 16, 10: 16, 11: 12, 12: 12, 13: 14, 14: 22},
        max_widths={4: 34, 14: 34},
    )

    output = io.BytesIO()
    wb.save(output)
    content = output.getvalue()
    # Close buffers only after wb.save has serialized all images.
    for buf in image_buffers:
        buf.close()

    display_filename, ascii_filename = _build_filenames(team_name, filters)
    return {
        'content': content,
        'filename': display_filename,
        'ascii_filename': ascii_filename,
        'entry_count': len(entries),
        'payment_count': len(payments_flat),
        'receipt_count': len(receipt_entries),
        'embedded_receipt_count': embedded_count,
        'pdf_receipt_count': pdf_count,
        'scope_label': scope_label,
        'base': base_code,
        'filters': filters,
    }
