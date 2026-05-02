from django.apps import AppConfig


class CargoConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cargo'
    verbose_name = 'Грузоперевозки'

    def ready(self):
        # Регистрируем сигналы для авто-запуска воркфлоу при создании сущностей
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
