"""Where a detection's expanded row links, when the source has a page for it."""

from __future__ import annotations

from string import Formatter
from typing import Mapping, Optional

from core.federation.registry import get_adapter, is_registered
from core.integrations._base.config import resolve
from core.integrations._base.descriptor import IntegrationDescriptor, get_descriptor
from core.integrations.integration_bridge_service import base_of

_formatter = Formatter()


def _http_ref(evidence_links: object) -> Optional[str]:
    if not isinstance(evidence_links, list):
        return None
    for link in evidence_links:
        if not isinstance(link, Mapping):
            continue
        ref = link.get("ref")
        if not isinstance(ref, str):
            continue
        text = ref.strip()
        if text.lower().startswith(("http://", "https://")):
            return text
    return None


def descriptor_for_source(source: str) -> Optional[IntegrationDescriptor]:
    """The descriptor behind a stored ``data_source`` name.

    Stored names (``azure_sentinel``) differ from descriptor ids
    (``azure-sentinel``) for some sources; the federation adapter carries the
    id. An instance's (#5353) is its base's. A source with no adapter is looked
    up by its own name.
    """
    base = base_of(source)
    if base:
        return get_descriptor(base)
    adapter = get_adapter(source) if is_registered(source) else None
    return get_descriptor(getattr(adapter, "integration_id", source))


def _template_values(
    descriptor: IntegrationDescriptor, integration_id: Optional[str] = None
) -> dict[str, str]:
    """Non-secret field values, an instance's own when ``integration_id`` names
    one. A secret in the template does not fill."""
    try:
        resolved = resolve(descriptor, integration_id=integration_id)
    except Exception:
        resolved = {}
    values: dict[str, str] = {}
    for field in descriptor.fields:
        if field.secret:
            continue
        raw = resolved.get(field.name)
        if raw is None or raw == "":
            continue
        values[field.name] = str(raw)
    return values


def _fill(template: str, values: Mapping[str, str]) -> Optional[str]:
    needed = [name for _, name, _, _ in _formatter.parse(template) if name]
    if any(not values.get(name) for name in needed):
        return None
    try:
        return template.format(**{name: values[name] for name in needed})
    except (KeyError, IndexError, ValueError):
        return None


def resolve_source_link(
    finding: Optional[Mapping],
    *,
    configs: Optional[dict[str, dict[str, str]]] = None,
) -> Optional[str]:
    """First http(s) evidence ``ref``, else ``console_link_template``.

    ``configs`` is a per-call cache of resolved field values, keyed by source.
    """
    if not isinstance(finding, Mapping):
        return None
    direct = _http_ref(finding.get("evidence_links"))
    if direct:
        return direct
    source = finding.get("data_source")
    if not isinstance(source, str) or not source:
        return None
    descriptor = descriptor_for_source(source)
    template = descriptor.console_link_template if descriptor else None
    if not isinstance(template, str) or not template:
        return None
    if configs is None:
        configs = {}
    if source not in configs:
        # An instance's link is filled from its own tenant's config.
        configs[source] = _template_values(
            descriptor, source if base_of(source) else None
        )
    external = finding.get("external_id")
    values = dict(configs[source])
    if isinstance(external, str) and external:
        values["external_id"] = external
    return _fill(template, values)
