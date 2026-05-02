from django.contrib.auth.models import User
from django.test import Client, TestCase


class ApiSmokeTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=False)
        self.user = User.objects.create_user('tester', password='pw-test-only')

    def test_health_anonymous(self):
        r = self.client.get('/api/v1/health/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get('status'), 'ok')

    def test_dashboard_widgets_requires_auth(self):
        anon = Client()
        r = anon.get('/api/v1/dashboard/widgets/')
        # @login_required редиректит на /login/
        self.assertIn(r.status_code, (302, 401, 403))

        self.client.force_login(self.user)
        r = self.client.get('/api/v1/dashboard/widgets/')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn('widgets', body)
        self.assertIn('pagination', body)

    def test_workflows_post_invalid_json_returns_400(self):
        self.client.force_login(self.user)
        r = self.client.post(
            '/api/v1/workflows/',
            data='this-is-not-json',
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('error', r.json())
