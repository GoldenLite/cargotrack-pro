"""Диагностика возможных якорей связи ED.11003 ↔ наша CMN.11349.

Цель: понять как сматчить пришедший от таможни запрос с нашей исходящей
декларацией. Стандартный InitialEnvelopeID в ED.11003 пустой.
"""
import django, os, re
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cargotrack.settings')
django.setup()

from cargo.models import AltaInboxMessage, AltaOutboxObservation


def _xml_field(xml, tag):
    m = re.search(
        rf'<(?:[\w-]+:)?{tag}\b[^>]*>([^<]+)</(?:[\w-]+:)?{tag}>', xml)
    return m.group(1).strip() if m else ''


print('=== Анализ 5 свежих ED.11003 ===')
for msg in AltaInboxMessage.objects.filter(msg_type='ED.11003').order_by('-received_at')[:5]:
    print(f'\nED.11003 envelope={msg.envelope_id}')
    raw = msg.raw_xml or ''
    process_id     = _xml_field(raw, 'ProccessID')
    doc_id         = _xml_field(raw, 'DocumentID')
    initial_env    = _xml_field(raw, 'InitialEnvelopeID')
    sender_customs = _xml_field(raw, 'CustomsCode')
    # GTDNumber внутри rid:GTDNumber может быть «000000» или реальный
    gtd_block = re.search(
        r'<rid:GTDNumber\b[^>]*>(.*?)</rid:GTDNumber>', raw, re.S)
    gtd_full = ''
    if gtd_block:
        body = gtd_block.group(1)
        cc = _xml_field(body, 'CustomsCode')
        rd = _xml_field(body, 'RegistrationDate')
        gn = _xml_field(body, 'GTDNumber')
        gtd_full = f'{cc}/{rd}/{gn}'
    print(f'  ProcessID:        {process_id}')
    print(f'  DocumentID:       {doc_id}')
    print(f'  InitialEnvelope:  {initial_env!r}')
    print(f'  SenderCustoms:    {sender_customs}')
    print(f'  GTDNumber(body):  {gtd_full!r}')

    # Поищем CMN.11349/11023 с этим ProcessID или DocumentID
    if process_id:
        obs = AltaOutboxObservation.objects.filter(
            parsed_meta__contains={'process_id': process_id}).first()
        # JSONField __contains не всегда работает, fallback на raw_xml содержит
        if not obs:
            obs = AltaOutboxObservation.objects.filter(
                parsed_meta__raw_xml__icontains=process_id).first()
        print(f'  Outbox by ProcessID:   {obs and obs.msg_type} env={obs and obs.envelope_id}')

print('\n=== Что хранится в parsed_meta CMN.11349 outbox observations? ===')
obs = AltaOutboxObservation.objects.filter(msg_type='CMN.11349').first()
if obs:
    pm = obs.parsed_meta or {}
    print(f'CMN.11349 envelope={obs.envelope_id}')
    print(f'  parsed_meta keys: {list(pm.keys())}')
    raw = pm.get('raw_xml', '')
    if raw:
        print(f'  ProcessID in CMN.11349 raw_xml: {_xml_field(raw, "ProccessID")!r}')
        print(f'  EnvelopeID in CMN.11349 raw_xml: {_xml_field(raw, "EnvelopeID")!r}')
else:
    print('CMN.11349 не нашёл в БД')
