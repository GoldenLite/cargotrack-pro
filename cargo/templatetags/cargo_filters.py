from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Получить значение из словаря по ключу в шаблоне: {{ dict|get_item:key }}"""
    if isinstance(dictionary, dict):
        return dictionary.get(key, '')
    return ''


@register.filter
def status_color(status):
    colors = {
        'RLSE': 'success', 'REJ': 'danger', 'HOLD': 'warning',
        'EXAM': 'warning', 'HLDP': 'danger', 'RTO': 'purple',
    }
    return colors.get(status, 'secondary')
