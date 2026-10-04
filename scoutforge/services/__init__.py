"""Service layer: orchestration between discovery, scoring, monitoring and CRM."""

from scoutforge.services.alert_service import (
    AlertDispatcher,
    AlertOutcome,
    AlertPayload,
    AlertService,
)
from scoutforge.services.container import Container
from scoutforge.services.crm_service import (
    InvalidTransitionError,
    LeadService,
    LeadView,
    UnknownStatusError,
    allowed_transitions,
    can_transition,
    parse_status,
    status_choices,
)
from scoutforge.services.discovery_service import DiscoveryWorker, ScanResult
from scoutforge.services.settings_service import SETTINGS, SettingsService, UnknownSettingError
from scoutforge.services.stats_service import StatsService, StatsSnapshot

__all__ = [
    "SETTINGS",
    "AlertDispatcher",
    "AlertOutcome",
    "AlertPayload",
    "AlertService",
    "Container",
    "DiscoveryWorker",
    "InvalidTransitionError",
    "LeadService",
    "LeadView",
    "ScanResult",
    "SettingsService",
    "StatsService",
    "StatsSnapshot",
    "UnknownSettingError",
    "UnknownStatusError",
    "allowed_transitions",
    "can_transition",
    "parse_status",
    "status_choices",
]
