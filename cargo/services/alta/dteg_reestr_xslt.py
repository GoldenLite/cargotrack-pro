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

_WKHTMLTOPDF_CANDIDATES = [
    r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe',
    r'C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe',
]


def _wkhtmltopdf_exe():
    for p in _WKHTMLTOPDF_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


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

# Сообщения с отметкой ВЫПУСК ДТЭГ. CMN.11350 (ExpressCargoDeclarationCustomMark)
# несёт выпуск ИМПОРТА; ЭКСПОРТ (ПТДЭГ) выпускается через CMN.11341 — оно
# CMN.11350 НЕ шлёт, но parsed_meta.consignments у него ТОЙ ЖЕ формы
# (decision_code/waybills/decision_date) и raw_xml несёт инспектора/ЛНП/рег.№.
RELEASE_MSG_TYPES = ['CMN.11350', 'CMN.11341']


def find_release_message(hawb_number: str):
    """Сообщение с решением ВЫПУСК (decision_code=10) по этой накладной.

    Ищем в CMN.11350 (импорт) и CMN.11341 (экспорт/ПТДЭГ) — см. RELEASE_MSG_TYPES.
    Быстрый JSONB-поиск по parsed_meta.consignments (LIKE по raw_xml медленный).
    По накладной бывает несколько сообщений (позже код 70 «продлён»/90 «отказ») —
    матчим ИМЕННО выпуск (10). None если выпуска нет.
    """
    from cargo.models import AltaInboxMessage
    return (AltaInboxMessage.objects
            .filter(msg_type__in=RELEASE_MSG_TYPES,
                    parsed_meta__consignments__contains=[
                        {'decision_code': '10', 'waybills': [hawb_number]}])
            .order_by('-id').first())


def find_custommark_for_hawb(hawb_number: str):
    """Отметки выпуска (decision_code=10) по накладной.

    Дата решения и рег.№ — из parsed_meta (быстро); инспектор/ЛНП — из raw_xml
    найденного сообщения (один парс, не скан). None если решения-выпуска нет.
    Возвращает dict{decision_code, decision_date, reg_customs, reg_date, reg_gtd,
    inspector, lnp}.
    """
    msg = find_release_message(hawb_number)
    if msg is None:
        return None
    pm = msg.parsed_meta or {}
    cons = next((c for c in pm.get('consignments', [])
                 if hawb_number in (c.get('waybills') or [])
                 and (c.get('decision_code') or '') == '10'), {})
    # инспектор/ЛНП есть только в raw_xml (в parsed_meta их нет)
    insp, lnp = '', ''
    rx = msg.raw_xml or ''
    if rx:
        try:
            root = etree.fromstring(rx.encode('utf-8') if isinstance(rx, str) else rx)
            for e in root.iter():
                ln = e.tag.rsplit('}', 1)[-1]
                if ln == 'PersonName' and not insp:
                    insp = (e.text or '').strip()
                elif ln == 'LNP' and not lnp:
                    lnp = (e.text or '').strip()
        except etree.XMLSyntaxError:
            pass
    return {
        'decision_code': '10',
        'decision_date': cons.get('decision_date') or '',
        'reg_customs': pm.get('customs_code') or '',
        'reg_date': pm.get('registration_date') or '',
        'reg_gtd': pm.get('gtd_number') or '',
        'inspector': insp,
        'lnp': lnp,
    }


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
    return {
        # Текст решения (design) считается по режиму в build_print_ecd.
        'decision_code': (cm or {}).get('decision_code') or '10',
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


def _html_to_pdf_bytes(html: str) -> bytes:
    """HTML → PDF. Предпочитаем wkhtmltopdf (надёжен под SYSTEM/session 0, где
    Edge headless падает rc=1002); Edge — fallback для интерактивного контекста."""
    workdir = tempfile.mkdtemp(prefix='reestr_')
    html_path = os.path.join(workdir, 'reestr.html')
    pdf_path = os.path.join(workdir, 'reestr.pdf')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    try:
        wk = _wkhtmltopdf_exe()
        if wk:
            cmd = [wk, '--enable-local-file-access', '--encoding', 'utf-8',
                   '--orientation', 'Landscape', '--page-size', 'A4',
                   '-B', '6mm', '-T', '6mm', '-L', '6mm', '-R', '6mm',
                   '--quiet', html_path, pdf_path]
            proc = subprocess.run(cmd, timeout=120, capture_output=True)
            if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                with open(pdf_path, 'rb') as f:
                    return f.read()
            err = (proc.stderr or b'')[-600:].decode('utf-8', 'replace')
            raise RuntimeError(f'wkhtmltopdf не создал PDF (rc={proc.returncode}): {err}')

        edge = _edge_exe()
        if edge:
            profile = os.path.join(workdir, 'profile')
            url = 'file:///' + html_path.replace('\\', '/')
            cmd = [edge, '--headless=new', '--disable-gpu', '--no-sandbox',
                   '--disable-dev-shm-usage', '--no-first-run',
                   '--no-default-browser-check', '--no-pdf-header-footer',
                   f'--user-data-dir={profile}', f'--crash-dumps-dir={workdir}',
                   f'--print-to-pdf={pdf_path}', url]
            proc = subprocess.run(cmd, timeout=120, capture_output=True)
            if os.path.exists(pdf_path):
                with open(pdf_path, 'rb') as f:
                    return f.read()
            err = (proc.stderr or b'')[-600:].decode('utf-8', 'replace')
            raise RuntimeError(f'Edge не создал PDF (rc={proc.returncode}): {err}')

        raise RuntimeError('Не найден движок HTML→PDF (wkhtmltopdf/Edge)')
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


def render_pdf(hawb_number: str) -> bytes | None:
    """Полный per-HAWB реестр в PDF (родной бланк Альты + отметки). None если нет данных."""
    html = render_html(hawb_number)
    if html is None:
        return None
    return _html_to_pdf_bytes(html)
