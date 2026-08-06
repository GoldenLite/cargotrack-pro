"""Рендер per-HAWB реестра ДТЭГ РОДНЫМ XSLT-шаблоном Альты → HTML → PDF.

Почему так, а не reportlab: ручной бланк «сильно отличается» от бланка Альты.
Здесь берём НАСТОЯЩИЙ шаблон Альты `_EXPRESSCARGODECLARATION4.XSLT` (XSLT 1.0,
вывод HTML) и прогоняем через него наш raw_xml подачи → пиксель-в-пиксель
«Декларация на товары для экспресс-грузов».

Отметки о выпуске (ОБЯЗАТЕЛЬНЫ) в подаче отсутствуют — инжектим их в XML перед
трансформацией из НАШЕЙ БД (release_date, номер ДТ, решение) и обогащаем из
CMN.11350 (ExpressCargoDeclarationCustomMark: инспектор/ЛНП/точный рег.№):
  • по каждой накладной: <ecd:Design> + <ecd:TaxBase_DecisionDate> (штамп кол.13-15);
  • блок C: второй <ecd:GoodsShipment>/HouseShipment[1] с Design+DecisionDate;
  • <ecd:Inspector> + <ecd:LNP> (дети ExpressCargoDeclaration);
  • параметр $ExpressCargoDeclarationCustomMark → рег.№ (ApplicationRegNumber).

Выборочная печать (одна накладная): удаляем прочие HouseShipment из
GoodsShipment[1]; «Всего по ДТЭГ» не ломается (XSLT берёт его из агрегата уровня
GoodsShipment). Лишнюю пустую строку «Всего по инд.накладной» от GoodsShipment[2]
чистим пост-обработкой HTML.

HTML→PDF: Microsoft Edge headless (`--print-to-pdf`) — уже стоит на VPS, Chromium.

См. release-reestr-mailer в памяти.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import uuid

from lxml import etree

NS = 'urn:customs.ru:Information:CustomsDocuments:ExpressCargoDeclaration:5.27.0'
NS_CM = 'urn:customs.ru:Information:CustomsDocuments:ExpressCargoDeclarationCustomMark:5.16.0'
NS_CAT = 'urn:customs.ru:CommonAggregateTypes:5.24.0'  # PrDocumentNumber и т.п.

XSLT_PATH = os.path.join(os.path.dirname(__file__), 'templates_ed',
                         'EXPRESSCARGODECLARATION4.xslt')

# Текст решения о выпуске (ecd:Design) — как в бланке Альты, зависит от режима:
# экспорт (10) — «без уплаты таможенных платежей», импорт (40) — «для свободного
# обращения». Мы шлём только по выпуску (decision_code=10).
RELEASE_DESIGN_BY_MODE = {
    '10': '10-выпуск товаров без уплаты таможенных платежей',     # экспорт
    '40': '10-выпуск товаров разрешен для свободного обращения',  # импорт
}
RELEASE_DESIGN_DEFAULT = '10-выпуск товаров разрешен'

# Сентинел в ObjectOrdinal служебного GoodsShipment[2] (нужен XSLT для блока C):
# for-each товаров рисует по нему ДВЕ артефактные строки (пустая строка товара +
# «Всего по инд.накладной») — вычищаем их из HTML по этому маркеру.
_GS2_SENTINEL = '§C§'

_EDGE_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
]


def _q(tag: str) -> str:
    return '{%s}%s' % (NS, tag)


def _qcm(tag: str) -> str:
    return '{%s}%s' % (NS_CM, tag)


def _edge_exe() -> str | None:
    for p in _EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


# ── источник отметок ─────────────────────────────────────────────────────────

def find_custommark_for_hawb(hawb_number: str):
    """Ищет CMN.11350 (ExpressCargoDeclarationCustomMark) с решением по накладной.

    Возвращает dict{decision_code, decision_date, reg_customs, reg_date, reg_gtd,
    inspector, lnp} или None. Матч по Consignment/IndividualWayBill/PrDocumentNumber.
    """
    from cargo.models import AltaInboxMessage

    def _ln(el):
        return el.tag.rsplit('}', 1)[-1]

    def _find_ln(el, name):
        for e in el.iter():
            if _ln(e) == name:
                return e
        return None

    def _text_ln(el, name):
        e = _find_ln(el, name)
        return (e.text or '').strip() if (e is not None and e.text) else ''

    qs = (AltaInboxMessage.objects
          .filter(msg_type='CMN.11350', raw_xml__contains=hawb_number)
          .order_by('-id')[:20])
    for msg in qs:
        rx = msg.raw_xml or ''
        if 'ExpressCargoDeclarationCustomMark' not in rx:
            continue
        try:
            root = etree.fromstring(rx.encode('utf-8') if isinstance(rx, str) else rx)
        except etree.XMLSyntaxError:
            continue
        cm = None
        for e in root.iter():
            if _ln(e) == 'ExpressCargoDeclarationCustomMark':
                cm = e
                break
        if cm is None:
            continue
        # рег.№ (ApplicationRegNumber → CustomsCode/RegistrationDate/GTDNumber)
        reg = {'customs': '', 'date': '', 'gtd': ''}
        arn = None
        for e in cm.iter():
            if _ln(e) == 'ApplicationRegNumber':
                arn = e
                break
        if arn is not None:
            reg = {'customs': _text_ln(arn, 'CustomsCode'),
                   'date': _text_ln(arn, 'RegistrationDate'),
                   'gtd': _text_ln(arn, 'GTDNumber')}
        # инспектор/ЛНП (CustomsPerson)
        insp, lnp = '', ''
        for e in cm.iter():
            ln = _ln(e)
            if ln == 'PersonName' and not insp:
                insp = (e.text or '').strip()
            elif ln == 'LNP' and not lnp:
                lnp = (e.text or '').strip()
        # нужная накладная + ИМЕННО решение о выпуске (код 10). По одной накладной
        # может быть несколько CMN.11350 (напр. позже код 70 «продлён»/запрос) —
        # для рассылки-по-выпуску берём только выпуск (10), иначе пропускаем.
        for cons in cm.iter():
            if _ln(cons) != 'Consignment':
                continue
            iwb = None
            for e in cons.iter():
                if _ln(e) == 'IndividualWayBill':
                    iwb = e
                    break
            num = _text_ln(iwb, 'PrDocumentNumber') if iwb is not None else ''
            code = _text_ln(cons, 'DecisionCode')
            if num == hawb_number and code == '10':
                return {
                    'decision_code': code,
                    'decision_date': _text_ln(cons, 'DecisionDate'),
                    'reg_customs': reg['customs'], 'reg_date': reg['date'], 'reg_gtd': reg['gtd'],
                    'inspector': insp, 'lnp': lnp,
                }
    return None


def _marks_for_hawb(hawb_number: str) -> dict:
    """Собирает отметки: обязательные из БД + обогащение из CMN.11350."""
    from cargo.models import HouseWaybill
    h = (HouseWaybill.objects.filter(hawb_number=hawb_number)
         .order_by('-id').first())
    release_iso = ''
    reg_full = ''
    if h is not None:
        if h.release_date:
            rd = h.release_date
            # release_date хранится в UTC → показываем в МСК (как Альта).
            if hasattr(rd, 'isoformat'):
                try:
                    from django.utils import timezone as _tz
                    if _tz.is_aware(rd):
                        rd = _tz.localtime(rd)
                except Exception:
                    pass
                release_iso = rd.isoformat()
            else:
                release_iso = str(rd)
        reg_full = h.customs_declaration_number or ''

    cm = find_custommark_for_hawb(hawb_number)
    code = (cm or {}).get('decision_code') or '10'
    return {
        'design': DECISION_TEXT.get(code, DECISION_TEXT['']),
        'decision_date': (cm or {}).get('decision_date') or release_iso,
        'inspector': (cm or {}).get('inspector') or '',
        'lnp': (cm or {}).get('lnp') or '',
        'reg_full': reg_full,          # номер ДТ → ecd:RegNUM (ячейка «A»)
    }


# ── сборка печатного XML ──────────────────────────────────────────────────────

def build_print_ecd(hawb_number: str):
    """Готовит ExpressCargoDeclaration (одна накладная + отметки) для XSLT.

    Возвращает (ecd_element, n_house) или (None, 0).
    """
    from cargo.services.alta.dteg_reestr import find_submission_for_hawb
    rx, _mt, _obs = find_submission_for_hawb(hawb_number)
    if not rx:
        return None, 0
    data = rx.encode('utf-8') if isinstance(rx, str) else rx
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError:
        return None, 0
    ecd = root.find('.//' + _q('ExpressCargoDeclaration'))
    if ecd is None:
        return None, 0
    gs = ecd.find(_q('GoodsShipment'))
    if gs is None:
        return None, 0

    # выборочная печать: оставить только целевую накладную
    houses = gs.findall(_q('HouseShipment'))
    target = None
    for hs in houses:
        hwd = hs.find(_q('HouseWaybillDetails'))
        num = (hwd.findtext('{%s}PrDocumentNumber' % NS_CAT) if hwd is not None else '') or ''
        if num.strip() == hawb_number:
            target = hs
    if target is None:
        target = houses[0] if houses else None
    if target is None:
        return None, 0
    for hs in houses:
        if hs is not target:
            gs.remove(hs)

    marks = _marks_for_hawb(hawb_number)
    # Текст решения — по режиму декларации (как в бланке Альты).
    mode = (ecd.findtext(_q('CustomsModeCode')) or '').strip()
    design = RELEASE_DESIGN_BY_MODE.get(mode, RELEASE_DESIGN_DEFAULT)

    # штамп по накладной (кол.13-15 строки «Всего по инд.накладной»)
    etree.SubElement(target, _q('Design')).text = design
    etree.SubElement(target, _q('TaxBase_DecisionDate')).text = marks['decision_date']

    # блок C: XSLT читает решение из GoodsShipment[2]/HouseShipment[1]. Помечаем
    # его ObjectOrdinal сентинелом — по нему потом чистим артефактные строки.
    gs2 = etree.SubElement(ecd, _q('GoodsShipment'))
    hs2 = etree.SubElement(gs2, _q('HouseShipment'))
    etree.SubElement(hs2, _q('ObjectOrdinal')).text = _GS2_SENTINEL
    etree.SubElement(hs2, _q('Design')).text = design
    etree.SubElement(hs2, _q('TaxBase_DecisionDate')).text = marks['decision_date']

    # инспектор/ЛНП
    if marks['inspector']:
        etree.SubElement(ecd, _q('Inspector')).text = marks['inspector']
    if marks['lnp']:
        etree.SubElement(ecd, _q('LNP')).text = marks['lnp']

    # рег.№ ДТ — XSLT рисует его в ячейке «A» из прямого поля ecd:RegNUM
    # (простой value-of), без параметра-узла. У нас номер уже в нужном формате.
    if marks.get('reg_full'):
        etree.SubElement(ecd, _q('RegNUM')).text = marks['reg_full']

    return ecd, 1


# ── трансформация + чистка + PDF ──────────────────────────────────────────────

_PRINT_STYLE = ('<style>@page{size:A4 landscape;margin:6mm;}'
                'body{-webkit-print-color-adjust:exact;}</style>')


def _strip_block_c_rows(html: str, keep: int) -> str:
    """Вычищает артефактные строки служебного GoodsShipment[2] (для блока C).

    For-each товаров идёт по ВСЕМ GoodsShipment/HouseShipment, поэтому GS[2]
    рисует ДВЕ лишние строки: (1) пустую строку-товар (та самая «полоска» под
    накладной) — помечена сентинелом в ObjectOrdinal; (2) пустую
    «Всего по инд.накладной». Убираем обе, оставляя `keep` настоящих итогов.
    """
    try:
        doc = etree.HTML(html)
    except etree.XMLSyntaxError:
        return html
    # (1) пустая строка-товар GS[2] — по сентинелу
    for tr in list(doc.iter('tr')):
        if _GS2_SENTINEL in ''.join(tr.itertext()):
            parent = tr.getparent()
            if parent is not None:
                parent.remove(tr)
    # (2) лишние «Всего по инд.накладной» сверх keep (последняя — от GS[2])
    marker = 'Всего по индивидуальной накладной'
    matches = [tr for tr in doc.iter('tr') if marker in ''.join(tr.itertext())]
    for tr in matches[keep:]:
        parent = tr.getparent()
        if parent is not None:
            parent.remove(tr)
    return etree.tostring(doc, method='html', encoding='unicode')


def render_html(hawb_number: str) -> str | None:
    ecd, n_house = build_print_ecd(hawb_number)
    if ecd is None:
        return None
    transform = etree.XSLT(etree.parse(XSLT_PATH))
    res = transform(etree.ElementTree(ecd))
    html = str(res)
    html = _strip_block_c_rows(html, keep=n_house)
    # печатные стили (landscape) в <head>
    if '<head>' in html:
        html = html.replace('<head>', '<head>' + _PRINT_STYLE, 1)
    else:
        html = html.replace('<html', _PRINT_STYLE + '<html', 1)
    return html


def render_pdf(hawb_number: str) -> bytes | None:
    """Полный per-HAWB реестр в PDF (родной бланк Альты + отметки). None если нет данных."""
    html = render_html(hawb_number)
    if html is None:
        return None
    edge = _edge_exe()
    if not edge:
        raise RuntimeError('Microsoft Edge не найден для HTML→PDF')
    workdir = tempfile.mkdtemp(prefix='reestr_')
    html_path = os.path.join(workdir, 'reestr.html')
    pdf_path = os.path.join(workdir, 'reestr.pdf')
    profile = os.path.join(workdir, 'profile')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    url = 'file:///' + html_path.replace('\\', '/')
    cmd = [
        edge, '--headless', '--disable-gpu', '--no-sandbox',
        '--no-pdf-header-footer', f'--user-data-dir={profile}',
        f'--print-to-pdf={pdf_path}', url,
    ]
    try:
        subprocess.run(cmd, timeout=120, capture_output=True)
        if not os.path.exists(pdf_path):
            raise RuntimeError('Edge не создал PDF')
        with open(pdf_path, 'rb') as f:
            return f.read()
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
