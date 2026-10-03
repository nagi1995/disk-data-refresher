# Working plan

For a behaviour change, use this order:

1. Read the relevant context documents in `.agents` and the affected code,
   tests, and requirements in `prompt.txt`.
2. Identify safety and data-state effects before editing.
3. Make the smallest scoped implementation and documentation changes.
4. Add or update focused tests.
5. Run focused tests and then `python -m pytest` when practical.
6. Update `memory.md`, `decisions.md`, `data_contracts.md`, or
   `open_questions.md` when the change creates durable context.

There is no active uncompleted implementation plan at this time.
