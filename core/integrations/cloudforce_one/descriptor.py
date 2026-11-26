"""Cloudforce One integration descriptor — source of truth for its registry entries.

The poller and the credential check both resolve this descriptor. There is no
MCP server: Vigil pulls the TAXII collections itself.
"""

from typing import Any, Dict, Optional

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    TaxiiFeed,
    parse_collection_ids,
    register_descriptor,
)


def _taxii_feed(config: Dict[str, Any]) -> Optional[TaxiiFeed]:
    """Its existing fields are its feed; no TLS fields, so it always verifies."""
    collection_ids = parse_collection_ids(config.get("collection_ids"))
    if not collection_ids:
        return None
    from core.config import get_integration_config

    # Saved only: the resolver would read CLOUDFORCE_ONE_POLL_INTERVAL_SECONDS,
    # which is not a fallback, ahead of THREAT_FEED_POLL_INTERVAL.
    stored = get_integration_config("cloudforce_one").get("poll_interval_seconds")
    interval = None if stored in (None, "") else config.get("poll_interval_seconds")
    return TaxiiFeed(
        server_url=config.get("taxii_server_url"),
        api_token=config.get("api_token"),
        collection_ids=collection_ids,
        poll_interval_seconds=interval,
    )


CLOUDFORCE_ONE = register_descriptor(
    IntegrationDescriptor(
        id="cloudforce_one",
        category="Threat Intelligence",
        fields=(
            IntegrationField("api_token", secret=True),
            IntegrationField("taxii_server_url"),
            IntegrationField("collection_ids"),
            IntegrationField("poll_interval_seconds", value_type="int"),
        ),
        taxii_feed=_taxii_feed,
    )
)
