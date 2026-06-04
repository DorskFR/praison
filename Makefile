APP ?= praison
TESTS ?= ./tests
PYTHON ?= uv run python

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} \;
	find . -type d -name .cache -prune -exec rm -rf {} \;
	find . -type d -name .mypy_cache -prune -exec rm -rf {} \;
	find . -type d -name .pytest_cache -prune -exec rm -rf {} \;
	find . -type d -name .ruff_cache -prune -exec rm -rf {} \;
	find . -type d -name venv -prune -exec rm -rf {} \;

lint:
	$(PYTHON) -m ruff check ./$(APP) $(TESTS)
	$(PYTHON) -m ruff format --check ./$(APP) $(TESTS)
	$(PYTHON) -m mypy --cache-dir .cache/mypy_cache ./$(APP) $(TESTS)

lint/fix:
	$(PYTHON) -m ruff check --fix-only ./$(APP) $(TESTS)
	$(PYTHON) -m ruff format ./$(APP) $(TESTS)

run:
	$(PYTHON) -m $(APP)

setup:
	uv sync

test:
	$(PYTHON) -m pytest --rootdir=. -o cache_dir=.cache/pytest_cache $(TESTS) -s -x -v $(options)

docker/build:
	docker build -t praison .

docker/run:
	docker compose up --build

.PHONY: $(shell grep --no-filename -E '^([a-zA-Z_-]|\/)+:' $(MAKEFILE_LIST) | sed 's/:.*//')
