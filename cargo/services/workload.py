"""Сервис планирования нагрузки сотрудников.

Расчёт прогнозируемой нагрузки на каждого исполнителя по дням, обнаружение
перегруза и построение рекомендаций по перераспределению накладных.

Основные функции:
    compute_user_capacity(user, date) -> минут доступного времени за день
    compute_user_load(user, date)     -> минут нагрузки по плану
    compute_load_grid(date_from, date_to) -> матрица user × date
    find_overloaded(date)             -> перегруженные + свободные
    build_rebalance_plan(date)        -> список рекомендаций
    apply_rebalance_plan(plan, by_user) -> применить план, лог в БД
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Iterable

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

from ..models import (
    DEFAULT_WORK_SCHEDULE,
    HouseWaybill,
    ProcessingNorm,
    UserProfile,
    WorkloadRebalanceLog,
    WorkScheduleException,
)


# Терминальные логистические статусы — накладная больше не требует работы.
TERMINAL_LOGISTICS_STATUSES = ('DELIVERED', 'RETURNED', 'LOST')

# Маппинг weekday() (0=пн … 6=вс) в ключи work_schedule.
_WEEKDAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


# ── Нормативы ─────────────────────────────────────────────────────────────────

def get_norms_map() -> dict[tuple[str, str], int]:
    """Возвращает {(shipment_type, cargo_type): minutes} для активных нормативов.

    Не кэшируется глобально — Django вызывает функцию из view, где per-request
    кэш достаточен; админ может менять нормативы и сразу видеть эффект.
    """
    return {
        (n.shipment_type, n.cargo_type): n.minutes
        for n in ProcessingNorm.objects.filter(is_active=True)
    }


def get_norm_minutes(norms: dict, shipment_type: str, cargo_type: str) -> int:
    """Норматив для конкретной HAWB. Если нет в БД — fallback 30 мин."""
    return norms.get((shipment_type, cargo_type), 30)


# ── Профиль и capacity ────────────────────────────────────────────────────────

def _get_or_default_profile(user: User) -> dict:
    """Возвращает словарь параметров профиля; для пользователей без профиля
    подставляет дефолты (используется старыми тестовыми данными)."""
    try:
        p = user.profile
        return {
            'timezone': p.timezone or 'Europe/Moscow',
            'is_active_op': p.is_active_op,
            'work_schedule': p.work_schedule or DEFAULT_WORK_SCHEDULE,
            'daily_capacity_minutes': p.daily_capacity_minutes or 0,
            'primary_role': p.primary_role,
        }
    except UserProfile.DoesNotExist:
        return {
            'timezone': 'Europe/Moscow',
            'is_active_op': bool(user.is_active),
            'work_schedule': DEFAULT_WORK_SCHEDULE,
            'daily_capacity_minutes': 0,
            'primary_role': '',
        }


def _schedule_minutes(schedule: dict, weekday: int) -> int:
    """Сумма минут рабочих интервалов на weekday (0=пн)."""
    if not isinstance(schedule, dict):
        return 0
    intervals = schedule.get(_WEEKDAY_KEYS[weekday], [])
    total = 0
    for pair in intervals or []:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            continue
        try:
            h1, m1 = (int(x) for x in pair[0].split(':'))
            h2, m2 = (int(x) for x in pair[1].split(':'))
        except (ValueError, AttributeError):
            continue
        start = h1 * 60 + m1
        end = h2 * 60 + m2
        if end > start:
            total += end - start
    return total


def _is_excepted(user: User, date: _dt.date) -> bool:
    """True, если на эту дату у сотрудника есть исключение (отпуск/больничный)."""
    return WorkScheduleException.objects.filter(
        user=user, date_from__lte=date, date_to__gte=date,
    ).exists()


def compute_user_capacity(user: User, date: _dt.date) -> int:
    """Доступное время сотрудника на дату (минут). 0 — выходной/неактивен/в отпуске."""
    profile = _get_or_default_profile(user)
    if not profile['is_active_op']:
        return 0
    if _is_excepted(user, date):
        return 0
    schedule_minutes = _schedule_minutes(profile['work_schedule'], date.weekday())
    override = profile['daily_capacity_minutes']
    if override > 0:
        return min(schedule_minutes, override) if schedule_minutes else override
    return schedule_minutes


# ── HAWB-якорь даты и нагрузка ────────────────────────────────────────────────

def _hawb_anchor_date(hawb) -> _dt.date | None:
    """Дата, к которой относим работу по HAWB.

    Приоритет: flight_date партии (плановое прибытие) → дата последней смены
    лог. статуса → дата создания.
    """
    if hawb.mawb_id and hawb.mawb and hawb.mawb.flight_date:
        return hawb.mawb.flight_date
    if hawb.logistics_status_date:
        return timezone.localtime(hawb.logistics_status_date).date()
    if hawb.created_at:
        return timezone.localtime(hawb.created_at).date()
    return None


def get_active_hawb_qs(date_from: _dt.date, date_to: _dt.date, user: User | None = None):
    """QuerySet HAWB, активных в окне [date_from, date_to].

    Фильтрация по дате-якорю выполняется частично в SQL (по mawb__flight_date)
    и доуточняется в Python при необходимости (logistics_status_date берётся
    fallback'ом). Для широких окон — приемлемо: основная масса HAWB имеет
    flight_date.
    """
    qs = HouseWaybill.objects.exclude(
        logistics_status__in=TERMINAL_LOGISTICS_STATUSES,
    ).select_related('mawb')
    if user is not None:
        qs = qs.filter(assigned_to=user)

    # Расширяем фильтр: HAWB может попадать в окно либо через mawb.flight_date,
    # либо через logistics_status_date (для standalone HAWB), либо через created_at.
    qs = qs.filter(
        Q(mawb__flight_date__gte=date_from, mawb__flight_date__lte=date_to)
        | Q(mawb__isnull=True, logistics_status_date__date__gte=date_from,
            logistics_status_date__date__lte=date_to)
        | Q(mawb__isnull=True, logistics_status_date__isnull=True,
            created_at__date__gte=date_from, created_at__date__lte=date_to)
    )
    return qs


def compute_user_load(user: User, date: _dt.date, norms: dict | None = None) -> tuple[int, int]:
    """Возвращает (load_minutes, hawb_count) на конкретную дату."""
    if norms is None:
        norms = get_norms_map()
    qs = get_active_hawb_qs(date, date, user=user)
    total = 0
    count = 0
    for h in qs:
        anchor = _hawb_anchor_date(h)
        if anchor != date:
            continue
        total += get_norm_minutes(norms, h.shipment_type, h.cargo_type)
        count += 1
    return total, count


# ── Сетка нагрузки команды ────────────────────────────────────────────────────

@dataclass
class LoadCell:
    user_id: int
    user_name: str
    date: str  # ISO
    weekday: int
    load_minutes: int
    capacity_minutes: int
    hawb_count: int
    ratio: float  # load/capacity, 0 если capacity=0

    @property
    def status(self) -> str:
        if self.capacity_minutes == 0:
            return 'off' if self.load_minutes == 0 else 'overload'
        if self.ratio < 0.7:
            return 'free'
        if self.ratio <= 1.0:
            return 'busy'
        if self.ratio <= 1.3:
            return 'overload'
        return 'critical'


def compute_load_grid(date_from: _dt.date, date_to: _dt.date,
                      include_inactive: bool = False) -> list[LoadCell]:
    """Список ячеек user × date с нагрузкой и capacity.

    Возвращаются только пользователи, у которых либо есть назначенные HAWB
    в окне, либо есть активный профиль (is_active_op=True).
    """
    norms = get_norms_map()

    # Все HAWB в окне с привязкой к assigned_to
    hawb_qs = get_active_hawb_qs(date_from, date_to).filter(
        assigned_to__isnull=False,
    )

    # Соберём активных пользователей по профилям + всех, у кого есть HAWB
    profile_users = User.objects.select_related('profile').filter(
        profile__is_active_op=True,
    ) if not include_inactive else User.objects.select_related('profile').all()
    user_ids_with_hawb = set(hawb_qs.values_list('assigned_to_id', flat=True))
    user_ids = set(profile_users.values_list('id', flat=True)) | user_ids_with_hawb
    if not user_ids:
        return []

    users = {u.id: u for u in User.objects.filter(id__in=user_ids).select_related('profile')}

    # Группируем нагрузку: (user_id, anchor_date) → (minutes, count)
    bucket: dict[tuple[int, _dt.date], list[int]] = defaultdict(lambda: [0, 0])
    for h in hawb_qs:
        anchor = _hawb_anchor_date(h)
        if not anchor or anchor < date_from or anchor > date_to:
            continue
        key = (h.assigned_to_id, anchor)
        minutes = get_norm_minutes(norms, h.shipment_type, h.cargo_type)
        bucket[key][0] += minutes
        bucket[key][1] += 1

    # Загрузим исключения (отпуск/больничный) в окне одним запросом
    exc_map: dict[int, set[_dt.date]] = defaultdict(set)
    for exc in WorkScheduleException.objects.filter(
        user_id__in=user_ids,
        date_from__lte=date_to,
        date_to__gte=date_from,
    ).values('user_id', 'date_from', 'date_to'):
        d = max(exc['date_from'], date_from)
        end = min(exc['date_to'], date_to)
        while d <= end:
            exc_map[exc['user_id']].add(d)
            d += _dt.timedelta(days=1)

    # Формируем сетку
    days = (date_to - date_from).days + 1
    grid: list[LoadCell] = []
    for uid, user in sorted(users.items(), key=lambda kv: kv[1].last_name or kv[1].username):
        for i in range(days):
            d = date_from + _dt.timedelta(days=i)
            if d in exc_map.get(uid, ()):
                cap = 0
            else:
                cap = compute_user_capacity(user, d)
            load, cnt = bucket.get((uid, d), [0, 0])
            ratio = (load / cap) if cap > 0 else (0.0 if load == 0 else float('inf'))
            grid.append(LoadCell(
                user_id=uid,
                user_name=user.get_full_name() or user.username,
                date=d.isoformat(),
                weekday=d.weekday(),
                load_minutes=load,
                capacity_minutes=cap,
                hawb_count=cnt,
                ratio=round(ratio, 2) if ratio != float('inf') else 999.0,
            ))
    return grid


def cells_to_dicts(cells: Iterable[LoadCell]) -> list[dict]:
    return [{**asdict(c), 'status': c.status} for c in cells]


# ── Поиск перегруженных и план перераспределения ──────────────────────────────

@dataclass
class RebalanceAction:
    hawb_id: int
    hawb_number: str
    from_user_id: int | None
    from_user_name: str
    to_user_id: int
    to_user_name: str
    minutes: int
    reason: str  # короткое пояснение


def find_overloaded(date: _dt.date) -> dict:
    """Находит перегруженных и свободных на дату.

    Возвращает {'overloaded': [...], 'free': [...]} в формате для виджета.
    """
    grid = compute_load_grid(date, date)
    overloaded = []
    free = []
    for c in grid:
        if c.capacity_minutes <= 0:
            continue
        if c.ratio > 1.0:
            overloaded.append(c)
        elif c.ratio < 0.7:
            free.append(c)
    overloaded.sort(key=lambda c: -c.ratio)
    free.sort(key=lambda c: c.ratio)
    return {
        'date': date.isoformat(),
        'overloaded': cells_to_dicts(overloaded),
        'free': cells_to_dicts(free),
    }


def build_rebalance_plan(date: _dt.date) -> list[RebalanceAction]:
    """Жадный план: с каждого перегруженного снимаем самые лёгкие HAWB и
    отдаём самому свободному, пока ratio не ≤ 1.0 или больше некому отдать.
    """
    norms = get_norms_map()
    grid = compute_load_grid(date, date)

    # Текущая нагрузка/ёмкость на пользователя
    state = {c.user_id: {'load': c.load_minutes, 'cap': c.capacity_minutes,
                         'name': c.user_name} for c in grid}
    # HAWB перегруженных, отсортированные по нагрузке asc — лёгкие сначала
    overloaded_ids = [c.user_id for c in grid if c.capacity_minutes > 0 and c.ratio > 1.0]
    if not overloaded_ids:
        return []

    actions: list[RebalanceAction] = []

    for src_id in overloaded_ids:
        hawbs = list(get_active_hawb_qs(date, date, user=User.objects.get(id=src_id))
                     .select_related('mawb'))
        # Оставляем только те, чей anchor == date
        hawbs = [h for h in hawbs if _hawb_anchor_date(h) == date]
        # Сортируем по минутам asc (отдаём лёгкие — меньше потеря контекста)
        hawbs.sort(key=lambda h: get_norm_minutes(norms, h.shipment_type, h.cargo_type))

        for h in hawbs:
            if state[src_id]['load'] <= state[src_id]['cap']:
                break
            minutes = get_norm_minutes(norms, h.shipment_type, h.cargo_type)
            # Ищем самого свободного — capacity > 0 и максимальный запас
            target_id = None
            target_slack = -1
            for uid, s in state.items():
                if uid == src_id or s['cap'] <= 0:
                    continue
                slack = s['cap'] - s['load']
                if slack >= minutes and slack > target_slack:
                    target_slack = slack
                    target_id = uid
            if target_id is None:
                break

            actions.append(RebalanceAction(
                hawb_id=h.id,
                hawb_number=h.hawb_number,
                from_user_id=src_id,
                from_user_name=state[src_id]['name'],
                to_user_id=target_id,
                to_user_name=state[target_id]['name'],
                minutes=minutes,
                reason=f'Перегруз {state[src_id]["name"]} (>100%) — снимаем {minutes} мин',
            ))
            # Обновляем виртуальное состояние
            state[src_id]['load'] -= minutes
            state[target_id]['load'] += minutes

    return actions


def actions_to_dicts(actions: Iterable[RebalanceAction]) -> list[dict]:
    return [asdict(a) for a in actions]


@transaction.atomic
def apply_rebalance_plan(actions: list[dict], by_user: User,
                         target_date: _dt.date) -> dict:
    """Применяет план к БД: меняет HouseWaybill.assigned_to и пишет лог.

    actions — список словарей с ключами hawb_id, from_user_id, to_user_id.
    Возвращает {'applied': N, 'errors': [...], 'log_ids': [...]}.
    """
    applied = 0
    errors = []
    log_ids = []
    for a in actions:
        try:
            hawb = HouseWaybill.objects.select_for_update().get(id=a['hawb_id'])
        except HouseWaybill.DoesNotExist:
            errors.append(f"HAWB #{a['hawb_id']} не найдена")
            continue
        if hawb.assigned_to_id != a.get('from_user_id'):
            errors.append(
                f"HAWB {hawb.hawb_number} уже переназначена (ожидался "
                f"#{a.get('from_user_id')}, у HAWB #{hawb.assigned_to_id})"
            )
            continue
        hawb.assigned_to_id = a['to_user_id']
        hawb.save(update_fields=['assigned_to'])
        log = WorkloadRebalanceLog.objects.create(
            hawb=hawb,
            from_user_id=a.get('from_user_id'),
            to_user_id=a['to_user_id'],
            reason='overload_manual',
            target_date=target_date,
            created_by=by_user,
        )
        log_ids.append(log.id)
        applied += 1
    return {'applied': applied, 'errors': errors, 'log_ids': log_ids}


# ── My day ────────────────────────────────────────────────────────────────────

def compute_my_day(user: User, date: _dt.date) -> dict:
    """Данные для виджета «Мой день»: список HAWB + сводка по нагрузке."""
    norms = get_norms_map()
    qs = get_active_hawb_qs(date, date, user=user).order_by('logistics_status', 'hawb_number')
    items = []
    total_minutes = 0
    for h in qs:
        if _hawb_anchor_date(h) != date:
            continue
        minutes = get_norm_minutes(norms, h.shipment_type, h.cargo_type)
        total_minutes += minutes
        items.append({
            'id': h.id,
            'hawb_number': h.hawb_number,
            'cargo_type': h.cargo_type,
            'shipment_type': h.shipment_type,
            'logistics_status': h.logistics_status,
            'logistics_status_label': h.get_logistics_status_display(),
            'mawb_number': h.mawb.awb_number if h.mawb_id else None,
            'consignee_name': h.consignee_name,
            'minutes': minutes,
        })
    capacity = compute_user_capacity(user, date)
    return {
        'date': date.isoformat(),
        'user_name': user.get_full_name() or user.username,
        'items': items,
        'total_minutes': total_minutes,
        'capacity_minutes': capacity,
        'ratio': round(total_minutes / capacity, 2) if capacity > 0 else 0,
    }


# ── Прогноз нагрузки на N дней ────────────────────────────────────────────────

def compute_workload_forecast(date_from: _dt.date, days: int = 7) -> list[dict]:
    """Агрегированная нагрузка команды по дням (для виджета forecast)."""
    date_to = date_from + _dt.timedelta(days=days - 1)
    grid = compute_load_grid(date_from, date_to)
    by_date: dict[str, dict] = {}
    for c in grid:
        d = by_date.setdefault(c.date, {
            'date': c.date,
            'weekday': c.weekday,
            'load_minutes': 0,
            'capacity_minutes': 0,
            'hawb_count': 0,
            'overloaded_users': 0,
        })
        d['load_minutes'] += c.load_minutes
        d['capacity_minutes'] += c.capacity_minutes
        d['hawb_count'] += c.hawb_count
        if c.ratio > 1.0 and c.capacity_minutes > 0:
            d['overloaded_users'] += 1
    # Гарантируем все даты в диапазоне (даже без HAWB)
    result = []
    for i in range(days):
        d = date_from + _dt.timedelta(days=i)
        ds = d.isoformat()
        result.append(by_date.get(ds, {
            'date': ds,
            'weekday': d.weekday(),
            'load_minutes': 0,
            'capacity_minutes': 0,
            'hawb_count': 0,
            'overloaded_users': 0,
        }))
    return result
