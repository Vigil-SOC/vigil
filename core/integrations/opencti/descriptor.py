"""OpenCTI integration descriptor — source of truth for its registry entries.

Read-only: observable, indicator and entity lookups over the GraphQL API, and
the TAXII 2.1 collections the threat feed poller pulls into threat_indicators.
"""

from typing import Any, Dict, Optional

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    TaxiiFeed,
    parse_collection_ids,
    register_descriptor,
)

OPENCTI_REQUIRED = ("url", "api_token")


def _taxii_feed(config: Dict[str, Any]) -> Optional[TaxiiFeed]:
    """OpenCTI serves TAXII 2.1 discovery at ``{url}/taxii2/``."""
    collection_ids = parse_collection_ids(config.get("collection_ids"))
    if not collection_ids:
        return None
    url = config.get("url")
    return TaxiiFeed(
        server_url=f"{str(url).rstrip('/')}/taxii2/" if url else None,
        api_token=config.get("api_token"),
        collection_ids=collection_ids,
        verify_ssl=config.get("verify_ssl") is not False,
        ca_cert_path=config.get("ca_cert_path") or None,
    )


OPENCTI = register_descriptor(
    IntegrationDescriptor(
        id="opencti",
        category="Threat Intelligence",
        mcp_server_names=("opencti",),
        fields=(
            IntegrationField("url"),
            IntegrationField("api_token", secret=True),
            # Unset verifies: skipping it sends the API token to whoever answers.
            IntegrationField("verify_ssl", value_type="bool", default=True),
            IntegrationField("ca_cert_path"),
            # Comma-separated TAXII collection ids the threat feed poller pulls
            # from {url}/taxii2/. Blank: lookups only, nothing is polled.
            IntegrationField("collection_ids"),
        ),
        taxii_feed=_taxii_feed,
    )
)
