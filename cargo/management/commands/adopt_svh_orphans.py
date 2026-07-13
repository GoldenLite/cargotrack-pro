"""Sweeper: усыновляет HAWB-сирот в их партию по СВХ-сообщениям.

ПРОБЛЕМА: специалисты вносят отдельные накладные (HAWB) в CRM/«Общее»,
CargoTrack создаёт их как СИРОТ (mawb=NULL) — номер партии (MAWB/транзит)
в лист не пишут. Параллельно таможня присылает СВХ-представление
(CMN.13029) / регистрацию ДО1 (CMN.13010), где эти HAWB перечислены под
номером партии (svh_mawb, напр. транзитный «070726-5»). Раньше такие
сообщения повисали cargo=None (партии в БД нет), а сироты навсегда
оставались без партии / склада / даты — в «Общее»/CRM пусто по СВХ.

РЕШЕНИЕ (то, что просил юзер: «видим сироту, знаем её партию → заводим
Cargo и вносим всю инфу»): для несматченного СВХ-сообщения, если его
svh_mawb ещё НЕ Cargo, НО среди перечисленных в XML HAWB есть наши сироты
— создаём минимальный Cargo(svh_mawb, stage=DRAFT), привязываем сирот
(_link_hawbs_to_cargo — прямой UPDATE + запись «номера партии» в Sheets)
и передиспатчиваем сообщения партии: match_svh находит новый Cargo →
apply_svh_placement проставляет лицензию СВХ.

Дата прибытия из ПРЕДСТАВЛЕНИЯ (CMN.13029) НЕ ставится — apply_svh_placement
пишет только warehouse_license; scan_into_bond ставит лишь полный ДО1
(CMN.13010). Это ровно правило юзера для партий 070726-* «представление =
только лицензия». Когда придёт ДО1 — он сам сматчится к уже созданному
Cargo и проставит дату.

КОНСЕРВАТИВНО: Cargo создаётся ТОЛЬКО если ≥1 наша сирота есть в сообщении
— не плодим партии, которые мы не ведём. HAWB из XML фильтруются через БД
(mawb=NULL), поэтому ложные 11-значные последовательности отсеиваются.

Идемпотентно, durable — можно кроном:
    manage.py adopt_svh_orphans                       # dry-run
    manage.py adopt_svh_orphans --apply
    manage.py adopt_svh_orphans --apply --mawb 070726-5   # одна партия
    manage.py adopt_svh_orphans --apply --limit 20
"""
import re

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, Cargo, HouseWaybill

SVH_KINDS = ['svh_placed', 'svh_do1_registered']
# HAWB (11 цифр) в XML лежат внутри описания товара:
# «...ИНД. НАКЛАДНАЯ 10282772561 КОНСЕРВИРОВАННЫЕ...». svh_mawb (070726-5)
# и рег.номера (10001020/080726/5007997) содержат дефис/слэш → не ловятся.
HAWB_RE = re.compile(r'\b\d{11}\b')


class Command(BaseCommand):
    help = ('Усыновляет HAWB-сирот в их партию по СВХ-сообщениям '
            '(auto-create Cargo + привязка + применение СВХ).')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально создать/привязать (без флага — dry-run)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Сколько партий за прогон (0 = все)')
        parser.add_argument('--mawb', default='',
                            help='Только эта партия (svh_mawb)')

    def handle(self, *args, **opts):
        want_mawb = (opts['mawb'] or '').strip().lower()

        # Партии, у которых Cargo уже есть — их не трогаем (это забота
        # redispatch_unmatched_svh, который доматчивает к существующим).
        cargo_awbs = {a.strip().lower()
                      for a in Cargo.objects.values_list('awb_number', flat=True)
                      if a}

        qs = (AltaInboxMessage.objects
              .filter(cargo__isnull=True, msg_kind__in=SVH_KINDS)
              .order_by('prepared_at'))

        # Группируем по партии: union сирот + все её СВХ-сообщения.
        plan: dict[str, dict] = {}
        for m in qs.iterator():
            pm = m.parsed_meta or {}
            mawb = (pm.get('svh_mawb') or pm.get('svh_mawb_raw') or '').strip()
            if not mawb:
                continue
            key = mawb.lower()
            if want_mawb and key != want_mawb:
                continue
            if key in cargo_awbs:
                continue  # Cargo уже есть

            nums = set(HAWB_RE.findall(m.raw_xml or ''))
            orphans = list(HouseWaybill.objects
                           .filter(hawb_number__in=nums, mawb__isnull=True)
                           .values_list('hawb_number', flat=True)) if nums else []

            e = plan.get(key)
            if e is None:
                if opts['limit'] and len(plan) >= opts['limit'] and orphans:
                    continue  # достигли лимита новых партий
                e = plan.setdefault(key, {'mawb': mawb, 'orphans': set(),
                                          'msgs': []})
            e['msgs'].append(m)
            e['orphans'].update(orphans)

        # Оставляем только партии, где реально есть наши сироты.
        adoptable = {k: e for k, e in plan.items() if e['orphans']}

        total_orphans = sum(len(e['orphans']) for e in adoptable.values())
        self.stdout.write(f'партий к усыновлению: {len(adoptable)}; '
                          f'сирот всего: {total_orphans}')
        for e in list(adoptable.values())[:40]:
            self.stdout.write(
                f'  {e["mawb"]}: {len(e["orphans"])} сирот, '
                f'{len(e["msgs"])} СВХ-сообщ. '
                f'(напр. {sorted(e["orphans"])[:3]})')

        if not opts['apply']:
            if adoptable:
                self.stdout.write('(dry-run — добавь --apply)')
            return
        if not adoptable:
            return

        from cargo.services.alta.inbox import dispatch, _retry_on_locked
        from cargo.services.alta.outbox import _link_hawbs_to_cargo

        created = linked = dispatched = err = 0
        for e in adoptable.values():
            mawb = e['mawb']
            try:
                cargo = Cargo.objects.filter(awb_number__iexact=mawb).first()
                if not cargo:
                    cargo = _retry_on_locked(
                        Cargo.objects.create,
                        awb_number=mawb, stage='DRAFT', svh_source='')
                    created += 1
                    self.stdout.write(f'  + Cargo {mawb} (DRAFT)')
                _retry_on_locked(_link_hawbs_to_cargo,
                                 sorted(e['orphans']), cargo)
                linked += len(e['orphans'])
                # Передиспатчим все СВХ-сообщения партии → применят лицензию.
                for m in e['msgs']:
                    _retry_on_locked(dispatch, m)
                    dispatched += 1
            except Exception as ex:  # noqa: BLE001
                err += 1
                self.stderr.write(f'  {mawb}: {ex}')

        self.stdout.write(self.style.SUCCESS(
            f'создано партий {created}, привязано сирот {linked}, '
            f'передиспатчено сообщений {dispatched}, ошибок {err}'))
