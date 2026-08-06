"""Рендер per-HAWB реестра ДТЭГ в PDF (reportlab).

Вход — структура из dteg_reestr.reestr_data_for_hawb(hawb) + необязательные
доп.поля (master_awb, registration_number, release_datetime, inspector), которых
нет в подаче ДТЭГ (номер регистрации/штамп выпуска/инспектор берутся из БД/выпуска).

Документ — «выборочная печать» госформы ДТЭГ, отфильтрованная на ОДНУ накладную:
шапка (отпр/получ по документу, рег.№, режим, таможня) → таблица товаров с
документами гр.44 → «Всего по инд. накладной» + штамп выпуска → «Всего по ДТЭГ» →
B (декларант) → C (решение) → D (платежи).

Кириллица — через системный Arial (Windows). Fallback — Helvetica (латиница).
"""
from __future__ import annotations

import io
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)

FONT = 'Helvetica'
FONT_BOLD = 'Helvetica-Bold'
_FONTS_READY = False

_ARIAL_CANDIDATES = [
    (r'C:\Windows\Fonts\arial.ttf', r'C:\Windows\Fonts\arialbd.ttf'),
    (os.path.join(os.path.dirname(__file__), 'fonts', 'DejaVuSans.ttf'),
     os.path.join(os.path.dirname(__file__), 'fonts', 'DejaVuSans-Bold.ttf')),
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
     '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
]


def _ensure_fonts() -> None:
    """Регистрирует кириллический TTF один раз. Тихо откатывается на Helvetica."""
    global FONT, FONT_BOLD, _FONTS_READY
    if _FONTS_READY:
        return
    _FONTS_READY = True
    for regular, bold in _ARIAL_CANDIDATES:
        if not os.path.exists(regular):
            continue
        try:
            pdfmetrics.registerFont(TTFont('CargoSans', regular))
            FONT = 'CargoSans'
            if bold and os.path.exists(bold):
                pdfmetrics.registerFont(TTFont('CargoSans-Bold', bold))
                FONT_BOLD = 'CargoSans-Bold'
            else:
                FONT_BOLD = 'CargoSans'
            return
        except Exception:
            continue


def _fmt_dt(value: str) -> str:
    """ISO-дату/время → человекочитаемо (без TZ-хвоста)."""
    if not value:
        return ''
    v = value.replace('T', ' ')
    for cut in ('+', 'Z'):
        i = v.find(cut)
        if i > 0:
            v = v[:i]
    return v.strip()


def _esc(text) -> str:
    """Экранирует СЫРЫЕ данные для вставки в reportlab-разметку (<b>, <br/> — свои)."""
    from xml.sax.saxutils import escape
    return escape('' if text is None else str(text))


def _para(markup: str, style):
    """Paragraph из ГОТОВОЙ разметки (данные внутри уже экранированы _esc)."""
    return Paragraph(markup or '&nbsp;', style)


def render_reestr_pdf(data: dict, *, master_awb: str = '', registration_number: str = '',
                      release_datetime: str = '', inspector: str = '') -> bytes:
    """Рендерит реестр ОДНОЙ накладной в PDF. Возвращает bytes."""
    _ensure_fonts()
    dec = data['declaration']
    hs = data['house_shipment']

    body = ParagraphStyle('body', fontName=FONT, fontSize=7, leading=8.5)
    small = ParagraphStyle('small', fontName=FONT, fontSize=6, leading=7.2)
    bold = ParagraphStyle('bold', fontName=FONT_BOLD, fontSize=7, leading=8.5)
    hdr = ParagraphStyle('hdr', fontName=FONT_BOLD, fontSize=6.5, leading=7.5,
                         alignment=1, textColor=colors.black)
    title = ParagraphStyle('title', fontName=FONT_BOLD, fontSize=11, leading=13,
                           alignment=1, spaceAfter=4)
    label = ParagraphStyle('label', fontName=FONT, fontSize=6, leading=7,
                           textColor=colors.HexColor('#555555'))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=8 * mm, rightMargin=8 * mm,
        topMargin=8 * mm, bottomMargin=8 * mm,
        title=f"Реестр ДТЭГ {hs.get('waybill','')}",
    )
    els = []
    PW = doc.width  # рабочая ширина

    reg_kind = dec.get('registry_kind') or 'ДТЭГ'
    els.append(_para(f"Реестр по единой декларации на товары экспресс-груза ({_esc(reg_kind)})", title))
    if registration_number:
        els.append(_para(f"Регистрационный номер: <b>{_esc(registration_number)}</b>", hdr))
    els.append(Spacer(1, 3))

    # --- Шапка: отправитель / получатель по всему документу ---
    def subj_cell(lbl, s):
        lines = [f"<b>{_esc(lbl)}</b>", _esc(s.get('name', ''))]
        inn = s.get('inn', '')
        if inn:
            lines.append(f"ИНН/КПП: {_esc(inn)} / {_esc(s.get('kpp',''))}")
        if s.get('passport'):
            lines.append(_esc(s['passport']))
        if s.get('address'):
            lines.append(_esc(s['address']))
        if s.get('phone'):
            lines.append(f"тел.: {_esc(s['phone'])}")
        return _para('<br/>'.join(x for x in lines if x), small)

    head = Table([[
        subj_cell('Отправитель (по всему документу):', dec.get('consignor', {})),
        subj_cell('Получатель (по всему документу):', dec.get('consignee', {})),
    ]], colWidths=[PW * 0.5, PW * 0.5])
    head.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    els.append(head)

    meta_line = (f"Направление: <b>{_esc(dec.get('decl_kind',''))}</b> &nbsp; "
                 f"Таможенный режим: <b>{_esc(dec.get('customs_mode',''))}</b> &nbsp; "
                 f"Таможня/пост: <b>{_esc(dec.get('customs_office',''))}</b> &nbsp; "
                 f"Место: {_esc(dec.get('customs_place',''))}")
    els.append(_para(meta_line, small))
    els.append(Spacer(1, 4))

    # --- Блок A: сведения по конкретной накладной ---
    els.append(_para(
        f"A. Индивидуальная накладная № <b>{_esc(hs.get('waybill',''))}</b>"
        + (f" &nbsp; (общая накладная: {_esc(master_awb)})" if master_awb else '')
        + f" &nbsp; порядковый № в ДТЭГ: {_esc(hs.get('ordinal',''))}", bold))
    ind = Table([[
        subj_cell('Индивид. отправитель:', hs.get('consignor', {})),
        subj_cell('Индивид. получатель:', hs.get('consignee', {})),
    ]], colWidths=[PW * 0.5, PW * 0.5])
    ind.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    els.append(ind)
    els.append(Spacer(1, 3))

    # --- Таблица товаров ---
    cols = ['N', 'Наименование товара', 'ТН ВЭД', 'Кол-во', 'Вес брутто, кг',
            'Стоимость (гр.12)', 'Там. стоимость (гр.13)', 'Документы (гр.44): код / дата / номер']
    cw = [PW * x for x in (0.03, 0.30, 0.075, 0.06, 0.07, 0.09, 0.09, 0.275)]
    rows = [[_para(_esc(c), hdr) for c in cols]]
    for g in hs.get('goods', []):
        vals = g.get('values') or []
        v12 = vals[0] if len(vals) >= 1 else {'value': g.get('value', ''), 'currency': g.get('currency', '')}
        v13 = vals[1] if len(vals) >= 2 else {}
        docs_lines = []
        for d in g.get('documents', []):
            code = d.get('kind', '')
            dd = _fmt_dt(d.get('date', ''))
            num = d.get('number', '')
            docs_lines.append(_esc(f"{code} / {dd} / {num}".strip(' /')))
        rows.append([
            _para(_esc(g.get('num', '')), body),
            _para(_esc(g.get('description', '')), small),
            _para(_esc(g.get('tnved', '')), body),
            _para(_esc(f"{g.get('qty','')} {g.get('qty_unit','')}".strip()), body),
            _para(_esc(g.get('gross_kg', '')), body),
            _para(_esc(f"{v12.get('currency','')} {v12.get('value','')}".strip()), body),
            _para(_esc(f"{v13.get('currency','')} {v13.get('value','')}".strip()), body),
            _para('<br/>'.join(docs_lines), small),
        ])
    gt = Table(rows, colWidths=cw, repeatRows=1)
    gt.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8e8e8')),
        ('LEFTPADDING', (0, 0), (-1, -1), 2), ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    els.append(gt)
    els.append(Spacer(1, 3))

    # --- Итог по накладной + штамп выпуска ---
    stamp = ''
    if release_datetime:
        stamp = (f"<b>10 — Выпуск товаров разрешён.</b> Дата принятия решения: "
                 f"{_esc(_fmt_dt(release_datetime))}"
                 + (f" &nbsp; Инспектор: {_esc(inspector)}" if inspector else ''))
    tot_ind = Table([[
        _para(f"Всего по индивидуальной накладной: масса брутто {_esc(hs.get('total_gross_kg',''))} кг; "
              f"стоимость {_esc(hs.get('total_currency',''))} {_esc(hs.get('total_value',''))}", bold),
        _para(stamp, small),
    ]], colWidths=[PW * 0.45, PW * 0.55])
    tot_ind.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    els.append(tot_ind)
    els.append(Spacer(1, 2))

    tg = dec.get('total_gross', {})
    tv = dec.get('total_value', {})
    cc = dec.get('customs_cost', {})
    els.append(_para(
        f"<b>Всего по ДТЭГ:</b> масса брутто {_esc(tg.get('value',''))} {_esc(tg.get('unit',''))}; "
        f"стоимость {_esc(tv.get('currency',''))} {_esc(tv.get('value',''))}; "
        f"таможенная стоимость {_esc(cc.get('currency',''))} {_esc(cc.get('value',''))}", bold))
    els.append(Spacer(1, 5))

    # --- Блок B: декларант ---
    br = dec.get('broker', {})
    sig = dec.get('signatory', {})
    pas = sig.get('passport', {})
    poa = sig.get('power_of_attorney', {})
    fio = ' '.join(x for x in (sig.get('surname', ''), sig.get('name', ''), sig.get('middle', '')) if x)
    b_lines = [
        f"<b>B. Декларант (таможенный представитель):</b> {_esc(br.get('name',''))} "
        f"(реестр {_esc(br.get('registry_doc_kind',''))} № {_esc(br.get('registry_number',''))})",
        f"Подписант: {_esc(fio)} — {_esc(sig.get('post',''))}",
    ]
    if pas:
        b_lines.append(
            f"Документ, удостоверяющий личность: {_esc(pas.get('name',''))} "
            f"{_esc(pas.get('series',''))} № {_esc(pas.get('number',''))} от {_esc(_fmt_dt(pas.get('date','')))}, "
            f"{_esc(pas.get('issued_by',''))}")
    if poa:
        b_lines.append(
            f"Документ о полномочиях (код {_esc(poa.get('kind',''))}): № {_esc(poa.get('number',''))} "
            f"от {_esc(_fmt_dt(poa.get('date','')))}, действ. до {_esc(_fmt_dt(poa.get('valid','')))}")
    els.append(_para('<br/>'.join(b_lines), small))
    els.append(Spacer(1, 3))

    # --- Блок C: решение о выпуске ---
    c_txt = "<b>C. Решение о выпуске:</b> "
    c_txt += (f"10 — Выпуск разрешён, {_esc(_fmt_dt(release_datetime))}" if release_datetime
              else "— (нет данных о выпуске)")
    if inspector:
        c_txt += f" &nbsp; Должностное лицо: {_esc(inspector)}"
    els.append(_para(c_txt, small))
    els.append(Spacer(1, 3))

    # --- Блок D: платежи ---
    tp = dec.get('total_payment', {})
    els.append(_para(
        f"<b>D. Таможенные платежи (всего по ДТЭГ):</b> "
        f"{_esc(tp.get('value','') or '—')} {_esc(tp.get('currency',''))}", small))

    doc.build(els)
    return buf.getvalue()
