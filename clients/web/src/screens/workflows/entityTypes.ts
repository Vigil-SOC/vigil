/** The ten kinds an Entity Key can carry, held to the hunt's own closed set by
 *  the spec loader, which refuses anything else rather than coercing it. Spelled
 *  here so the operator is told before submitting. No imports, so
 *  `scripts/check_entity_key_agreement.py` can read it under Node. */
export const ENTITY_TYPES = ['ip', 'domain', 'host', 'url', 'email', 'hash', 'arn', 'aws_key', 'user', 'process']
