# make install / make test / make lint / make up   (docs/00 §2)
PY ?= python3

.PHONY: install install-pi test lint fmt up bindings publish

install:
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pip install -e shared/py -e rover -e "gx10[brain,mapper,overhead]"
	cd spacetimedb && npm install
	cd dashboard && npm install

install-pi:
	$(PY) -m pip install -e shared/py -e "rover[pi]"

# pytest exits 5 when no tests are collected; treat that as a pass while the repo is empty.
test:
	$(PY) -m pytest || [ $$? -eq 5 ]

lint:
	$(PY) -m ruff check .
	$(PY) -m black --check .
	cd spacetimedb && npm run typecheck
	cd dashboard && npm run lint

fmt:
	$(PY) -m ruff check --fix .
	$(PY) -m black .

publish:
	./scripts/publish_stdb.sh

bindings:
	./scripts/gen_bindings.sh

up: publish bindings
	./scripts/dev_up.sh
