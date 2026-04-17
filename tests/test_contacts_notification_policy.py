from groupware_sync.provider import NotificationCapability
from groupware_sync_contacts.adapters.carddav_adapter import CardDavContactAdapter
from groupware_sync_contacts.adapters.graph_adapter import GraphContactAdapter
from groupware_sync_contacts.adapters.jmap_adapter import JmapContactAdapter

ALL_ADAPTERS = [CardDavContactAdapter, GraphContactAdapter, JmapContactAdapter]


def test_all_contacts_adapters_declare_suppressed():
    for cls in ALL_ADAPTERS:
        p = cls.notification_policy
        for field in ("create_item", "update_item", "delete_item", "delete_container"):
            assert getattr(p, field) is NotificationCapability.SUPPRESSED, (
                f"{cls.__name__}.notification_policy.{field} must be SUPPRESSED"
            )
