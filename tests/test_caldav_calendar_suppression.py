from groupware_sync.provider import NotificationCapability
from groupware_sync_calendar.adapters.caldav_adapter import CalDavCalendarAdapter


def test_caldav_policy_is_unsupported_on_all_ops():
    p = CalDavCalendarAdapter.notification_policy
    assert p.create_item is NotificationCapability.UNSUPPORTED
    assert p.update_item is NotificationCapability.UNSUPPORTED
    assert p.delete_item is NotificationCapability.UNSUPPORTED
    assert p.delete_container is NotificationCapability.UNSUPPORTED
