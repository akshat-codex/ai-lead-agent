# Tests

Each package owns its own test suite for now:

- Backend: [`backend/tests/`](../backend/tests/) — pytest, run with `pytest` from `backend/`.
- Frontend: no business logic yet; `npm run build` (from `frontend/`) is the compile/type-check smoke
  test until real UI logic exists.

This top-level directory is reserved for future cross-cutting and end-to-end tests (e.g. tests that
exercise the frontend and backend together, or full pipeline tests once later phases are implemented).
