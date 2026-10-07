import type { components } from './generated/schema'

/** A named schema from the generated OpenAPI types. */
export type Schema<Name extends keyof components['schemas']> =
  components['schemas'][Name]
