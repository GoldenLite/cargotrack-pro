"""SLA computation service.

compute_sla_state() — основная публичная функция. Читает активные SLAPolicy
(с простым кешем, инвалидируемым по max(updated_at)) и возвращает dict с
дедлайном и остатком времени, либо None если норматив не задан.
"""
from decimal import Decimal

from django.utils import timezone


_POLICY_CACHE = {
    'stamp': None,   # max updated_at на момент загрузки
    'map':   {},     # (entity_type, status_field, status_value) → SLAPolicy
}


def _load_policies():
    from .models import SLAPolicy

    latest = SLAPolicy.objects.filter(is_active=True).order_by('-updated_at').values_list('updated_at', flat=True).first()
    if latest == _POLICY_CACHE['stamp'] and _POLICY_CACHE['map']:
        return _POLICY_CACHE['map']
    mapping = {}
    for p in SLAPolicy.objects.filter(is_active=True):
        mapping[(p.entity_type, p.status_field, p.status_value)] = p
    _POLICY_CACHE['stamp'] = latest
    _POLICY_CACHE['map']   = mapping
    return mapping


def invalidate_policy_cache():
    _POLICY_CACHE['stamp'] = None
    _POLICY_CACHE['map']   = {}


def compute_sla_state(entity_type, status_field, status_value,
                      status_changed_at, workflow_step=None, now=None):
    """
    Вернуть dict с состоянием SLA или None, если нет ни политики, ни override.

    entity_type:       'cargo' | 'hawb'
    status_field:      'stage' | 'logistics_status' | 'customs_status'
    status_value:      код текущего этапа/статуса
    status_changed_at: datetime входа в этап
    workflow_step:     экземпляр WorkflowStep (или None) — если его sla_hours_override задан, он перекрывает политику
    now:               datetime для тестов; по умолчанию timezone.now()
    """
    if not status_value or status_changed_at is None:
        return None

    override = getattr(workflow_step, 'sla_hours_override', None) if workflow_step else None
    policy = _load_policies().get((entity_type, status_field, status_value))

    if override is not None:
        hours = override
        threshold = policy.warning_threshold_pct if policy else 75
        source = 'workflow_step'
        policy_id = policy.pk if policy else None
    elif policy is not None:
        hours = policy.hours
        threshold = policy.warning_threshold_pct
        source = 'policy'
        policy_id = policy.pk
    else:
        return None

    now = now or timezone.now()
    total_seconds = int(Decimal(hours) * 3600)
    if total_seconds <= 0:
        return None
    deadline = status_changed_at + timezone.timedelta(seconds=total_seconds)
    remaining = int((deadline - now).total_seconds())

    return {
        'hours':               float(hours),
        'deadline_iso':        deadline.isoformat(),
        'total_seconds':       total_seconds,
        'remaining_seconds':   remaining,
        'warning_threshold_pct': int(threshold),
        'breached':            remaining < 0,
        'source':              source,
        'policy_id':           policy_id,
    }


def get_active_workflow_step(entity_type, entity_id):
    """Вернуть WorkflowStep активного инстанса для сущности, либо None."""
    from .models import WorkflowInstance
    inst = (WorkflowInstance.objects
            .filter(entity_type=entity_type, entity_id=entity_id, status='active')
            .select_related('current_step').first())
    return inst.current_step if inst else None
