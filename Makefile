.PHONY: dev ui test

# API + both workers in one terminal; Ctrl-C stops everything
dev:
	./scripts/dev.sh

# rebuild the dashboard (the API serves frontend/dist at /ui)
ui:
	cd frontend && npm run build

test:
	uv run pytest -q
