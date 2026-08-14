// The response envelope, taken from the published schema instead of re-spelled here (issue #1446).
//
// `schema.ts` next to this file is GENERATED from `openapi.json`, which is dumped straight off the
// FastAPI app (`scripts/dump_openapi.py`). Nothing in either file is hand-edited: the Python test
// `tests/unit/api/test_openapi_snapshot.py` fails when the snapshot is stale against the app, and
// UI Build regenerates `schema.ts` and fails on a diff. So a route that changes shape breaks the
// build here rather than at runtime in front of a user.
//
// What this file adds is ergonomics. `paths['/api/user/settings']['get']['responses'][200]
// ['content']['application/json']['detail']` is the truth, and nobody is going to write it twice.

import type { components, paths } from './schema'

/** Every path the published schema documents. `/api/admin/*` is deliberately not among them. */
export type ApiPath = keyof paths

/**
 * The `{status_code, detail}` envelope almost every route returns, with a payload this bundle
 * knows more about than the schema does.
 *
 * `status_code` comes from the generated component, so the envelope is described in exactly one
 * place. Reach for this only where `detail` is still an unnarrowed object in the schema — where it
 * is narrowed, `ApiDetail` below gives you the real payload type for free.
 */
export type ApiEnvelope<Detail> =
  Omit<components['schemas']['ResponseModel_dict_str__Any__'], 'detail'> & { detail: Detail }

/** The 200 JSON body of one operation, e.g. `ApiResponse<paths['/api/app-info']['get']>`. */
export type ApiResponse<Operation> = Operation extends {
  responses: { 200: { content: { 'application/json': infer Body } } }
}
  ? Body
  : never

/** The `detail` payload of one operation's 200 envelope. */
export type ApiDetail<Operation> =
  ApiResponse<Operation> extends { detail: infer Detail } ? Detail : never

/** The `detail` payload of a GET, by path — `GetDetail<'/api/user/settings'>`. */
export type GetDetail<Path extends ApiPath> =
  paths[Path] extends { get: infer Operation } ? ApiDetail<Operation> : never
