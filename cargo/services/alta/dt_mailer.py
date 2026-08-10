"""Рассылка регулярных ДТ (печатный бланк-PDF) при выпуске — серверная логика.

В отличие от ДТЭГ-реестра (его мы рендерим родным XSLT Альты), классический
бланк ДТ (GTD.PRN + QR ФТС + «ДОПОЛНЕНИЕ») печатает ТОЛЬКО сама Альта. Поэтому:
агент на Альта-машине забирает готовый PDF из C:\\GTDSERV\\ED\\IN_PDF и POST'ит
его на /api/v1/dt/pdf/; здесь мы храним файл, привязываем к накладной по рег.№
(из имени файла GTD_<пост>_<ддммгг>_<номер>) и шлём письмом с темой/текстом по
инд.накладной (код 02021). См. dt-mailer в памяти.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET

from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.utils import timezone

# Подачи, несущие ESADout_CU (регулярная ДТ). CMN.11023 несёт И ДТЭГ, И ДТ —
# различаем по корню 'ESADout_CU'.
DT_SUBMISSION_MSG_TYPES = ['CMN.11023', 'CMN.11024']

# Коды видов транспортных документов (гр.44) — fallback для темы письма, если
# инд.накладной (02021) в ДТ нет.
TRANSPORT_DOC_CODES = {'02011', '02012', '02013', '02014', '02015', '02017', '02099'}
IND_WAYBILL_CODE = '02021'  # индивидуальная накладная при экспресс-доставке


def _lt(e) -> str:
    return e.tag.rsplit('}', 1)[-1]


# Имя печатного бланка = GTD_<пост:8цифр>_<дата:6цифр>_<номер> (+ возможный
# хвост от коллизии имён: '_2', ' (1)'). Берём СТРОГО три поля, хвост игнорируем
# — иначе суффикс дал бы другой рег.№ и сломал дедуп рассылки.
_GTD_NAME_RE = re.compile(r'^GTD[_-](\d{8})[_-](\d{6})[_-]([0-9A-Za-zА-Яа-я]+)',
                          re.IGNORECASE)


def reg_from_filename(name: str) -> str:
    """GTD_10511010_100826_5129147[.pdf|_2| (1)] -> 10511010/100826/5129147.

    Строгий разбор: пост(8)/дата(6)/номер. Пусто, если имя не в этом формате
    (тогда рег.№ не парсится — рассылка по такому файлу не идёт вслепую).
    """
    base = (name or '').rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
    if '.' in base:
        base = base.rsplit('.', 1)[0]
    m = _GTD_NAME_RE.match(base)
    if m:
        return f'{m.group(1)}/{m.group(2)}/{m.group(3)}'
    return ''


def dt_docs_for_hawb(hawb_number: str) -> tuple[str, str]:
    """Из подачи ДТ (ESADout_CU) по накладной достаёт (инд.накладная 02021,
    транспортный документ). Пустые строки, если подачи/документов нет."""
    from cargo.models import AltaOutboxObservation
    for mt in DT_SUBMISSION_MSG_TYPES:
        for obs in (AltaOutboxObservation.objects
                    .filter(msg_type=mt, parsed_meta__hawbs__contains=[hawb_number])
                    .order_by('-received_at')[:3]):
            rx = (obs.parsed_meta or {}).get('raw_xml') or ''
            if 'ESADout_CU' not in rx:
                continue
            try:
                root = ET.fromstring(rx.encode('utf-8') if isinstance(rx, str) else rx)
            except ET.ParseError:
                continue
            ind = transport = ''
            for e in root.iter():
                if _lt(e) != 'ESADout_CUPresentedDocument':
                    continue
                code = num = ''
                for ch in e:
                    ln = _lt(ch)
                    if ln == 'PresentedDocumentModeCode':
                        code = (ch.text or '').strip()
                    elif ln == 'PrDocumentNumber':
                        num = (ch.text or '').strip()
                if code == IND_WAYBILL_CODE and not ind:
                    ind = num
                elif code in TRANSPORT_DOC_CODES and not transport:
                    transport = num
            if ind or transport:
                return ind, transport
    return '', ''


def dt_label_for_doc(doc) -> tuple[str, str]:
    """Метка для темы/тела письма по документу ДТ: (label, kind).

    Приоритет юзера: инд.накладная (02021); если нет — транспортный документ;
    если и его нет — сам рег.№ ДТ. kind ∈ {'02021','transport','reg'}.
    """
    hn = doc.hawb.hawb_number if doc.hawb_id and doc.hawb else ''
    ind = transport = ''
    if hn:
        ind, transport = dt_docs_for_hawb(hn)
    if ind:
        return ind, '02021'
    if transport:
        return transport, 'transport'
    return doc.declaration_number or '(без номера)', 'reg'


# ── приём + хранение PDF ──────────────────────────────────────────────────────

def store_incoming_dt(pdf_bytes: bytes, filename: str):
    """Сохраняет входящий PDF-бланк ДТ на диск + строку IncomingDTDocument.

    Идемпотентно по sha256 (агент может прислать один файл дважды). Рег.№ парсит
    из имени, привязывает к накладной по customs_declaration_number. Возвращает
    (doc, created).
    """
    from cargo.models import IncomingDTDocument, HouseWaybill

    digest = hashlib.sha256(pdf_bytes).hexdigest()
    existing = IncomingDTDocument.objects.filter(sha256=digest).first()
    if existing is not None:
        return existing, False

    reg = reg_from_filename(filename)
    hawb = None
    if reg:
        hawb = (HouseWaybill.objects.filter(customs_declaration_number=reg)
                .order_by('-id').first())

    doc = IncomingDTDocument(
        declaration_number=reg,
        hawb=hawb,
        filename=(filename or '')[:255],
        size_bytes=len(pdf_bytes),
        sha256=digest,
        status=IncomingDTDocument.STATUS_PENDING,
    )
    safe_name = (filename or f'DT_{reg or digest[:12]}.pdf')
    # имя внутри upload_to — только basename, без путей
    safe_name = safe_name.rsplit('/', 1)[-1].rsplit('\\', 1)[-1][:255] or 'dt.pdf'
    doc.pdf.save(safe_name, ContentFile(pdf_bytes), save=False)
    try:
        doc.save()
    except IntegrityError:
        # Гонка: параллельный POST того же файла успел вставить строку раньше.
        # Удаляем свежезаписанный файл-сироту и возвращаем существующую строку —
        # приём остаётся идемпотентным (200, а не 500).
        try:
            doc.pdf.delete(save=False)
        except Exception:
            pass
        existing = IncomingDTDocument.objects.filter(sha256=digest).first()
        if existing is not None:
            return existing, False
        raise
    return doc, True


# ── ротация файлов после отправки ─────────────────────────────────────────────

def purge_sent_dt_docs(older_than_days: int = 14) -> int:
    """Удаляет PDF-файлы разосланных ДТ с диска (строка-аудит остаётся).

    Оставляем лаг (по умолч. 14 дней) как страховку на случай переотправки.
    """
    from datetime import timedelta
    from cargo.models import IncomingDTDocument

    cutoff = timezone.now() - timedelta(days=older_than_days)
    n = 0
    for doc in (IncomingDTDocument.objects
                .filter(status=IncomingDTDocument.STATUS_SENT, sent_at__lt=cutoff)
                .exclude(pdf='')):
        try:
            doc.pdf.delete(save=False)
        except Exception:
            pass
        doc.status = IncomingDTDocument.STATUS_PURGED
        doc.purged_at = timezone.now()
        doc.save(update_fields=['pdf', 'status', 'purged_at', 'updated_at'])
        n += 1
    return n
