from django.apps import AppConfig


class CargoConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cargo'
    verbose_name = 'Грузоперевозки'

    def ready(self):
        # Регистрируем сигналы для авто-запуска воркфлоу при создании сущностей
        import threading
        from django.db.models.signals import post_save
        from django.dispatch import receiver

        from .models import Cargo, HouseWaybill
        from . import workflow_runner

        @receiver(post_save, sender=Cargo, weak=False)
        def cargo_created(sender, instance, created, **kwargs):
            if created:
                workflow_runner.start_for_entity(instance, 'cargo')

        @receiver(post_save, sender=HouseWaybill, weak=False)
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
