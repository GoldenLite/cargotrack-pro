from django.urls import path
from . import views

urlpatterns = [
    # ── REST API v1 ──
    path('api/v1/health/', views.api_health, name='api_health'),
    path('api/v1/cargo/', views.CargoListAPIView.as_view(), name='api_cargo_list'),
    path('api/v1/cargo/<str:awb_number>/', views.CargoDetailAPIView.as_view(), name='api_cargo_detail'),
    path('api/v1/hawbs/', views.HawbListAPIView.as_view(), name='api_hawb_list'),

    # ── Dashboard Widgets API ──
    path('api/v1/dashboard/widgets/',                views.api_dashboard_widgets,      name='api_dashboard_widgets'),
    path('api/v1/dashboard/widgets/<int:widget_id>/', views.api_dashboard_widget,      name='api_dashboard_widget'),
    path('api/v1/dashboard/widgets/<int:widget_id>/data/', views.api_dashboard_widget_data, name='api_dashboard_widget_data'),
    path('api/v1/dashboard/layout/',                 views.api_dashboard_layout,        name='api_dashboard_layout'),
    path('api/v1/dashboard/cql/validate/',           views.api_dashboard_cql_validate,  name='api_dashboard_cql_validate'),
    path('api/v1/dashboard/cql/fields/',             views.api_dashboard_cql_fields,    name='api_dashboard_cql_fields'),
    path('api/v1/dashboard/cql/values/',             views.api_dashboard_cql_values,    name='api_dashboard_cql_values'),
    path('api/v1/dashboard/cql/parse-tree/',         views.api_dashboard_cql_parse_tree, name='api_dashboard_cql_parse_tree'),
    path('api/v1/dashboard/pivot/fields/',           views.api_dashboard_pivot_fields,  name='api_dashboard_pivot_fields'),
    path('api/v1/dashboard/widgets/<int:widget_id>/drill/', views.api_dashboard_pivot_drill, name='api_dashboard_pivot_drill'),
    path('api/v1/widgets/entities/',                 views.api_widgets_entities,        name='api_widgets_entities'),
    path('api/v1/widgets/fields/',                   views.api_widgets_fields,          name='api_widgets_fields'),
    path('api/v1/widgets/drill/',                    views.api_widgets_drill,           name='api_widgets_drill'),
    path('drill/',                                   views.drill_view,                  name='drill_view'),

    # ── Метки (Labels) ──
    path('labels/',                                  views.labels_page,                 name='labels_page'),
    path('api/v1/labels/',                           views.api_labels,                  name='api_labels'),
    path('api/v1/labels/<int:label_id>/',            views.api_label,                   name='api_label'),
    path('api/v1/cargo/<str:awb_number>/labels/',    views.api_cargo_labels,            name='api_cargo_labels'),
    path('api/v1/hawbs/<int:hawb_id>/labels/',       views.api_hawb_labels,             name='api_hawb_labels'),

    # ── Обычные страницы ──
    path('', views.dashboard, name='dashboard'),
    path('list/', views.cargo_list, name='cargo_list'),
    path('list/new/', views.cargo_create_from_hawbs, name='cargo_create_from_hawbs'),
    path('hawbs/', views.all_hawbs, name='all_hawbs'),
    path('hawbs/new/', views.hawb_create_standalone, name='hawb_create_standalone'),
    path('hawbs/export/', views.export_hawbs_excel, name='export_hawbs_excel'),
    path('faq/', views.faq, name='faq'),
    path('cargo/<str:awb_number>/', views.cargo_detail, name='cargo_detail'),
    path('cargo/<str:awb_number>/update/', views.update_status, name='update_status'),
    path('cargo/<str:awb_number>/assign/', views.assign_user, name='assign_user'),
    path('cargo/<str:awb_number>/stage/', views.cargo_set_stage, name='cargo_set_stage'),
    path('cargo/<str:awb_number>/hawb/', views.hawb_list, name='hawb_list'),
    path('cargo/<str:awb_number>/hawb/create/', views.hawb_create, name='hawb_create'),
    path('hawb/<int:hawb_id>/', views.hawb_detail, name='hawb_detail'),
    path('hawb/<int:hawb_id>/update/', views.hawb_update, name='hawb_update'),
    path('hawb/<int:hawb_id>/delete/', views.hawb_delete, name='hawb_delete'),
    path('export/', views.export_excel, name='export_excel'),
    path('api/document-types/', views.document_types_search, name='document_types_search'),
    path('goods/', views.goods_list, name='goods_list'),
    path('goods/export/', views.goods_export_excel, name='goods_export_excel'),
    path('goods/approve/', views.goods_approve, name='goods_approve'),

    # ── Workflows (Бизнес-процессы) ──
    path('workflows/', views.workflow_list, name='workflow_list'),
    path('workflows/<int:workflow_id>/', views.workflow_editor, name='workflow_editor'),

    # ── Workflows API ──
    path('api/v1/workflows/',                                          views.api_workflows,               name='api_workflows'),
    path('api/v1/workflows/<int:workflow_id>/',                        views.api_workflow,                 name='api_workflow'),
    path('api/v1/workflows/<int:workflow_id>/steps/',                  views.api_workflow_steps,           name='api_workflow_steps'),
    path('api/v1/workflows/<int:workflow_id>/steps/<int:step_id>/',    views.api_workflow_step,            name='api_workflow_step'),
    path('api/v1/workflows/<int:workflow_id>/transitions/',            views.api_workflow_transitions,     name='api_workflow_transitions'),
    path('api/v1/workflows/<int:workflow_id>/transitions/<int:transition_id>/', views.api_workflow_transition, name='api_workflow_transition'),
    path('api/v1/workflows/<int:workflow_id>/automations/',            views.api_workflow_automations,     name='api_workflow_automations'),
    path('api/v1/workflows/<int:workflow_id>/automations/<int:automation_id>/', views.api_workflow_automation, name='api_workflow_automation'),
    path('api/v1/workflows/<int:workflow_id>/layout/',                 views.api_workflow_save_layout,     name='api_workflow_save_layout'),

    # ── Workflow Instances API ──
    path('api/v1/cargo/<str:awb_number>/workflow-instances/',             views.api_cargo_workflow_instances,   name='api_cargo_workflow_instances'),
    path('api/v1/hawbs/<int:hawb_id>/workflow-instances/',               views.api_hawb_workflow_instances,    name='api_hawb_workflow_instances'),
    path('api/v1/workflows/<int:workflow_id>/start/',                    views.api_workflow_start,             name='api_workflow_start'),
    path('api/v1/workflow-instances/<int:instance_id>/cancel/',          views.api_workflow_instance_cancel,   name='api_workflow_instance_cancel'),

    # ── Workload (планирование нагрузки) ──
    path('api/v1/workload/rebalance/preview/', views.api_workload_rebalance_preview, name='api_workload_rebalance_preview'),
    path('api/v1/workload/rebalance/apply/',   views.api_workload_rebalance_apply,   name='api_workload_rebalance_apply'),

    # ── Команда (графики и отпуска) ──
    path('team/',                                 views.team_page,           name='team_page'),
    path('api/v1/team/profiles/',                 views.api_team_profiles,   name='api_team_profiles'),
    path('api/v1/team/profiles/<int:user_id>/',   views.api_team_profile,    name='api_team_profile'),
    path('api/v1/team/exceptions/',               views.api_team_exceptions, name='api_team_exceptions'),
    path('api/v1/team/exceptions/<int:exception_id>/', views.api_team_exception, name='api_team_exception'),
    path('api/v1/team/organization/',             views.api_organization_settings, name='api_organization_settings'),

    # ── Экспорт ДО1 ──
    path('cargo/<str:awb_number>/export/do1/',      views.cargo_export_do1,      name='cargo_export_do1'),
    path('cargo/<str:awb_number>/export/manifest/', views.cargo_export_manifest, name='cargo_export_manifest'),

    # ── Экспорт XML для Альта-ГТД (скачивание) ──
    path('hawb/<int:hawb_id>/export/alta/',         views.hawb_export_alta_xml,     name='hawb_export_alta_xml'),
    path('hawb/<int:hawb_id>/export/alta/indpost/', views.hawb_export_alta_indpost, name='hawb_export_alta_indpost'),
    path('hawb/<int:hawb_id>/export/alta/invoice/', views.hawb_export_alta_invoice, name='hawb_export_alta_invoice'),
    path('cargo/<str:awb_number>/export/alta/',     views.cargo_export_alta_express, name='cargo_export_alta_express'),
    path('cargo/<str:awb_number>/export/alta/dt/',  views.cargo_export_alta_dt,      name='cargo_export_alta_dt'),

    # ── Отправка в hot-folder Альты через очередь ──
    path('hawb/<int:hawb_id>/send/alta/indpost/', views.hawb_send_alta_indpost, name='hawb_send_alta_indpost'),
    path('hawb/<int:hawb_id>/send/alta/waybill/', views.hawb_send_alta_waybill, name='hawb_send_alta_waybill'),
    path('hawb/<int:hawb_id>/send/alta/invoice/', views.hawb_send_alta_invoice, name='hawb_send_alta_invoice'),
    path('cargo/<str:awb_number>/send/alta/',     views.cargo_send_alta_express, name='cargo_send_alta_express'),
    path('cargo/<str:awb_number>/send/alta/dt/',  views.cargo_send_alta_dt,      name='cargo_send_alta_dt'),
    path('alta/queue/',                           views.alta_queue_page,         name='alta_queue_page'),
    path('alta/queue/<int:item_id>/retry/',       views.alta_queue_retry,        name='alta_queue_retry'),

    # ── API для агента Альты ──
    path('api/v1/alta/queue/',                       views.api_alta_queue_list, name='api_alta_queue_list'),
    path('api/v1/alta/queue/<int:item_id>/file/',    views.api_alta_queue_file, name='api_alta_queue_file'),
    path('api/v1/alta/queue/<int:item_id>/ack/',     views.api_alta_queue_ack,  name='api_alta_queue_ack'),
    path('api/v1/alta/queue/<int:item_id>/fail/',    views.api_alta_queue_fail, name='api_alta_queue_fail'),

    # ── Импорт из Google Sheets ──
    path('imports/sheets/',                            views.sheets_imports_page,  name='sheets_imports'),
    path('imports/sheets/sources/',                    views.sheets_sources_page,  name='sheets_sources'),
    path('imports/sheets/runs/',                       views.sheets_runs_page,     name='sheets_runs'),
    path('imports/sheets/run/<int:source_id>/',        views.sheets_run_now,       name='sheets_run_now'),
    path('imports/sheets/rows/<int:row_id>/',          views.sheets_row_detail,    name='sheets_row_detail'),
    path('imports/sheets/rows/<int:row_id>/promote/',  views.sheets_row_promote,   name='sheets_row_promote'),
    path('imports/sheets/rows/<int:row_id>/ignore/',   views.sheets_row_ignore,    name='sheets_row_ignore'),
    path('imports/sheets/rows/<int:row_id>/rematch/',  views.sheets_row_rematch,   name='sheets_row_rematch'),

    # ── SLA ──
    path('sla/', views.sla_policies_page, name='sla_policies'),
    path('api/v1/sla-policies/',                 views.api_sla_policies, name='api_sla_policies'),
    path('api/v1/sla-policies/<int:policy_id>/', views.api_sla_policy,   name='api_sla_policy'),
]
