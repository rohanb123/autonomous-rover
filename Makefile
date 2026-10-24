# make install / make test / make lint / make up   (docs/00 §2)
PY ?= python3

.PHONY: install install-gx10 install-pi test lint fmt up bindings publish

# Laptop / dev machine: everything except the heavy ML stack (vision, voice, ml).
install:
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pip install -e shared/py -e rover
	$(PY) -m pip install -r gx10/brain/requirements.txt -r gx10/mapper/requirements.txt -r gx10/overhead/requirements.txt
	cd spacetimedb && npm install
	cd dashboard && npm install

# GX10: every service, including the ML stack.
install-gx10:
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pip install -e shared/py
	$(PY) -m pip install -r gx10/brain/requirements.txt -r gx10/brain/voice/requirements.txt \
		-r gx10/vision/requirements.txt -r gx10/mapper/requirements.txt \
		-r gx10/overhead/requirements.txt -r ml/requirements.txt
	cd spacetimedb && npm install

install-pi:
	$(PY) -m pip install -e shared/py -e "rover[pi]"

# pytest exits 5 when no tests are collected; treat that as a pass while the repo is empty.
test:
	$(PY) -m pytest || [ $$? -eq 5 ]
	cd dashboard && npm test

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
