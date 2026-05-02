"""
WorkflowRunner — движок выполнения бизнес-процессов.

Отвечает за:
- автоматический запуск инстансов при создании сущности
- продвижение инстансов при смене статуса сущности
- выполнение автоматизаций (AutomationRule)
"""
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def _get_models():
    """Ленивый импорт моделей во избежание circular import."""
    from .models import (
        Workflow, WorkflowInstance, WorkflowInstanceEvent, AutomationRule
    )
    return Workflow, WorkflowInstance, WorkflowInstanceEvent, AutomationRule


# ──────────────────────────────────────────────────────────────────────────────
# Публичный API
# ──────────────────────────────────────────────────────────────────────────────

def start_for_entity(entity, entity_type: str, user=None) -> list:
    """
    Найти все активные воркфлоу с auto_start=True, подходящие под условия сущности,
    и создать для них WorkflowInstance.

    Возвращает список созданных инстансов.
    """
    Workflow, WorkflowInstance, _, _ = _get_models()

    created = []
    workflows = Workflow.objects.filter(
        entity_type=entity_type, auto_start=True, is_active=True
    )
    for wf in workflows:
        if _matches_conditions(entity, wf.trigger_conditions):
            instance = _create_instance(wf, entity, entity_type, user)
            if instance:
                created.append(instance)
    return created


def start_workflow_manually(workflow_id: int, entity, entity_type: str, user=None):
    """
    Ручной запуск конкретного воркфлоу на сущности.
    Возвращает (instance, error_str | None).
    """
    Workflow, WorkflowInstance, _, _ = _get_models()

    try:
        wf = Workflow.objects.get(pk=workflow_id, is_active=True)
    except Workflow.DoesNotExist:
        return None, 'Воркфлоу не найден или неактивен'

    if wf.entity_type != entity_type:
        return None, f'Воркфлоу предназначен для «{wf.get_entity_type_display()}», а не для выбранной сущности'

    # Проверим, нет ли уже активного инстанса
    if WorkflowInstance.objects.filter(
        workflow=wf, entity_type=entity_type, entity_id=entity.pk, status='active'
    ).exists():
        return None, 'Воркфлоу уже запущен для этой сущности'

    instance = _create_instance(wf, entity, entity_type, user)
    return instance, None


def advance_on_status_change(entity, entity_type: str, status_field: str, new_value: str) -> None:
    """
    Вызывается при смене статуса сущности.
    Продвигает все активные инстансы воркфлоу для данной сущности, если найден шаг,
    соответствующий новому статусу.

    status_field: 'stage' | 'logistics_status' | 'customs_status'
    """
    _, WorkflowInstance, _, _ = _get_models()

    instances = WorkflowInstance.objects.filter(
        entity_type=entity_type, entity_id=entity.pk, status='active'
    ).select_related('workflow', 'current_step')

    for instance in instances:
        next_step = _find_step_for_status(instance.workflow, entity_type, status_field, new_value)
        if not next_step or next_step == instance.current_step:
            continue
        transition, allowed = _check_transition(
            instance.current_step, next_step, entity, entity_type
        )
        if not allowed:
            logger.info(
                f'WorkflowInstance #{instance.pk}: переход в «{next_step.name}» '
                f'заблокирован — CQL-условие не выполнено'
            )
            continue
        _advance(instance, next_step, triggered_by=None, transition=transition)


def cancel_instance(instance_id: int, user=None) -> tuple:
    """
    Отменить инстанс воркфлоу.
    Возвращает (instance | None, error_str | None).
    """
    _, WorkflowInstance, WorkflowInstanceEvent, _ = _get_models()

    try:
        instance = WorkflowInstance.objects.get(pk=instance_id)
    except WorkflowInstance.DoesNotExist:
        return None, 'Инстанс не найден'

    if instance.status != 'active':
        return None, 'Инстанс уже завершён или отменён'

    WorkflowInstanceEvent.objects.create(
        instance=instance,
        from_step=instance.current_step,
        to_step=None,
        triggered_by=user,
        note='Отменён вручную',
    )
    instance.status = 'cancelled'
    instance.completed_at = timezone.now()
    instance.save(update_fields=['status', 'completed_at'])
    logger.info(f'WorkflowInstance #{instance_id} отменён пользователем {user}')
    return instance, None


def get_instances_for_entity(entity_type: str, entity_id: int) -> list:
    """Вернуть все инстансы воркфлоу для данной сущности."""
    _, WorkflowInstance, WorkflowInstanceEvent, _ = _get_models()
    return list(
        WorkflowInstance.objects.filter(
            entity_type=entity_type, entity_id=entity_id
        ).select_related('workflow', 'current_step').order_by('-started_at')
    )


def serialize_instance(instance) -> dict:
    """Сериализовать инстанс воркфлоу в словарь для API."""
    steps = list(instance.workflow.steps.order_by('order', 'id'))
    total = len(steps)

    # Найти номер текущего шага
    progress = 0
    if instance.current_step:
        for i, s in enumerate(steps):
            if s.pk == instance.current_step_id:
                progress = i + 1
                break

    # История событий
    events = []
    for ev in instance.events.select_related('from_step', 'to_step', 'triggered_by').order_by('triggered_at'):
        events.append({
            'from_step': ev.from_step.name if ev.from_step else None,
            'to_step': ev.to_step.name if ev.to_step else None,
            'triggered_by': str(ev.triggered_by) if ev.triggered_by else 'Система',
            'triggered_at': ev.triggered_at.isoformat(),
            'note': ev.note,
            'automation_log': ev.automation_log,
        })

    return {
        'id': instance.pk,
        'workflow_id': instance.workflow_id,
        'workflow_name': instance.workflow.name,
        'entity_type': instance.entity_type,
        'entity_id': instance.entity_id,
        'status': instance.status,
        'status_display': instance.get_status_display(),
        'current_step': _step_to_dict(instance.current_step) if instance.current_step else None,
        'progress': progress,
        'total_steps': total,
        'steps': [_step_to_dict(s) for s in steps],
        'started_at': instance.started_at.isoformat(),
        'completed_at': instance.completed_at.isoformat() if instance.completed_at else None,
        'events': events,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────

def _matches_conditions(entity, conditions: dict) -> bool:
    """Проверить, подходит ли сущность под условия запуска воркфлоу."""
    if not conditions:
        return True
    # Поддержка CQL-ключа: {'cql': 'stage = CUSTOMS AND weight > 1000'}
    if 'cql' in conditions:
        return _entity_matches_cql(entity, conditions['cql'],
                                   'hawb' if entity.__class__.__name__ == 'HouseWaybill' else 'cargo')
    for field, value in conditions.items():
        actual = str(getattr(entity, field, None) or '')
        if actual != str(value):
            return False
    return True


def _entity_matches_cql(entity, condition: str, entity_type: str) -> bool:
    """Проверить, удовлетворяет ли сущность CQL-условию (через запрос к БД)."""
    if not condition or not condition.strip():
        return True
    try:
        from .cql_parser import parse_cql, CQLError as _CQLError
        q = parse_cql(condition.strip(), entity_type=entity_type)
        from .models import Cargo, HouseWaybill
        model = Cargo if entity_type == 'cargo' else HouseWaybill
        return model.objects.filter(q, pk=entity.pk).exists()
    except Exception as exc:
        logger.warning(f'CQL condition check failed ({condition!r}): {exc}')
        return False


def _check_transition(from_step, to_step, entity, entity_type: str):
    """Проверить, существует ли допустимый переход из from_step в to_step для данной сущности.

    Возвращает (transition, allowed):
      - allowed=True + transition=None   — переходов не задано, продвижение разрешено (обратная совместимость)
      - allowed=True + transition=<obj>  — найден переход с пройденным CQL-условием
      - allowed=False + transition=None  — переходы есть, но ни один не прошёл условие
    """
    if not from_step:
        return None, True

    transitions = list(
        from_step.transitions_out.filter(to_step=to_step).order_by('order')
    )
    if not transitions:
        return None, True  # граф не описывает этот путь → разрешаем (обратная совместимость)

    for tr in transitions:
        if _entity_matches_cql(entity, tr.condition, entity_type):
            return tr, True

    return None, False  # переходы есть, но ни одно условие не выполнено


def _find_step_for_status(workflow, entity_type: str, status_field: str, value: str):
    """Найти шаг воркфлоу, соответствующий новому статусу сущности."""
    if entity_type == 'cargo' and status_field == 'stage':
        return workflow.steps.filter(step_type='stage', stage=value).first()
    if status_field == 'logistics_status':
        return workflow.steps.filter(hawb_logistics_status=value).first()
    if status_field == 'customs_status':
        return workflow.steps.filter(hawb_customs_status=value).first()
    return None


def _create_instance(workflow, entity, entity_type: str, user=None):
    """Создать WorkflowInstance для сущности. Если уже есть активный — пропустить."""
    _, WorkflowInstance, _, _ = _get_models()

    if WorkflowInstance.objects.filter(
        workflow=workflow, entity_type=entity_type, entity_id=entity.pk, status='active'
    ).exists():
        return None

    first_step = workflow.steps.order_by('order', 'id').first()
    instance = WorkflowInstance.objects.create(
        workflow=workflow,
        entity_type=entity_type,
        entity_id=entity.pk,
        current_step=first_step,
        status='active',
        started_by=user,
    )
    logger.info(
        f'WorkflowInstance создан: воркфлоу «{workflow.name}» '
        f'для {entity_type}:{entity.pk}'
    )
    return instance


def _advance(instance, to_step, triggered_by=None, transition=None, note='') -> None:
    """Продвинуть инстанс на следующий шаг и выполнить автоматизации."""
    _, _, WorkflowInstanceEvent, AutomationRule = _get_models()

    event = WorkflowInstanceEvent.objects.create(
        instance=instance,
        from_step=instance.current_step,
        to_step=to_step,
        transition=transition,
        triggered_by=triggered_by,
        note=note,
    )

    instance.current_step = to_step

    # Если у шага нет исходящих переходов — процесс завершён
    if not to_step.transitions_out.exists():
        instance.status = 'completed'
        instance.completed_at = timezone.now()

    instance.save(update_fields=['current_step', 'status', 'completed_at'])
    logger.info(
        f'WorkflowInstance #{instance.pk}: шаг -> «{to_step.name}» '
        f'(статус: {instance.status})'
    )

    # Выполнить автоматизации для перехода
    automation_log = _fire_automations(instance, event, transition)
    if automation_log:
        event.automation_log = automation_log
        event.save(update_fields=['automation_log'])


def _fire_automations(instance, event, transition=None) -> list:
    """Выполнить AutomationRule для данного перехода. Вернуть лог."""
    _, _, _, AutomationRule = _get_models()

    rules = AutomationRule.objects.filter(
        workflow=instance.workflow,
        transition=transition,
        is_active=True,
    ).order_by('order')

    log = []
    for rule in rules:
        result = _execute_automation(rule, instance)
        log.append({
            'rule_id': rule.pk,
            'rule_name': rule.name,
            'action_type': rule.action_type,
            'result': result,
        })
    return log


def _execute_automation(rule, instance) -> str:
    """Выполнить одно правило автоматизации. Возвращает строку-результат."""
    try:
        if rule.action_type == 'send_notify':
            return _action_send_notify(rule, instance)
        if rule.action_type == 'change_stage':
            return _action_change_stage(rule, instance)
        if rule.action_type == 'set_field':
            return _action_set_field(rule, instance)
        if rule.action_type == 'assign_user':
            return _action_assign_user(rule, instance)
        if rule.action_type == 'webhook':
            return _action_webhook(rule, instance)
        if rule.action_type == 'sla_breach_notify':
            return _action_sla_breach_notify(rule, instance)
        return f'Неизвестный тип: {rule.action_type}'
    except Exception as exc:
        logger.exception(f'Ошибка автоматизации «{rule.name}»: {exc}')
        return f'Ошибка: {exc}'


def _get_entity(instance):
    """Получить объект сущности (Cargo или HouseWaybill) для инстанса."""
    from .models import Cargo, HouseWaybill
    if instance.entity_type == 'cargo':
        return Cargo.objects.filter(pk=instance.entity_id).first()
    return HouseWaybill.objects.filter(pk=instance.entity_id).first()


def _action_send_notify(rule, instance) -> str:
    """Отправить email-уведомление.

    config:
      message    — текст письма; поддерживает переменные {workflow_name},
                   {entity_type}, {entity_id}, {step_name},
                   {awb_number} (Cargo) / {hawb_number} (HAWB)
      subject    — тема письма (необязательно)
      recipients — список получателей: "assigned_user" или "email:addr@..."
    """
    from django.core.mail import send_mail
    from django.conf import settings as django_settings

    cfg = rule.config
    raw_message  = cfg.get('message', '')
    raw_subject  = cfg.get('subject', 'Уведомление CargoTrack Pro')
    recipients_cfg = cfg.get('recipients') or []

    # ── Переменные шаблона ────────────────────────────────────────────────────
    step_name = ''
    if instance.current_step_id:
        try:
            step_name = instance.current_step.name
        except Exception:
            logger.warning(
                'cannot resolve current_step.name for instance %s', instance.pk,
                exc_info=True,
            )

    tpl_vars = {
        'workflow_name': instance.workflow.name,
        'entity_type':  instance.entity_type,
        'entity_id':    str(instance.entity_id),
        'step_name':    step_name,
    }
    entity = _get_entity(instance)
    if entity:
        if instance.entity_type == 'cargo':
            tpl_vars['awb_number'] = getattr(entity, 'awb_number', '')
        else:
            tpl_vars['hawb_number'] = getattr(entity, 'hawb_number', '')

    try:
        message = raw_message.format_map(tpl_vars)
        subject = raw_subject.format_map(tpl_vars)
    except (KeyError, ValueError):
        message = raw_message
        subject = raw_subject

    # ── Разрешение адресов получателей ───────────────────────────────────────
    emails = []
    for rec in recipients_cfg:
        if rec == 'assigned_user':
            if entity and instance.entity_type == 'hawb':
                au = getattr(entity, 'assigned_to', None)
                if au and getattr(au, 'email', ''):
                    emails.append(au.email)
            elif entity and instance.entity_type == 'cargo':
                from .models import CargoAssignment
                for ca in CargoAssignment.objects.filter(cargo=entity, is_active=True).select_related('user'):
                    if ca.user.email:
                        emails.append(ca.user.email)
        elif isinstance(rec, str) and rec.startswith('email:'):
            addr = rec[len('email:'):].strip()
            if addr:
                emails.append(addr)

    emails = list(dict.fromkeys(emails))  # deduplicate, preserve order

    logger.info(
        f'[NOTIFY] Воркфлоу «{instance.workflow.name}» / '
        f'{instance.entity_type}:{instance.entity_id} -> {emails or "нет получателей"}: {message}'
    )

    if not emails:
        return f'Уведомление (нет получателей): {message}'

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(django_settings, 'DEFAULT_FROM_EMAIL', None),
            recipient_list=emails,
            fail_silently=False,
        )
        return f'Email отправлен ({len(emails)} получ.): {subject}'
    except Exception as exc:
        logger.exception(f'[NOTIFY] Ошибка отправки email: {exc}')
        return f'Ошибка отправки email: {exc}'


def _action_change_stage(rule, instance) -> str:
    """Сменить этап/статус сущности."""
    cfg = rule.config
    entity = _get_entity(instance)
    if not entity:
        return 'Сущность не найдена'

    if instance.entity_type == 'cargo':
        new_stage = cfg.get('stage', '')
        if new_stage:
            entity.set_stage(new_stage)
            return f'Этап партии изменён на {new_stage}'
    else:
        new_logistics = cfg.get('logistics_status', '')
        new_customs = cfg.get('customs_status', '')
        if new_logistics:
            entity.change_logistics_status(new_logistics)
            return f'Лог. статус изменён на {new_logistics}'
        if new_customs:
            entity.change_customs_status(new_customs)
            return f'Там. статус изменён на {new_customs}'
    return 'Нет параметров для смены статуса'


def _action_set_field(rule, instance) -> str:
    """Установить поле сущности."""
    cfg = rule.config
    field = cfg.get('field', '')
    value = cfg.get('value', '')
    if not field:
        return 'Не указано поле'
    entity = _get_entity(instance)
    if not entity:
        return 'Сущность не найдена'
    if hasattr(entity, field):
        setattr(entity, field, value)
        entity.save(update_fields=[field])
        return f'Поле {field} = {value}'
    return f'Поле {field} не найдено'


def _action_assign_user(rule, instance) -> str:
    """Назначить пользователя на сущность."""
    from django.contrib.auth.models import User
    cfg = rule.config
    username = cfg.get('username', '')
    if not username:
        return 'Не указан пользователь'
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return f'Пользователь {username} не найден'
    entity = _get_entity(instance)
    if not entity:
        return 'Сущность не найдена'
    if hasattr(entity, 'assigned_to'):
        entity.assigned_to = user
        entity.save(update_fields=['assigned_to'])
        return f'Назначен пользователь {username}'
    return 'Сущность не поддерживает назначение пользователя'


def _action_webhook(rule, instance) -> str:
    """Вызвать внешний webhook."""
    import json
    try:
        import urllib.request
        cfg = rule.config
        url = cfg.get('url', '')
        if not url:
            return 'URL не указан'
        payload = json.dumps({
            'workflow': instance.workflow.name,
            'entity_type': instance.entity_type,
            'entity_id': instance.entity_id,
            'current_step': instance.current_step.name if instance.current_step else None,
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return f'Webhook OK: {resp.status}'
    except Exception as exc:
        return f'Webhook ошибка: {exc}'


def _action_sla_breach_notify(rule, instance) -> str:
    """Уведомление о просрочке SLA.

    Предполагается, что правило вызвано из management-команды check_sla_breaches
    (см. instance.sla_breach_context, если задан) или при смене статуса —
    в последнем случае сработает только если на текущем шаге действительно нарушен SLA.

    config:
      subject, message, recipients — как у send_notify
      breach_info — необязательный текст-префикс, добавляется к message
    """
    # Если контекст нарушения передан — встроим его в message
    ctx = getattr(instance, 'sla_breach_context', None)
    if ctx:
        cfg = dict(rule.config)
        prefix = (
            f'[SLA нарушен] Поле {ctx.get("status_field")} = {ctx.get("status_value")}, '
            f'норматив {ctx.get("hours")}ч, просрочка {ctx.get("overdue_hours"):.1f}ч.\n\n'
        )
        cfg['message'] = prefix + cfg.get('message', '')
        rule = type(rule)(id=rule.id, workflow=rule.workflow, transition=rule.transition,
                          name=rule.name, action_type='send_notify', config=cfg,
                          is_active=rule.is_active, order=rule.order)
    return _action_send_notify(rule, instance)


# ──────────────────────────────────────────────────────────────────────────────
# Сериализация шагов
# ──────────────────────────────────────────────────────────────────────────────

def _step_to_dict(step) -> dict:
    if step is None:
        return None
    return {
        'id': step.pk,
        'name': step.name,
        'step_type': step.step_type,
        'stage': step.stage,
        'hawb_logistics_status': step.hawb_logistics_status,
        'hawb_customs_status': step.hawb_customs_status,
        'color': step.color,
        'order': step.order,
    }
