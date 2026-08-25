"""Последовательный конвейер обслуживания CargoTrack — ОДИН писатель за раз.

Заменяет пул из ~15 ПАРАЛЛЕЛЬНЫХ пишущих кронов одним ПОСЛЕДОВАТЕЛЬНЫМ
каналом. Мотив: SQLite = один писатель. Когда на :00/:30 совпадали
10-14 write-кронов (import_sheets, writeback, reconcile_*, redispatch_*,
svh_*, audit_* и дубли без лока), они лочили друг друга (`database is
locked`, 10-36/час), import растягивался на 13+ мин, waitress-воркеры
задыхались на write-локе (health timeout). Последовательный прогон
устраняет write-конкуренцию ПО ПОСТРОЕНИЮ — в любой момент пишет ровно
один шаг.

Три канала новой архитектуры (см. cron-pipeline-plan в памяти):
  • REALTIME (часто, легко, СВОИ локи): crm_sync_incremental, sync_hide_state
  • MAINTENANCE (ЭТОТ конвейер, раз в 30-60 мин): всё остальное, по очереди
  • DAILY (ночь): crm reindex, wal_maint, prune_arrival, backfill_do1_goods

ПОРЯДОК ШАГОВ ВАЖЕН — три фазы:
  1. ИНГЕСТ      — Sheets→БД, привязка по ТСД, склады (МС/Шер/Декларант),
                   «ТО КЛИЕНТ», СДЭК, выравнивание filed_date.
  2. РЕКОНСАЙЛ   — свипер-команды досводят выпуски/номера ДТ/СВХ в БД.
                   Все они dry-run ПО УМОЛЧАНИЮ → здесь идут с apply=True.
  3. ФИНАЛИЗАЦИЯ — audit --fix пушит в Sheets ВСЁ, что реконсайл записал в
                   БД (поэтому audit В КОНЦЕ, а не в середине как в auto_sync),
                   + экспорт-статистика.
Так изменения БД попадают в листы В ТОМ ЖЕ прогоне, без лага на цикл.

Каждый шаг в try/except — падение одного НЕ рушит конвейер (лог ERROR,
идём дальше). Защита от двойного запуска — lockfile run_maintenance.lock
(cron_lock: проверка ЖИВОСТИ PID владельца, не только возраста).

    manage.py run_maintenance                 # полный прогон (с локом)
    manage.py run_maintenance --no-lock       # ручной прогон без лока
    manage.py run_maintenance --full          # + reparse_alta_inbox (медленно)
    manage.py run_maintenance --only missing_decl,writeback_export
    manage.py run_maintenance --list          # только показать шаги, не запуская

Этот конвейер — АВТОРИТЕТНАЯ последовательность обслуживания. Команда
`auto_sync` покрывала только фазу ингеста (+ audit в середине) и остаётся
для ручного использования, но её крон отключён в пользу run_maintenance.
"""
from __future__ import annotations

import datetime
import os
import sys
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand


LOCK_DIR = os.path.join(os.path.dirname(sys.executable), '..', '..', 'tmp')
LOCK_PATH = os.path.join(os.path.abspath(LOCK_DIR), 'run_maintenance.lock')
# Маркер успешного ЗАВЕРШЕНИЯ полного прогона (для cron_watchdog: детект
# «частичного стопа» — прогоны идут, но не доигрывают до конца).
DONE_MARKER_PATH = os.path.join(os.path.abspath(LOCK_DIR), 'run_maintenance_done.marker')
# Конвейер последовательный и длинный (импорт+аудит+свиперы). Возраст —
# лишь страховка на случай неразбираемого PID; основная защита — pid_alive.
LOCK_STALE_AFTER_SEC = 60 * 60  # 60 минут


# (label, command, kwargs). label — короткий ярлык для --only/логов/сводки;
# для двух прогонов audit_sheets_vs_db нужны РАЗНЫЕ ярлыки при одной команде.
INGEST_STEPS = [
    ('import_general',   'import_sheets',           {'source': 'general'}),
    # CRM-источники («Сводная по СТО», «Парсинг (СВОДНАЯ ВЭД)») — нужны
    # delete_to_client_hawbs (читает пометки «ТО КЛИЕНТ» из ImportedSheetRow).
    ('import_crm',       'import_sheets',           {'source': 'crm'}),
    ('relink_tsd',       'relink_hawbs_from_tsd',    {'all': True}),
    ('moscow_cargo',     'refresh_moscow_cargo',     {}),
    ('shercargo',        'refresh_shercargo',        {}),
    # sync_deklarant_svh/delete_to_client/sync_cdek — no-op если фича выключена
    # (гейт внутри команды). delete_to_client держит СВОЙ лок — под нашим это ок.
    ('deklarant_svh',    'sync_deklarant_svh',       {'limit': 100}),
    ('to_client',        'delete_to_client_hawbs',   {'apply': True}),
    ('cdek_statuses',    'sync_cdek_statuses',       {}),
    # Выравнивает filed_date по siblings ДТ (гонка CMN.11023 до CMN.11350).
    ('filed_dates',      'sync_filed_dates',         {}),
]

RECONCILE_STEPS = [
    # Пере-диспатч race-застрявших сообщений на СУЩЕСТВУЮЩИЕ HAWB. Гонка
    # (особенно ЭКСПОРТ): таможенный выпуск CMN.11341 приходит на секунды
    # раньше, чем наша исходящая подача создаёт HAWB → dispatch не нашёл
    # HAWB → status_applied=False; а из-за пустого initial_envelope обычные
    # re-dispatch-свиперы это не ловят. Идёт ПЕРВЫМ — чтобы применённые
    # выпуски/статусы прошли через остальные реконсайлы и audit в ЭТОМ же
    # прогоне. Дёшево: трогает только сообщения, чей waybill = наш HAWB.
    ('redispatch_matched', 'redispatch_matched_unapplied',  {'apply': True}),
    ('crm_releases',     'reconcile_crm_releases',          {'apply': True}),
    # lean=True: bulk_update вместо полного re-dispatch. Без lean один
    # проблемный финал (большой raw_xml ДТЭГ) держит write-транзакцию минуты,
    # бьётся с realtime crm_sync за SQLite-лок → 6 ретраев × ~3.5мин = ~20 мин
    # на ОДНУ накладную (кейс 10274180730, 26.07). lean пишет статус коротким
    # bulk_update; листы догонит audit_general (следующий шаг) + crm_sync.
    ('stuck_finals',     'redispatch_stuck_finals',         {'apply': True, 'lean': True}),
    ('consign_releases', 'reconcile_consignment_releases',  {'apply': True}),
    ('missing_decl',     'reconcile_missing_decl',          {'apply': True}),
    ('adopt_svh',        'adopt_svh_orphans',               {'apply': True}),
    ('rematch_svh',      'redispatch_unmatched_svh',        {'apply': True}),
    ('arrival_from_do1', 'set_arrival_from_do1',            {'apply': True}),
    ('tsd_from_cargo',   'backfill_tsd_from_cargo',         {'apply': True}),
    # Транзит: партия пришла к нам транзитом (transit_doc), свой ДО1 часто не
    # приходит → scan_into_bond «застревает» на ранней авиа-дате, а лицензия/
    # ТСД уже транзитные (рассинхрон «половина авиа / половина партии»). Сдвигаем
    # дату размещения к дате нашей описи (вперёд), ТСД←transit_doc (перебивая
    # авиа-номер), «дата прибытия»←дата размещения (для транзита юзер разрешил
    # перетирать). Идёт ПОСЛЕ tsd_from_cargo/arrival_from_do1 — финальное слово
    # по транзитным строкам. Идемпотентно (diff-based).
    ('transit_placement', 'reconcile_transit_placement',    {'apply': True}),
    # Бизнес-правило: при отказе/отзыве рег.номер ДТ анулирован — стираем
    # customs_declaration_number (с фильтром переподачи: если после отказа
    # пришла новая регистрация — ДТ валидна, не трогаем). Иначе «Общее»
    # показывает «Отказано» + номер ДТ (номер валиден только при выпуске,
    # как в CRM-вкладках). Идёт ПОСЛЕ реконсиляций, присваивающих ДТ.
    ('cleanup_rej_decls', 'cleanup_rejected_decls',         {}),
]

# audit --force: gate «import свежее 5 мин» теперь soft-warning (аудит
# sort-proof, таргетит живые строки), но import был в начале ЭТОГО же прогона
# — force убирает лишний warning в логах. audit ПОСЛЕ реконсайла.
FINALIZE_STEPS = [
    ('audit_general',    'audit_sheets_vs_db',  {'fix': True, 'kind': 'general', 'force': True}),
    # Рассылка per-HAWB реестров ДТЭГ по свежим выпускам (отдельный поток,
    # независимый от службы «Регистрация» Альты). Лёгкий: читает несколько
    # RELEASED-строк, шлёт до REESTR_MAILER_CAP писем, дедуп через
    # ReleaseReestrNotification. No-op пока REESTR_MAILER_ENABLED=False.
    # Идёт ПОСЛЕ реконсайла — чтобы выпуск/номер ДТ уже были в БД.
    ('release_reestrs',  'send_release_reestrs', {}),
    # Рассылка регулярных ДТ (печатный бланк-PDF из Альты). Лёгкий: берёт
    # pending-документы IncomingDTDocument (их создаёт приём /api/v1/dt/pdf/),
    # шлёт до DT_MAILER_CAP писем, ротирует старые PDF. No-op пока
    # DT_MAILER_ENABLED=False.
    ('release_dt',       'send_release_dt', {}),
    # Авто-комакт на СВХ Внуково по свежим выпускам (svh-komakt-automation).
    # Лёгкий: берёт RELEASED-импорт Внуково без KomaktNotification, шлёт до
    # KOMAKT_MAILER_CAP писем (Excel-разрез ДТЭГ по ТН ВЭД). No-op пока
    # KOMAKT_MAILER_ENABLED=False. Идёт ПОСЛЕ реконсайла (выпуск/номер ДТ в БД).
    ('svh_komakts',      'send_svh_komakts', {}),
]

# Тяжёлые Sheets-шаги экспорта — ТОЛЬКО в --full (PT6H). В hourly они раздували
# прогон: audit_export + покраска 3653 ячеек = ~4 мин Sheets-вызовов, а под
# 429-throttling (общая квота Sheets исчерпана: конвейер + realtime + reindex)
# растягивались до убийства по лимиту. Экспортная статистика — отчётная
# вкладка, ежечасная свежесть не нужна; 6ч достаточно.
FULL_EXPORT_STEPS = [
    ('audit_export',     'audit_sheets_vs_db',  {'fix': True, 'kind': 'export',  'force': True}),
    ('writeback_export', 'writeback_all_export', {}),
]

# Опциональный шаг (--full): переразбор + пере-диспатч ТОЛЬКО unapplied
# сообщений (status_applied=False). Раньше был --force-dispatch по ВСЕМ ~52k
# сообщениям → reparse не укладывался в лимит PT1H и Full убивался (rc=267014),
# из-за чего FULL_EXPORT_STEPS (экспорт-аудит) вообще не доходили. --unapplied
# режет набор до застрявших (~34k, из них почти всё чужой трафик, match=no-op)
# и служит широкой сеткой для race-стрелков, чей waybill только в consignments
# (redispatch_matched в hourly ловит по waybill_number_raw — быстрый путь).
# Полный переразбор всех сообщений (при обновлении парсера) — вручную:
#   manage.py reparse_alta_inbox --force-dispatch
FULL_STEP = ('reparse', 'reparse_alta_inbox', {'unapplied': True})


def _acquire_lock() -> bool:
    from cargo.services.cron_lock import acquire
    return acquire(LOCK_PATH, LOCK_STALE_AFTER_SEC)


def _release_lock() -> None:
    from cargo.services.cron_lock import release
    release(LOCK_PATH)


class Command(BaseCommand):
    help = 'Последовательный конвейер обслуживания (для Task Scheduler).'

    def add_arguments(self, parser):
        parser.add_argument('--no-lock', action='store_true',
                            help='Игнорировать lockfile (для ручного прогона)')
        parser.add_argument('--full', action='store_true',
                            help='+ экспорт-аудит в конце (FULL_EXPORT_STEPS)')
        parser.add_argument('--reparse', action='store_true',
                            help='+ reparse_alta_inbox --unapplied (широкая сетка по '
                                 'застрявшим, МЕДЛЕННО — держит общий лок минуты, '
                                 'ставит на паузу hourly-конвейер). Race-выпуски и '
                                 'так лечит hourly-шаг redispatch_matched, поэтому в '
                                 'штатном --full НЕ включается. Полный переразбор '
                                 'после смены парсера: reparse_alta_inbox --force-dispatch')
        parser.add_argument('--only', default='',
                            help='Только эти шаги (label или команда), через запятую')
        parser.add_argument('--list', action='store_true',
                            help='Показать шаги и выйти, ничего не запуская')

    def _steps(self, opts):
        steps = list(INGEST_STEPS)
        # reparse — ТОЛЬКО по явному --reparse (не по --full). Он держит общий
        # лок минуты и пауз ит hourly-конвейер (freshness ≤20 мин), а его
        # ценность (пере-диспатч race-застрявших) закрыта hourly-шагом
        # redispatch_matched. Штатный Full остаётся лёгким (~12 мин).
        if opts.get('reparse'):
            steps.append(FULL_STEP)
        steps += RECONCILE_STEPS + FINALIZE_STEPS
        if opts['full']:
            steps += FULL_EXPORT_STEPS  # тяжёлый экспорт-хвост только в --full
        only = {s.strip() for s in opts['only'].split(',') if s.strip()}
        if only:
            steps = [s for s in steps if s[0] in only or s[1] in only]
            missing = only - {s[0] for s in steps} - {s[1] for s in steps}
            if missing:
                self.stdout.write(self.style.WARNING(
                    f'--only: не найдены шаги {sorted(missing)}'))
        return steps

    def handle(self, *args, **opts):
        steps = self._steps(opts)

        if opts['list']:
            for label, cmd, kwargs in steps:
                self.stdout.write(f'  {label:18} → {cmd} {kwargs}')
            self.stdout.write(f'ИТОГО шагов: {len(steps)}')
            return

        # Пропускаем шаги, чьей команды нет на этом инстансе (напр. CDEK на
        # паузе — sync_cdek_statuses не задеплоен). Это SKIP, а не FAILED —
        # чтобы сводка FAILED означала РЕАЛЬНЫЕ сбои, а не отсутствие фичи.
        from django.core.management import get_commands
        available = set(get_commands())
        for label, cmd, _kw in steps:
            if cmd not in available:
                self.stdout.write(self.style.WARNING(
                    f'  SKIP {label} ({cmd}) — команда не установлена'))
        steps = [s for s in steps if s[1] in available]

        started = datetime.datetime.now()
        self.stdout.write(f'[{started.isoformat()}] run_maintenance start '
                          f'(full={opts["full"]}, шагов={len(steps)})')

        if not opts['no_lock']:
            if not _acquire_lock():
                self.stdout.write(self.style.WARNING(
                    'Предыдущий прогон ещё работает (lock занят). Выхожу.'))
                return

        ok, failed = [], []
        try:
            for label, cmd, kwargs in steps:
                step_started = time.monotonic()
                self.stdout.write('')
                self.stdout.write(self.style.NOTICE(
                    f'[{datetime.datetime.now().isoformat()}] → {label} ({cmd})'))
                try:
                    call_command(cmd, **kwargs)
                    ok.append(label)
                except Exception as e:
                    failed.append(label)
                    self.stdout.write(self.style.ERROR(
                        f'  ШАГ {label} ({cmd}) FAILED: {e}'))
                finally:
                    dt = time.monotonic() - step_started
                    self.stdout.write(f'  {label}: {dt:.0f} сек')
        finally:
            if not opts['no_lock']:
                _release_lock()

        elapsed = (datetime.datetime.now() - started).total_seconds()
        self.stdout.write('')
        style = self.style.SUCCESS if not failed else self.style.WARNING
        self.stdout.write(style(
            f'run_maintenance done за {elapsed:.0f} сек — '
            f'OK {len(ok)}, FAILED {len(failed)}'
            + (f': {failed}' if failed else '')))

        # Маркер ЗАВЕРШЕНИЯ прогона — метка времени последнего доигранного до
        # конца конвейера. cron_watchdog проверяет его возраст: если давно нет
        # завершений (прогоны стартуют, но убиваются на середине — «частичный
        # стоп», инцидент 24-25.08.2026), сторож бьёт тревогу и перезапускает.
        # Пишем в самом конце handle(): маркер = «дошли до конца» (даже с FAILED
        # шагами — это не стоп, а частные сбои). Строкой '--only' не портим
        # (это ручной прогон подмножества, а не полный конвейер).
        if not opts.get('only'):
            try:
                with open(DONE_MARKER_PATH, 'w', encoding='utf-8') as _f:
                    _f.write(datetime.datetime.now().isoformat())
            except OSError:
                pass
