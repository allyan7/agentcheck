# more-itertools

Pure-Python library of iterator building blocks. No runtime dependencies.

## Layout
- `more_itertools/more.py`: most functions. `more_itertools/recipes.py`: recipes from the itertools docs.
- `more_itertools/*.pyi`: type stubs. If you change a function's signature, update its stub too.
- `tests/test_more.py`, `tests/test_recipes.py`: unittest-style tests, run with pytest.

## How to work
- Read the function and its existing tests before changing anything.
- Reproduce the bug with a quick `python -c` or a new test first, then fix it.
- Keep fixes minimal. Don't rename or reorder public parameters.
- Functions must stay lazy: never consume a whole iterable unless the function's docs say it does.
- Run `python -m pytest -q tests/test_more.py tests/test_recipes.py` before you finish.
