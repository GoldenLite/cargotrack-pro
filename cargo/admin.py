"""
Административная панель CargoTrack Pro
"""
from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from .models import (
    Cargo, StatusHistory, Warehouse, Flight, Label,
    DocumentType, CargoCategoryDocRule, CargoTypeDocTemplate, HAWBChecklistItem,
    UserProfile, ProcessingNorm, WorkloadRebalanceLog, WorkScheduleException,
    OrganizationSettings,
)

admin.site.site_header = 'CargoTrack Pro — Управление грузами'
admin.site.site_title = 'CargoTrack Pro'
admin.site.index_title = 'Панель управления'


class StatusHistoryInline(admin.TabularInline):
    model = StatusHistory
    extra = 0
    readonly_fields = ('old_status', 'new_status', 'changed_by', 'comment', 'changed_at')
    can_delete = False


@admin.register(Cargo)
class CargoAdmin(admin.ModelAdmin):
    list_display = (
        'awb_number', 'colored_stage',
        'flight_number', 'flight_date', 'departure_iata', 'arrival_iata',
        'weight', 'pieces_declared', 'warehouse_license', 'days_in_wh',
    )
    list_filter = ('stage', 'shp_type', 'is_transit', 'is_self_clearance',
                   'flight_date', 'warehouse')
    search_fields = ('awb_number', 'customs_declaration_number', 'description', 'description_ru',
                     'warehouse_license')
    readonly_fields = ('created_at', 'updated_at', 'last_status_change', 'days_in_warehouse_display',
                       'created_by')
    inlines = [StatusHistoryInline]
    list_per_page = 50
    date_hierarchy = 'flight_date'
    save_on_top = True

    fieldsets = (
        ('Идентификация', {
            'fields': ('awb_number', 'description', 'description_ru', 'shp_type', 'cpc_code')
        }),
        ('Этап', {
            'fields': ('stage', 'is_draft', 'stage_changed_at', 'last_status_change')
        }),
        ('Рейс', {
            'fields': ('flight_number', 'departure_date', 'flight_date',
                       'departure_iata', 'arrival_iata',
                       'movement_number', 'transportation_mode')
        }),
        ('Параметры груза', {
            'fields': ('weight', 'pieces_declared')
        }),
        ('Стоимость', {
            'fields': ('invoice_currency', 'invoice_value', 'customs_value_rub', 'duty_amount'),
            'classes': ('collapse',),
        }),
        ('Склад СВХ', {
            'fields': ('warehouse', 'warehouse_name', 'warehouse_license', 'bond_location',
                       'scan_into_bond', 'scan_out_of_bond', 'days_in_warehouse_display')
        }),
        ('Таможня', {
            'fields': ('customs_declaration_number', 'entry_date', 'release_date'),
            'classes': ('collapse',),
        }),
        ('Транзит / Сценарий ТО / RTO', {
            'fields': ('is_transit', 'bonded_dest', 'bonded_transit',
                       'is_self_clearance', 'rto_reason'),
            'classes': ('collapse',),
        }),
        ('UDF / Дополнительно', {
            'fields': ('payer_account', 'shipper_account'),
            'classes': ('collapse',),
        }),
        ('Системная информация', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    actions = ['action_advance_stage']

    # ── Отображение ──

    @admin.display(description='Этап')
    def colored_stage(self, obj):
        colors = {
            'DRAFT':      '#6c757d',
            'FORMED':     '#0dcaf0',
            'DISPATCHED': '#0d6efd',
            'ARRIVED':    '#fd7e14',
            'CUSTOMS':    '#ffc107',
            'RELEASED':   '#198754',
        }
        color = colors.get(obj.stage, '#6c757d')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px">{}</span>',
            color, obj.stage
        )

    @admin.display(description='Дней на складе')
    def days_in_wh(self, obj):
        d = obj.days_in_warehouse
        if d is None:
            return '—'
        if d > 7:
            return format_html('<span style="color:red;font-weight:bold">{} дн.</span>', d)
        return f'{d} дн.'

    @admin.display(description='Дней на складе (поле)')
    def days_in_warehouse_display(self, obj):
        d = obj.days_in_warehouse
        return f'{d} дней' if d is not None else 'Не на складе'

    # ── Массовые действия ──

    @admin.action(description='Перевести на следующий этап')
    def action_advance_stage(self, request, queryset):
        moved = 0
        for cargo in queryset:
            if cargo.can_advance_stage:
                cargo.advance_stage(user=request.user)
                moved += 1
        self.message_user(request, f'Этап продвинут у {moved} из {queryset.count()} партий.')

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ('name', 'license_number', 'city', 'iata_code', 'is_active')
    list_filter = ('is_active', 'city')
    search_fields = ('name', 'license_number', 'city')


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = ('flight_number', 'flight_date', 'airline', 'departure_iata', 'arrival_iata', 'status')
    list_filter = ('flight_date', 'airline')
    search_fields = ('flight_number',)
    date_hierarchy = 'flight_date'


@admin.register(StatusHistory)
class StatusHistoryAdmin(admin.ModelAdmin):
    list_display = ('cargo', 'old_status', 'new_status', 'changed_by', 'changed_at')
    list_filter = ('new_status', 'changed_at')
    search_fields = ('cargo__awb_number',)
    readonly_fields = ('cargo', 'old_status', 'new_status', 'changed_by', 'comment', 'changed_at')


# ─────────────────────────── ЧЕКЛИСТ ДОКУМЕНТОВ ───────────────────────────

@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display  = ('name', 'category_display', 'rules_count', 'is_active')
    list_filter   = ('category', 'is_active')
    search_fields = ('name', 'description')
    list_editable = ('is_active',)

    fieldsets = (
        (None, {
            'fields': ('name', 'category', 'description', 'is_active'),
        }),
    )

    @admin.display(description='Категория')
    def category_display(self, obj):
        colors = {
            'transport':   '#2563eb',
            'commercial':  '#059669',
            'customs':     '#7c3aed',
            'permit':      '#dc2626',
            'sanitary':    '#d97706',
            'certificate': '#0891b2',
            'other':       '#6b7280',
        }
        color = colors.get(obj.category, '#6b7280')
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            color, obj.get_category_display()
        )

    @admin.display(description='Обязателен для категорий')
    def rules_count(self, obj):
        cats = list(obj.category_rules.values_list('cargo_type', flat=True))
        if not cats:
            return format_html('<span style="color:#9ca3af">—</span>')
        return ', '.join(cats)


class CargoCategoryDocRuleInline(admin.TabularInline):
    """Список обязательных документов для шаблона категории груза"""
    model               = CargoCategoryDocRule
    extra               = 3
    fields              = ('document_type', 'doc_category_display')
    readonly_fields     = ('doc_category_display',)
    verbose_name        = 'Обязательный документ'
    verbose_name_plural = 'Обязательные документы'

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name == 'document_type':
            # Убираем лишние кнопки (карандаш / плюс / глаз) рядом с виджетом
            field.widget.can_add_related    = False
            field.widget.can_change_related = False
            field.widget.can_view_related   = False
            field.widget.can_delete_related = False
            # Показываем только активные типы документов
            field.queryset = field.queryset.filter(is_active=True).order_by('category', 'name')
        return field

    @admin.display(description='Категория документа')
    def doc_category_display(self, obj):
        if obj.pk:
            return obj.document_type.get_category_display()
        return '—'


@admin.register(CargoTypeDocTemplate)
class CargoTypeDocTemplateAdmin(admin.ModelAdmin):
    """
    Шаблоны документов по категориям груза.
    Откройте нужную категорию — увидите список обязательных документов,
    добавьте или удалите нужные строки.
    """
    list_display = ('cargo_type_label', 'docs_count')
    inlines      = [CargoCategoryDocRuleInline]

    def has_add_permission(self, request):
        # Запрещаем создавать новые шаблоны вручную — они создаются миграцией
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Категория груза')
    def cargo_type_label(self, obj):
        labels = {
            'B2C': ('B2C — Бизнес для потребителя', '#0284c7'),
            'B2B': ('B2B — Бизнес для бизнеса',     '#059669'),
            'C2C': ('C2C — Частное лицо',            '#7c3aed'),
            'DOC': ('Документация',                  '#d97706'),
        }
        text, color = labels.get(obj.cargo_type, (obj.cargo_type, '#6b7280'))
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>', color, text
        )

    @admin.display(description='Документов в шаблоне')
    def docs_count(self, obj):
        n = obj.rules.count()
        return f'{n} документ{"" if n == 1 else "а" if 2 <= n <= 4 else "ов"}'

    def save_formset(self, request, form, formset, change):
        """Автоматически проставляем cargo_type из шаблона при добавлении правила"""
        instances = formset.save(commit=False)
        for instance in instances:
            instance.cargo_type = form.instance.cargo_type
            instance.template   = form.instance
            instance.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()


@admin.register(Label)
class LabelAdmin(admin.ModelAdmin):
    list_display = ('name', 'colored_chip', 'description', 'usage_count', 'created_at', 'created_by')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'created_by')

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description='Цвет')
    def colored_chip(self, obj):
        return format_html(
            '<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            'background:{};color:white;font-size:11px;font-weight:600">{}</span>',
            obj.color, obj.name
        )

    @admin.display(description='Использований')
    def usage_count(self, obj):
        return obj.cargos.count() + obj.hawbs.count()


# ─────────────────────────── ПЛАНИРОВАНИЕ НАГРУЗКИ ────────────────────────────

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user_display', 'timezone', 'primary_role', 'is_active_op',
                    'capacity_display')
    list_filter = ('is_active_op', 'primary_role', 'timezone')
    search_fields = ('user__username', 'user__first_name', 'user__last_name')
    raw_id_fields = ('user',)
    fieldsets = (
        (None, {
            'fields': ('user', 'is_active_op', 'primary_role', 'timezone'),
        }),
        ('График работы', {
            'fields': ('work_schedule', 'daily_capacity_minutes'),
            'description': 'JSON-формат: {"mon":[["09:00","21:00"]], "tue":[...], ..., "sat":[], "sun":[]}. '
                           'Пустой массив — выходной.',
        }),
        ('Прочее', {
            'fields': ('notes',),
        }),
    )

    @admin.display(description='Сотрудник', ordering='user__last_name')
    def user_display(self, obj):
        return obj.user.get_full_name() or obj.user.username

    @admin.display(description='Лимит/день')
    def capacity_display(self, obj):
        if obj.daily_capacity_minutes:
            return f'{obj.daily_capacity_minutes} мин (override)'
        # Сумма минут по всем дням графика
        try:
            total = sum(
                (int(p[1].split(':')[0]) * 60 + int(p[1].split(':')[1])) -
                (int(p[0].split(':')[0]) * 60 + int(p[0].split(':')[1]))
                for ivs in (obj.work_schedule or {}).values()
                for p in (ivs or [])
            )
            return f'{total} мин/нед'
        except Exception:
            return '—'


@admin.register(ProcessingNorm)
class ProcessingNormAdmin(admin.ModelAdmin):
    list_display  = ('shipment_type', 'cargo_type', 'minutes', 'is_active', 'updated_at')
    list_filter   = ('shipment_type', 'cargo_type', 'is_active')
    list_editable = ('minutes', 'is_active')
    ordering      = ('shipment_type', 'cargo_type')


@admin.register(OrganizationSettings)
class OrganizationSettingsAdmin(admin.ModelAdmin):
    list_display = ('name', 'inn', 'updated_at')

    def has_add_permission(self, request):
        # Singleton — только одна запись
        return not OrganizationSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WorkScheduleException)
class WorkScheduleExceptionAdmin(admin.ModelAdmin):
    list_display  = ('user', 'kind', 'date_from', 'date_to', 'note', 'created_at')
    list_filter   = ('kind', 'date_from')
    search_fields = ('user__username', 'user__last_name', 'note')
    raw_id_fields = ('user',)
    date_hierarchy = 'date_from'


@admin.register(WorkloadRebalanceLog)
class WorkloadRebalanceLogAdmin(admin.ModelAdmin):
    list_display  = ('hawb', 'from_user', 'to_user', 'reason', 'target_date',
                     'created_by', 'created_at')
    list_filter   = ('reason', 'target_date', 'created_at')
    search_fields = ('hawb__hawb_number', 'from_user__username', 'to_user__username')
    readonly_fields = ('hawb', 'from_user', 'to_user', 'reason', 'target_date',
                       'created_by', 'created_at')
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False
