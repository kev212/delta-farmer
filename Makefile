.PHONY: prepare lint test update clean deploy foreach info stats-was stats-now

FOREACH_CLT := $(filter-out hyperliquid vault,$(basename $(notdir $(wildcard apps/*.py))))
FOREACH_CMD := $(strip $(cmd) $(if $(filter all,$(p)),,$(p)))
FOREACH_RUN = echo "\n── $(1) ──" && uv run -m apps.$(1) $(FOREACH_CMD) --no-banner || exit $$?

prepare: lint test

lint:
	uv run ruff format .
	uv run ruff check --fix .
	uv run pyright

test:
	uv run pytest -v

update:
	uv sync --upgrade --all-groups

clean:
	rm -rf .ruff_cache .venv uv.lock .python-version
	find . -type f -name "*.pyc" -delete

# --- Foreach ---

foreach:
	@if [ -z "$(FOREACH_CMD)" ]; then \
		echo 'usage: make foreach cmd="<command> [args...]" [p=last|this]'; \
		exit 2; \
	fi
	@$(foreach client,$(FOREACH_CLT),$(call FOREACH_RUN,$(client));)

info:
	@$(MAKE) -s foreach cmd="info"

stats-was:
	@$(MAKE) -s foreach cmd="stats last"

stats-now:
	@$(MAKE) -s foreach cmd="stats this"

# --- Deploy ---

HOST=lab
EXEC=ssh -tt $(HOST)
SYNC=rsync -avz --delete-after --exclude={'.git','.venv','.*cache','__pycache__','.DS_Store','*.pyc','.env'}
DDIR=~/delta-farmer
UV=~/.local/bin/uv

deploy:
	$(SYNC) ./ $(HOST):$(DDIR)
	$(EXEC) "cd $(DDIR) && $(UV) sync"
