# Generated from the backend OpenAPI spec. Do not edit.

Regenerate from the repo root:

```
python scripts/generate_frontend_types.py
```

or from `clients/web`:

```
npm run generate:api
```

CI fails if regeneration produces a diff. Unannotated routes stay empty in
the spec; that is expected until their module is sliced.

The generator pins the environment it dumps the spec under (context path,
webhook feature gates, state directory), so the output does not depend on your
shell, your `.env`, whether you have run `npm run build`, or what is in your
local database. If regeneration produces a diff, it is a real API change.
