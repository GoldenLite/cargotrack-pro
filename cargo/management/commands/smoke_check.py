"""
Программный smoke-test всего CargoTrack Pro через Django test Client.
Покрывает все страницы, все API, основные CRUD-операции, edge cases.

Запуск:
    python manage.py smoke_check                # SMOKE-данные удаляются после
    python manage.py smoke_check --keep         # оставить SMOKE-данные в БД
    python manage.py smoke_check --no-create    # только GET-проверки
"""
import argparse
import json
import traceback
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.test import Client
from django.utils import timezone

from cargo.models import (
    Cargo, HouseWaybill, Label, DashboardWidget,
    Workflow, WorkflowStep, WorkflowInstance, SLAPolicy,
)


PASS = 'PASS'
FAIL = 'FAIL'


class Command(BaseCommand):
    help = 'Программный smoke-test всех страниц и API'

    def add_arguments(self, parser):
        parser.add_argument('--keep', action='store_true',
                            help='Оставить SMOKE-данные в БД (по умолчанию они удаляются)')
        parser.add_argument('--cleanup', action='store_true',
                            help=argparse.SUPPRESS)  # совместимость со старым флагом
        parser.add_argument('--no-create', action='store_true',
                            help='Только GET-проверки, без создания данных')

    def handle(self, *args, **opts):
        self.results = []
        self.created = {}
        self.client = Client(enforce_csrf_checks=False)
        user = User.objects.filter(is_superuser=True).first() or User.objects.first()
        if not user:
            self.stderr.write('Нет пользователей в БД')
            return
        self.client.force_login(user)
        self.stdout.write(f'Login as: {user.username} (superuser={user.is_superuser})\n')

        self.cleanup_smoke_data()

        self.section_auth()
        self.section_pages()
        self.section_api_get()
        if not opts['no_create']:
            self.section_cargo()
            self.section_hawb()
            self.section_labels()
            self.section_widgets()
            self.section_workflows()
            self.section_sla()
        self.section_cql()
        self.section_edge_cases()

        if opts['keep']:
            self.stdout.write('SMOKE-данные оставлены в БД (--keep).')
        else:
            self.cleanup_smoke_data()
            self.stdout.write('SMOKE-данные удалены. Запусти с --keep, если нужно оставить их для визуальной проверки.')

        self.report()

    # ── helpers ────────────────────────────────────────────────────────────
    def record(self, section, name, status, detail=''):
        self.results.append((section, name, status, str(detail)[:600]))
        marker = '+' if status == PASS else 'X'
        msg = f'  [{marker}] {section} :: {name}'
        if status == FAIL:
            msg += f'  -- {detail}'
        self.stdout.write(msg)

    def expect(self, section, name, cond, detail=''):
        self.record(section, name, PASS if cond else FAIL, detail if not cond else '')

    def http(self, section, name, response, allowed=(200,)):
        ok = response.status_code in allowed
        detail = f'HTTP {response.status_code}'
        if not ok:
            body = response.content[:300].decode('utf-8', errors='replace')
            detail += f' (ожидали {allowed}). Body: {body}'
        self.record(section, name, PASS if ok else FAIL, detail)
        return ok

    def cleanup_smoke_data(self):
        Cargo.objects.filter(awb_number__startswith='999-99').delete()
        HouseWaybill.objects.filter(hawb_number__startswith='SMOKE-').delete()
        Label.objects.filter(name__startswith='SMOKE-').delete()
        Workflow.objects.filter(name__startswith='SMOKE-').delete()
        SLAPolicy.objects.filter(name__startswith='SMOKE-').delete()
        DashboardWidget.objects.filter(title__startswith='SMOKE-').delete()

    # ── sections ───────────────────────────────────────────────────────────
    def section_auth(self):
        self.stdout.write('\n--- 1. Public + auth ---')
        anon = Client()
        self.http('1.auth', 'GET /api/v1/health/ (anon)', anon.get('/api/v1/health/'))
        self.http('1.auth', 'GET / (anon -> 302)', anon.get('/'), (302,))
        self.http('1.auth', 'GET /login/', self.client.get('/login/'))

    def section_pages(self):
        self.stdout.write('\n--- 2. HTML страницы ---')
        pages = [
            ('GET /',                  '/'),
            ('GET /list/',             '/list/'),
            ('GET /list/new/',         '/list/new/'),
            ('GET /hawbs/',            '/hawbs/'),
            ('GET /hawbs/new/',        '/hawbs/new/'),
            ('GET /goods/',            '/goods/'),
            ('GET /workflows/',        '/workflows/'),
            ('GET /sla/',              '/sla/'),
            ('GET /labels/',           '/labels/'),
            ('GET /faq/',              '/faq/'),
            ('GET /export/',           '/export/'),
            ('GET /drill/',            '/drill/'),
            ('GET /admin/',            '/admin/'),
        ]
        for label, url in pages:
            r = self.client.get(url)
            self.http('2.pages', label, r, (200, 302))

    def section_api_get(self):
        self.stdout.write('\n--- 3. API GET ---')
        urls = [
            '/api/v1/cargo/',
            '/api/v1/hawbs/',
            '/api/v1/dashboard/widgets/',
            '/api/v1/dashboard/cql/fields/?entity_type=cargo',
            '/api/v1/dashboard/cql/fields/?entity_type=hawb',
            '/api/v1/dashboard/pivot/fields/?entity_type=cargo',
            '/api/v1/dashboard/pivot/fields/?entity_type=hawb',
            '/api/v1/widgets/entities/',
            '/api/v1/widgets/fields/?entity_type=cargo',
            '/api/v1/workflows/',
            '/api/v1/sla-policies/',
            '/api/v1/labels/',
            '/api/v1/labels/?with_usage=1',
        ]
        for url in urls:
            r = self.client.get(url)
            self.http('3.api_get', f'GET {url}', r)

    def section_cargo(self):
        self.stdout.write('\n--- 4. Cargo ---')
        try:
            cargo = Cargo.objects.create(
                awb_number='999-99000001', stage='DRAFT',
                weight=Decimal('123.45'), flight_number='SU100',
                flight_date=date.today(),
            )
            self.created['cargo'] = cargo
            self.record('4.cargo', 'create cargo 999-99000001', PASS)

            self.http('4.cargo', f'GET /cargo/{cargo.awb_number}/',
                      self.client.get(f'/cargo/{cargo.awb_number}/'))
            self.http('4.cargo', 'GET /api/v1/cargo/<awb>/',
                      self.client.get(f'/api/v1/cargo/{cargo.awb_number}/'))
            # update/assign/stage/hawb_create — POST-only inline-формы; GET = 302
            self.http('4.cargo', 'GET update (POST-only)', self.client.get(f'/cargo/{cargo.awb_number}/update/'), (200, 302))
            self.http('4.cargo', 'GET assign (POST-only)', self.client.get(f'/cargo/{cargo.awb_number}/assign/'), (200, 302))
            self.http('4.cargo', 'GET stage (POST-only)',  self.client.get(f'/cargo/{cargo.awb_number}/stage/'), (200, 302))
            self.http('4.cargo', f'GET hawb list',   self.client.get(f'/cargo/{cargo.awb_number}/hawb/'))
            self.http('4.cargo', 'GET hawb create (POST-only)', self.client.get(f'/cargo/{cargo.awb_number}/hawb/create/'), (200, 302))

            cargo.set_stage('FORMED')
            cargo.refresh_from_db()
            self.expect('4.cargo', 'set_stage(FORMED) применился',
                        cargo.stage == 'FORMED' and cargo.stage_changed_at is not None)
        except Exception:
            self.record('4.cargo', 'unexpected exception', FAIL, traceback.format_exc())

    def section_hawb(self):
        self.stdout.write('\n--- 5. HAWB ---')
        try:
            cargo = self.created.get('cargo')
            hawb = HouseWaybill.objects.create(
                mawb=cargo, hawb_number='SMOKE-001',
                logistics_status='AT_ORIGIN_WH',
                consignee_name='Тестовый получатель', weight=Decimal('10'),
            )
            self.created['hawb'] = hawb
            self.record('5.hawb', 'create HAWB SMOKE-001 (linked)', PASS)

            self.http('5.hawb', f'GET /hawb/{hawb.id}/',        self.client.get(f'/hawb/{hawb.id}/'))
            # update — POST-only inline-форма; GET = 302
            self.http('5.hawb', f'GET /hawb/{hawb.id}/update/ (POST-only)',
                      self.client.get(f'/hawb/{hawb.id}/update/'), (200, 302))

            standalone = HouseWaybill.objects.create(
                hawb_number='SMOKE-002', logistics_status='AT_ORIGIN_WH',
                consignee_name='Standalone',
            )
            self.expect('5.hawb', 'standalone HAWB has no mawb', standalone.mawb_id is None)

            hawb.change_logistics_status('IN_FLIGHT')
            hawb.refresh_from_db()
            self.expect('5.hawb', 'change_logistics_status', hawb.logistics_status == 'IN_FLIGHT')
        except Exception:
            self.record('5.hawb', 'unexpected exception', FAIL, traceback.format_exc())

    def section_labels(self):
        self.stdout.write('\n--- 6. Labels ---')
        try:
            r = self.client.post('/api/v1/labels/',
                data=json.dumps({'name': 'SMOKE-urgent', 'color': '#ff0000'}),
                content_type='application/json')
            if not self.http('6.labels', 'POST create', r, (200, 201)):
                return
            label = Label.objects.get(name='SMOKE-urgent')
            self.created['label'] = label

            r = self.client.put(f'/api/v1/labels/{label.id}/',
                data=json.dumps({'name': 'SMOKE-urgent', 'color': '#00ff00'}),
                content_type='application/json')
            self.http('6.labels', 'PUT update color', r)

            cargo = self.created.get('cargo')
            hawb = self.created.get('hawb')
            if cargo:
                r = self.client.put(f'/api/v1/cargo/{cargo.awb_number}/labels/',
                    data=json.dumps({'label_ids': [label.id]}),
                    content_type='application/json')
                self.http('6.labels', 'PUT cargo labels', r)
            if hawb:
                r = self.client.put(f'/api/v1/hawbs/{hawb.id}/labels/',
                    data=json.dumps({'label_ids': [label.id]}),
                    content_type='application/json')
                self.http('6.labels', 'PUT hawb labels', r)

            r = self.client.get('/api/v1/labels/?with_usage=1')
            body = r.json() if r.status_code == 200 else {}
            smoke_l = next((l for l in body.get('labels', []) if l.get('name') == 'SMOKE-urgent'), None)
            self.expect('6.labels', 'with_usage показывает usage_count >= 2',
                        smoke_l is not None and smoke_l.get('usage_count', 0) >= 2,
                        f'label data: {smoke_l}')

            r = self.client.get('/api/v1/labels/?page=1&page_size=2')
            body = r.json() if r.status_code == 200 else {}
            self.expect('6.labels', 'pagination meta',
                        'pagination' in body and body['pagination']['page_size'] == 2)
        except Exception:
            self.record('6.labels', 'unexpected exception', FAIL, traceback.format_exc())

    def section_widgets(self):
        self.stdout.write('\n--- 7. Widgets ---')
        try:
            r = self.client.post('/api/v1/dashboard/widgets/',
                data=json.dumps({
                    'widget_type': 'stat', 'entity_type': 'cargo',
                    'title': 'SMOKE-stat', 'filter_query': 'stage = FORMED',
                    'config': {'metric': 'count'},
                    'pos_x': 0, 'pos_y': 0, 'width': 3, 'height': 2,
                }),
                content_type='application/json')
            if not self.http('7.widgets', 'POST create', r, (200, 201)):
                return
            widget = DashboardWidget.objects.get(title='SMOKE-stat')
            self.created['widget'] = widget

            self.http('7.widgets', 'GET detail',
                      self.client.get(f'/api/v1/dashboard/widgets/{widget.id}/'))
            self.http('7.widgets', 'GET data',
                      self.client.get(f'/api/v1/dashboard/widgets/{widget.id}/data/'))

            r = self.client.put(f'/api/v1/dashboard/widgets/{widget.id}/',
                data=json.dumps({'title': 'SMOKE-stat-upd'}),
                content_type='application/json')
            self.http('7.widgets', 'PUT update', r)

            r = self.client.get('/api/v1/dashboard/widgets/?page=1&page_size=5')
            body = r.json() if r.status_code == 200 else {}
            self.expect('7.widgets', 'pagination key', 'pagination' in body)

            r = self.client.get('/api/v1/dashboard/pivot/fields/?entity_type=cargo')
            body = r.json() if r.status_code == 200 else {}
            self.expect('7.widgets', 'pivot fields has groupable',
                        'groupable' in body and 'aggregatable' in body)

            r = self.client.post('/api/v1/dashboard/layout/',
                data=json.dumps([{'id': widget.id, 'pos_x': 1, 'pos_y': 2,
                                  'width': 4, 'height': 3}]),
                content_type='application/json')
            self.http('7.widgets', 'POST layout', r)
        except Exception:
            self.record('7.widgets', 'unexpected exception', FAIL, traceback.format_exc())

    def section_workflows(self):
        self.stdout.write('\n--- 8. Workflows ---')
        try:
            r = self.client.post('/api/v1/workflows/',
                data=json.dumps({
                    'name': 'SMOKE-WF', 'description': 'smoke',
                    'entity_type': 'cargo', 'auto_start': False, 'is_active': True,
                }),
                content_type='application/json')
            if not self.http('8.wf', 'POST create', r, (200, 201)):
                return
            wf = Workflow.objects.get(name='SMOKE-WF')
            self.created['wf'] = wf

            self.http('8.wf', 'GET detail', self.client.get(f'/api/v1/workflows/{wf.id}/'))
            self.http('8.wf', f'GET /workflows/{wf.id}/', self.client.get(f'/workflows/{wf.id}/'))

            r = self.client.post(f'/api/v1/workflows/{wf.id}/steps/',
                data=json.dumps({'name': 'SMOKE-step1', 'step_type': 'stage',
                                 'stage': 'DRAFT', 'pos_x': 100, 'pos_y': 100,
                                 'color': '#3b5680', 'order': 0}),
                content_type='application/json')
            self.http('8.wf', 'POST step1', r, (200, 201))
            step1 = WorkflowStep.objects.get(workflow=wf, name='SMOKE-step1')

            r = self.client.post(f'/api/v1/workflows/{wf.id}/steps/',
                data=json.dumps({'name': 'SMOKE-step2', 'step_type': 'stage',
                                 'stage': 'FORMED', 'pos_x': 300, 'pos_y': 100,
                                 'color': '#3b5680', 'order': 1}),
                content_type='application/json')
            self.http('8.wf', 'POST step2', r, (200, 201))
            step2 = WorkflowStep.objects.get(workflow=wf, name='SMOKE-step2')

            r = self.client.post(f'/api/v1/workflows/{wf.id}/transitions/',
                data=json.dumps({'name': 'draft->formed',
                                 'from_step': step1.id, 'to_step': step2.id}),
                content_type='application/json')
            self.http('8.wf', 'POST transition', r, (200, 201))

            r = self.client.post(f'/api/v1/workflows/{wf.id}/automations/',
                data=json.dumps({'name': 'SMOKE-auto', 'action_type': 'log',
                                 'config': {'message': 'test'}, 'is_active': True}),
                content_type='application/json')
            self.http('8.wf', 'POST automation', r, (200, 201))

            r = self.client.post(f'/api/v1/workflows/{wf.id}/layout/',
                data=json.dumps([
                    {'id': step1.id, 'pos_x': 200, 'pos_y': 200},
                    {'id': step2.id, 'pos_x': 500, 'pos_y': 200},
                ]),
                content_type='application/json')
            self.http('8.wf', 'POST layout', r)

            cargo = self.created.get('cargo')
            if cargo:
                # Cargo.pk это `id` (BigAutoField), не awb_number
                r = self.client.post(f'/api/v1/workflows/{wf.id}/start/',
                    data=json.dumps({'entity_type': 'cargo',
                                     'entity_id': cargo.id}),
                    content_type='application/json')
                self.http('8.wf', 'POST manual start', r, (200, 201))

                r = self.client.get(f'/api/v1/cargo/{cargo.awb_number}/workflow-instances/')
                self.http('8.wf', 'GET instances for cargo', r)

            inst = WorkflowInstance.objects.filter(workflow=wf, status='active').first()
            if inst:
                r = self.client.post(f'/api/v1/workflow-instances/{inst.id}/cancel/',
                    data=json.dumps({}), content_type='application/json')
                self.http('8.wf', 'POST instance cancel', r)

            r = self.client.get('/api/v1/workflows/?page=1&page_size=5')
            body = r.json() if r.status_code == 200 else {}
            self.expect('8.wf', 'pagination key', 'pagination' in body)

            r = self.client.post('/api/v1/workflows/', data='garbage',
                                  content_type='application/json')
            self.expect('8.wf', 'bad JSON -> 400', r.status_code == 400, f'HTTP {r.status_code}')
        except Exception:
            self.record('8.wf', 'unexpected exception', FAIL, traceback.format_exc())

    def section_sla(self):
        self.stdout.write('\n--- 9. SLA ---')
        try:
            r = self.client.post('/api/v1/sla-policies/',
                data=json.dumps({
                    'name': 'SMOKE-policy', 'entity_type': 'cargo',
                    'status_field': 'stage', 'status_value': 'ARRIVED',
                    'hours': '2.0', 'warning_threshold_pct': 75, 'is_active': True,
                }),
                content_type='application/json')
            if not self.http('9.sla', 'POST create', r, (200, 201)):
                return
            policy = SLAPolicy.objects.get(name='SMOKE-policy')

            # api_sla_policy поддерживает только PUT/DELETE (нет retrieve по id);
            # для деталей используется GET list. Это не баг, а архитектура.

            r = self.client.put(f'/api/v1/sla-policies/{policy.id}/',
                data=json.dumps({'name': 'SMOKE-policy', 'hours': '3.0',
                                 'entity_type': 'cargo', 'status_field': 'stage',
                                 'status_value': 'ARRIVED', 'warning_threshold_pct': 80}),
                content_type='application/json')
            self.http('9.sla', 'PUT update', r)

            r = self.client.get('/api/v1/sla-policies/?page=1&page_size=5')
            body = r.json() if r.status_code == 200 else {}
            self.expect('9.sla', 'pagination key', 'pagination' in body)

            r = self.client.get('/api/v1/sla-policies/?entity_type=hawb')
            self.expect('9.sla', 'filter by entity_type', r.status_code == 200)
        except Exception:
            self.record('9.sla', 'unexpected exception', FAIL, traceback.format_exc())

    def section_cql(self):
        self.stdout.write('\n--- 10. CQL ---')
        cql_tests = [
            'stage = FORMED',
            'stage IN (DRAFT, ARRIVED)',
            'weight > 100 AND weight < 500',
            'flight_date BETWEEN -7d AND today()',
            'labels IS NULL',
        ]
        for q in cql_tests:
            r = self.client.post('/api/v1/dashboard/cql/validate/',
                data=json.dumps({'query': q, 'entity_type': 'cargo'}),
                content_type='application/json')
            body = r.json() if r.status_code == 200 else {}
            self.expect('10.cql', f'validate `{q}`',
                        r.status_code == 200 and body.get('valid') is True,
                        f'HTTP {r.status_code}, body: {body}')

        r = self.client.post('/api/v1/dashboard/cql/parse-tree/',
            data=json.dumps({'query': 'stage = ARRIVED OR weight > 100',
                             'entity_type': 'cargo'}),
            content_type='application/json')
        body = r.json() if r.status_code == 200 else {}
        self.expect('10.cql', 'parse-tree -> AST',
                    r.status_code == 200 and body.get('ok') and 'tree' in body,
                    f'HTTP {r.status_code}')

        r = self.client.post('/api/v1/dashboard/cql/validate/',
            data=json.dumps({'query': 'stage = ', 'entity_type': 'cargo'}),
            content_type='application/json')
        body = r.json() if r.status_code == 200 else {}
        self.expect('10.cql', 'invalid CQL valid=False + error',
                    r.status_code == 200 and body.get('valid') is False and 'error' in body,
                    f'body: {body}')

        r = self.client.post('/api/v1/dashboard/cql/validate/',
            data='not-json', content_type='application/json')
        self.expect('10.cql', 'bad JSON -> 400 (B.1)',
                    r.status_code == 400, f'HTTP {r.status_code}')

    def section_edge_cases(self):
        self.stdout.write('\n--- 11. Edge cases ---')
        r = self.client.get('/cargo/000-99999999/')
        self.expect('11.edge', '404 на несуществующий cargo',
                    r.status_code == 404, f'HTTP {r.status_code}')

        r = self.client.get('/hawb/9999999/')
        self.expect('11.edge', '404 на несуществующий HAWB',
                    r.status_code == 404, f'HTTP {r.status_code}')

        r = self.client.get('/api/v1/dashboard/widgets/9999999/')
        self.expect('11.edge', '404/403 на несуществующий widget',
                    r.status_code in (404, 403), f'HTTP {r.status_code}')

        r = self.client.get('/api/v1/workflows/9999999/')
        self.expect('11.edge', '404 на несуществующий workflow',
                    r.status_code == 404, f'HTTP {r.status_code}')

        r = self.client.get('/api/v1/dashboard/pivot/fields/?entity_type=invalid')
        self.expect('11.edge', 'pivot fields с невалидным entity_type',
                    r.status_code in (400, 404), f'HTTP {r.status_code}')

        r = self.client.get('/api/v1/labels/?page=999&page_size=10')
        ok = r.status_code == 200 and r.json().get('labels') == []
        self.expect('11.edge', 'pagination за пределами -> пустой',
                    ok, f'HTTP {r.status_code}')

        r = self.client.get('/api/v1/labels/?page=abc&page_size=xyz')
        self.expect('11.edge', 'pagination с буквами -> 200 (fallback)',
                    r.status_code == 200, f'HTTP {r.status_code}')

    def report(self):
        self.stdout.write('\n' + '=' * 70)
        total = len(self.results)
        passed = sum(1 for r in self.results if r[2] == PASS)
        failed = total - passed
        self.stdout.write(f'TOTAL: {total}  PASS: {passed}  FAIL: {failed}')
        self.stdout.write('=' * 70)
        if failed:
            self.stdout.write('\n--- FAILED ---')
            for s, n, st, d in self.results:
                if st == FAIL:
                    self.stdout.write(f'  [{s}] {n}\n    {d}\n')

        md = ['# Smoke-test results (программный, через Django Client)', '']
        md.append(f'**Дата:** {timezone.now().strftime("%Y-%m-%d %H:%M")}')
        md.append(f'**TOTAL:** {total}  **PASS:** {passed}  **FAIL:** {failed}')
        md.append('')
        md.append('| Раздел | Тест | Статус | Детали |')
        md.append('|---|---|---|---|')
        for s, n, st, d in self.results:
            md.append(f'| {s} | {n} | {st} | {d.replace("|", "\\|")[:200]} |')

        import os
        os.makedirs('docs', exist_ok=True)
        with open('docs/smoketest_results.md', 'w', encoding='utf-8') as f:
            f.write('\n'.join(md))
        self.stdout.write('\nReport saved -> docs/smoketest_results.md')
