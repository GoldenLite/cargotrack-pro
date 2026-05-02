"""Периодическая проверка просрочек SLA.

Запускается из Windows Task Scheduler / cron раз в 5–15 минут:
    python manage.py check_sla_breaches

Для каждой активной SLAPolicy находит сущности, которые превысили норматив,
создаёт SLABreachEvent (идемпотентно через unique_together) и, если
к текущему шагу workflow-инстанса привязано правило sla_breach_notify, —
вызывает его через workflow_runner.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from cargo.models import (
    Cargo, HouseWaybill, SLAPolicy, SLABreachEvent,
    WorkflowInstance, AutomationRule,
)


FIELD_TO_DATE = {
    'stage':            'stage_changed_at',
    'logistics_status': 'logistics_status_date',
    'customs_status':   'customs_status_date',
}


class Command(BaseCommand):
    help = 'Проверить просрочки SLA и уведомить о нарушениях'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Не создавать события и не слать уведомления, только логировать')

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        now = timezone.now()
        total_breaches = 0
        total_notified = 0

        for policy in SLAPolicy.objects.filter(is_active=True):
            date_field = FIELD_TO_DATE.get(policy.status_field)
            if not date_field:
                continue
            cutoff = now - timedelta(seconds=int(Decimal(policy.hours) * 3600))

            if policy.entity_type == 'cargo':
                qs = Cargo.objects.filter(**{
                    policy.status_field: policy.status_value,
                    f'{date_field}__lte': cutoff,
                    f'{date_field}__isnull': False,
                })
            else:
                qs = HouseWaybill.objects.filter(**{
                    policy.status_field: policy.status_value,
                    f'{date_field}__lte': cutoff,
                    f'{date_field}__isnull': False,
                })

            for entity in qs:
                breached_at = getattr(entity, date_field) + timedelta(
                    seconds=int(Decimal(policy.hours) * 3600),
                )

                inst = (WorkflowInstance.objects
                        .filter(entity_type=policy.entity_type, entity_id=entity.pk, status='active')
                        .select_related('current_step').first())

                # Применяем workflow override, если задан
                effective_hours = policy.hours
                if inst and inst.current_step and inst.current_step.sla_hours_override is not None:
                    effective_hours = inst.current_step.sla_hours_override
                    breached_at = getattr(entity, date_field) + timedelta(
                        seconds=int(Decimal(effective_hours) * 3600),
                    )
                    if breached_at > now:
                        continue  # с учётом override — ещё не просрочено

                if dry:
                    self.stdout.write(f'[dry] breach: policy={policy.pk} entity={policy.entity_type}#{entity.pk} at {breached_at.isoformat()}')
                    total_breaches += 1
                    continue

                event, created = SLABreachEvent.objects.get_or_create(
                    policy=policy, entity_type=policy.entity_type, entity_id=entity.pk,
                    breached_at=breached_at,
                    defaults={'workflow_instance': inst},
                )
                if not created:
                    continue

                total_breaches += 1

                # Отправим автоматизацию, если к переходам инстанса привязано sla_breach_notify
                if inst:
                    rules = AutomationRule.objects.filter(
                        workflow=inst.workflow, action_type='sla_breach_notify', is_active=True,
                    )
                    if rules.exists():
                        from cargo import workflow_runner
                        overdue_hours = float((now - breached_at).total_seconds()) / 3600
                        ctx = {
                            'status_field': policy.status_field,
                            'status_value': policy.status_value,
                            'hours':        float(effective_hours),
                            'overdue_hours': overdue_hours,
                        }
                        inst.sla_breach_context = ctx
                        for rule in rules:
                            result = workflow_runner._execute_automation(rule, inst)
                            self.stdout.write(f'  → {rule.name}: {result}')
                        event.notified = True
                        event.save(update_fields=['notified'])
                        total_notified += 1

        self.stdout.write(self.style.SUCCESS(
            f'SLA check done. Breaches: {total_breaches}, notifications: {total_notified}'
        ))
