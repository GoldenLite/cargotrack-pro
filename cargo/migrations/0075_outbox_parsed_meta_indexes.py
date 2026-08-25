"""Функциональные индексы на parsed_meta->>'mcd_id' и ->>'report_number'
у AltaOutboxObservation.

Инцидент 24-25.08.2026: dispatch()→match() фильтрует
`AltaOutboxObservation.filter(parsed_meta__mcd_id=...)` и
`parsed_meta__report_number=...` — БЕЗ индекса это Parallel Seq Scan по 559МБ
(parsed_meta содержит raw_xml до 4МБ на строку). Один такой dispatch = минуты;
пачка «отравленных» релизов держала конвейер в стопе. Индексы превращают seq-scan
в Bitmap Index Scan (cost 24030→2107). CONCURRENTLY + IF NOT EXISTS: на проде
индексы уже созданы вручную (no-op), на свежей БД — создаются. atomic=False
обязателен для CONCURRENTLY.
"""
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('cargo', '0074_incoming_dt_document'),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE INDEX CONCURRENTLY IF NOT EXISTS cargo_outbox_mcd_id_idx "
                "ON cargo_altaoutboxobservation ((parsed_meta->>'mcd_id'));",
            reverse_sql="DROP INDEX CONCURRENTLY IF EXISTS cargo_outbox_mcd_id_idx;",
        ),
        migrations.RunSQL(
            sql="CREATE INDEX CONCURRENTLY IF NOT EXISTS cargo_outbox_report_number_idx "
                "ON cargo_altaoutboxobservation ((parsed_meta->>'report_number'));",
            reverse_sql="DROP INDEX CONCURRENTLY IF EXISTS cargo_outbox_report_number_idx;",
        ),
    ]
