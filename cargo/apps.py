from django.apps import AppConfig


class CargoConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cargo'
    verbose_name = 'Грузоперевозки'

    def ready(self):
        # Регистрируем сигналы для авто-запуска воркфлоу при создании сущностей
        import threading
        from django.db.backends.signals import connection_created
        from django.db.models.signals import post_save
        from django.dispatch import receiver

        from .models import Cargo, HouseWaybill
        from . import workflow_runner

        @receiver(connection_created)
        def sqlite_pragmas(sender, connection, **kwargs):
            """Включаем WAL и normal sync для SQLite — без этого agent-poll
            и долгие импорты конкурируют за write-lock и роняют запросы с
            `database is locked`. WAL живёт в файле БД, но PRAGMA нужно
            повторять на каждом новом соединении (waitress открывает их пачкой).
            """
            if connection.vendor != 'sqlite':
                return
            with connection.cursor() as cur:
                cur.execute('PRAGMA journal_mode=WAL;')
                cur.execute('PRAGMA synchronous=NORMAL;')
                cur.execute('PRAGMA busy_timeout=5000;')

        @receiver(post_save, sender=Cargo, weak=False, dispatch_uid='cargo_created_workflow')
        def cargo_created(sender, instance, created, **kwargs):
            if created:
                workflow_runner.start_for_entity(instance, 'cargo')

        @receiver(post_save, sender=Cargo, weak=False, dispatch_uid='cargo_moscow_cargo_fetch')
        def cargo_moscow_cargo_fetch(sender, instance, created, **kwargs):
            """При создании Cargo с префиксом Москва-Карго → fetch внешнего ДО1.

            Только для партий с префиксами MOSCOW_CARGO_PREFIXES. Делаем в фоне
            чтобы не блокировать promote/save. Если ДО1 на сайте ещё нет —
            подберёт cron `refresh_moscow_cargo` позже.
            """
            if not created:
                return
            def _run():
                try:
                    from .services.external_warehouse.applier import (
                        is_moscow_cargo_candidate, fetch_and_apply,
                    )
                    if not is_moscow_cargo_candidate(instance):
                        return
                    fetch_and_apply(instance)
                except Exception:
                    import logging
                    logging.getLogger('cargo.external.moscow_cargo').exception(
                        'cargo_moscow_cargo_fetch failed for %s', instance.pk)
            threading.Thread(target=_run, daemon=True).start()

        @receiver(post_save, sender=Cargo, weak=False, dispatch_uid='cargo_svh_backfill')
        def cargo_svh_backfill(sender, instance, created, **kwargs):
            """При создании Cargo подхватываем висящие CMN.13029/CMN.13010.

            Сценарий: представление от таможни пришло до того как партия
            заведена в CargoTrack (через promote из Sheets или вручную).
            CMN.13029 в этот момент сохранился без cargo (match_svh не нашёл
            Cargo с этим MAWB). После создания Cargo → пере-dispatch висящих
            представлений → backfill ДО1 автоматически.
            """
            if not created:
                return
            def _run():
                try:
                    from .models import AltaInboxMessage
                    from .services.alta.inbox import dispatch
                    pending = AltaInboxMessage.objects.filter(
                        msg_kind='svh_placed',
                        cargo__isnull=True,
                        parsed_meta__svh_mawb=instance.awb_number,
                    )
                    for msg in pending:
                        dispatch(msg)
                except Exception:
                    import logging
                    logging.getLogger('cargo.alta.inbox').exception(
                        'cargo_svh_backfill failed for %s', instance.pk)
            threading.Thread(target=_run, daemon=True).start()

        @receiver(post_save, sender=HouseWaybill, weak=False, dispatch_uid='hawb_created_workflow')
        def hawb_created(sender, instance, created, **kwargs):
            if created:
                workflow_runner.start_for_entity(instance, 'hawb')

        @receiver(post_save, sender=HouseWaybill, weak=False)
        def hawb_writeback_to_sheets(sender, instance, created, update_fields, **kwargs):
            """При изменении customs_declaration_number — пишем в Sheets.

            Запускаем в фоновом потоке, чтобы не блокировать сохранение HAWB.
            Любые ошибки writeback ловятся внутри write_declaration().
            """
            if not (instance.customs_declaration_number or '').strip():
                return
            if update_fields is not None and 'customs_declaration_number' not in update_fields:
                return
            def _run():
                try:
                    from .services.sheets.writeback import write_declaration
                    write_declaration(instance)
                except Exception:
                    import logging
                    logging.getLogger('cargo.sheets.writeback').exception('writeback thread crashed')
            threading.Thread(target=_run, daemon=True).start()

        @receiver(post_save, sender=HouseWaybill, weak=False,
                  dispatch_uid='hawb_release_date_writeback')
        def hawb_release_date_writeback(sender, instance, created, update_fields, **kwargs):
            """При установке/изменении release_date — пишем в Sheets «дата выпуска».

            Фоновый поток (как customs_declaration_number writeback). Если save()
            был с update_fields — фильтруем, чтобы не дёргать Sheets на каждый save.
            """
            if not instance.release_date:
                return
            if update_fields is not None and 'release_date' not in update_fields:
                return
            def _run():
                try:
                    from .services.sheets.writeback import write_release_date_for_hawb
                    write_release_date_for_hawb(instance)
                except Exception:
                    import logging
                    logging.getLogger('cargo.sheets.writeback').exception(
                        'release_date writeback thread crashed')
            threading.Thread(target=_run, daemon=True).start()
