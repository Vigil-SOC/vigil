import type * as Api from '../services/api'

type Module = typeof Api

/**
 * The shape a vi.mock factory of services/api may take: only names the real module
 * exports. Checked with `satisfies`, a mock of a method that no longer exists fails
 * tsc instead of keeping a test green over a call that breaks in the app.
 */
export type ApiMock = {
  [K in keyof Module]?: Module[K] extends (...args: never[]) => unknown
    ? unknown
    : { [M in keyof Module[K]]?: unknown }
}
