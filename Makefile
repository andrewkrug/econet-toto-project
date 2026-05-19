# Rheem EcoNet telemetry pipeline.
#
# Two venvs by design (see README): .venv (collector + Datadog + recommender)
# and .venv-toto (Toto only -- separate because Toto's deps are heavy and
# version-touchy). Credentials live in ~/.config/rheem/env.

PY        ?= python3
PY312     ?= python3.12
VENV      := .venv
VENV_TOTO := .venv-toto
BIN       := $(VENV)/bin/python
BIN_TOTO  := $(VENV_TOTO)/bin/python
CONFIG    := $(HOME)/.config/rheem/env

# launchd job rendering: templates in launchd/*.plist.in get @REPO@/@PY@/
# @PYTOTO@ substituted and installed to ~/Library/LaunchAgents.
REPO       := $(CURDIR)
PY_ABS     := $(REPO)/$(VENV)/bin/python
PYTOTO_ABS := $(REPO)/$(VENV_TOTO)/bin/python
LA_DIR     := $(HOME)/Library/LaunchAgents
JOBS       := com.rheem.telemetry com.rheem.forecast com.rheem.recommend

# Overridable: make forecast COLUMN=set_point HORIZON=48
COLUMN    ?= tank_hot_water_availability
HORIZON   ?= 96
DASH_ID   ?=

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

# --- setup ----------------------------------------------------------------

.PHONY: setup
setup: $(BIN) ## Create .venv (3.14) and install collector deps

$(BIN):
	$(PY) -m venv $(VENV)
	$(BIN) -m pip install -q --upgrade pip
	$(BIN) -m pip install -q -r requirements.txt

.PHONY: setup-toto
setup-toto: $(BIN_TOTO) ## Create .venv-toto (3.12) and install Toto deps

# Prefer `uv` (it's the toolchain here; `python3.12 -m venv` can't run
# ensurepip on uv-managed standalone CPython). Fall back to stdlib venv.
$(BIN_TOTO):
	@if command -v uv >/dev/null 2>&1; then \
		uv venv --seed --python 3.12 $(VENV_TOTO); \
	else \
		$(PY312) -m venv $(VENV_TOTO); \
	fi
	$(BIN_TOTO) -m pip install -q --upgrade pip
	$(BIN_TOTO) -m pip install -q pandas pyarrow torch \
		"toto-2 @ git+https://github.com/DataDog/toto.git#subdirectory=toto2"

.PHONY: config
config: ## Create ~/.config/rheem/env from env.example (won't overwrite)
	@if [ -f "$(CONFIG)" ]; then \
		echo "$(CONFIG) already exists; leaving it alone."; \
	else \
		mkdir -p "$(dir $(CONFIG))"; \
		cp env.example "$(CONFIG)"; \
		chmod 600 "$(CONFIG)"; \
		echo "Created $(CONFIG) (mode 600) -- now fill in ECONET_* / DD_*."; \
	fi

# --- run (read-only toward the heater) ------------------------------------

.PHONY: status
status: setup ## One-shot telemetry dump
	$(BIN) rheem.py status

.PHONY: energy
energy: setup ## Recent energy-usage history
	$(BIN) rheem.py energy

.PHONY: log
log: setup ## One collection -> telemetry.parquet (+ Datadog if configured)
	$(BIN) rheem.py log

.PHONY: dashboard
dashboard: setup ## Create/update the Datadog dashboard (DASH_ID=<id> to update)
	$(BIN) dashboard.py $(DASH_ID)

.PHONY: forecast
forecast: setup-toto ## Toto forecast (COLUMN=, HORIZON=) -> forecast.parquet + rheem.toto.*
	$(BIN_TOTO) toto_forecast.py $(COLUMN) --horizon $(HORIZON)

.PHONY: recommend
recommend: setup ## Advisory setpoint schedule (reads telemetry + forecast; never writes the heater)
	$(BIN) recommend.py

# --- scheduled pipeline (launchd) -----------------------------------------
# telemetry: every 15 min  |  forecast: hourly :00  |  recommend: hourly :10
# All metrics go to Datadog from inside the scripts (rheem.* / rheem.toto.* /
# rheem.reco.*); stdout/stderr land in logs/.

.PHONY: install-agent
install-agent: setup setup-toto ## Render + load all launchd jobs (telemetry, forecast, recommend)
	@mkdir -p "$(LA_DIR)" logs
	@for j in $(JOBS); do \
		out="$(LA_DIR)/$$j.plist"; \
		sed -e 's#@REPO@#$(REPO)#g' \
		    -e 's#@PY@#$(PY_ABS)#g' \
		    -e 's#@PYTOTO@#$(PYTOTO_ABS)#g' \
		    "launchd/$$j.plist.in" > "$$out"; \
		launchctl unload "$$out" 2>/dev/null || true; \
		launchctl load "$$out" && echo "loaded $$out"; \
	done
	@echo "Scheduled: telemetry /15min, forecast hourly :00, recommend hourly :10"

.PHONY: uninstall-agent
uninstall-agent: ## Unload + remove all launchd jobs
	@for j in $(JOBS); do \
		out="$(LA_DIR)/$$j.plist"; \
		launchctl unload "$$out" 2>/dev/null || true; \
		rm -f "$$out"; \
		echo "removed $$out"; \
	done

# --- housekeeping ---------------------------------------------------------

.PHONY: lint
lint: setup ## Byte-compile all Python (quick syntax check)
	$(BIN) -m py_compile rheem.py dashboard.py toto_forecast.py recommend.py

.PHONY: clean
clean: ## Remove venvs, caches, and generated data files
	rm -rf $(VENV) $(VENV_TOTO) __pycache__ *.pyc
	rm -f rheem.jsonl collector.log telemetry.parquet forecast.parquet \
		recommendation.parquet
