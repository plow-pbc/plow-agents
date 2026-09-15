# Everything CI runs, in the order CI runs it.
default: lint test

lint:
    uv run ruff check .

test:
    uv run python tests/smoke-line-in-use.py
    uv run python tests/smoke-image-deploy.py
