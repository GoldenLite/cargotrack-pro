"""Диагностика: проверяем какие msg_type есть в БД (фильтр на 11003/MY)."""
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cargotrack.settings')
django.setup()

from django.db.models import Count
from cargo.models import AltaInboxMessage

# 1) Все типы сообщений в БД — топ-20 по частоте
print('=== Все msg_type в AltaInboxMessage (top 20) ===')
for x in (AltaInboxMessage.objects
          .values('msg_type')
          .annotate(n=Count('id'))
          .order_by('-n')[:20]):
    print(f'  {x["msg_type"]!r}: {x["n"]}')

# 2) Всё что содержит "11003" или "MY"
print('\n=== msg_type с "11003" или "MY" в имени ===')
for x in (AltaInboxMessage.objects
          .filter(msg_type__icontains='11003')
          .values('msg_type').annotate(n=Count('id'))):
    print(f'  {x["msg_type"]!r}: {x["n"]}')
for x in (AltaInboxMessage.objects
          .filter(msg_type__istartswith='MY')
          .values('msg_type').annotate(n=Count('id'))):
    print(f'  {x["msg_type"]!r}: {x["n"]}')

# 3) Один пример где raw_xml содержит "ReqInventoryDoc" (тело MY.11003)
print('\n=== Сообщения с ReqInventoryDoc в raw_xml ===')
qs = AltaInboxMessage.objects.filter(raw_xml__icontains='ReqInventoryDoc')[:5]
for m in qs:
    print(f'  pk={m.pk} envelope={m.envelope_id} msg_type={m.msg_type!r} kind={m.msg_kind!r}')
print(f'Всего таких: {AltaInboxMessage.objects.filter(raw_xml__icontains="ReqInventoryDoc").count()}')
