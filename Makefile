.PHONY: up demo down test validate e2e-local

up:            ## start registries, OpenBao and the distributor
	docker compose up -d --build

demo: up       ## run the end-to-end scenario (fresh state: make down first)
	docker compose run --rm demo

down:
	docker compose down -v

test:          ## unit tests
	python3 -m pytest -q tests/test_units.py

validate:      ## validate demo configs (legacy-api is broken on purpose)
	-GITOPS_DIR=poc/gitops/repos python3 -m distributor --platform poc/platform.yaml validate

e2e-local:     ## same scenario without Docker (needs zot, bao, regctl on PATH)
	./poc/run-local.sh
