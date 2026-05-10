# script.report Makefile
# Usage: make <target>
# On Windows, install make via: winget install GnuWin32.Make
# Or just run the python -m script_report commands directly.

PYTHON = python -m script_report

.PHONY: help setup refresh download extract spend schedule embed atc calendar build deploy clean

help:
	@echo ""
	@echo "  script.report — available targets"
	@echo ""
	@echo "  make setup      Install Python dependencies (pip install -e .)"
	@echo "  make refresh    Full pipeline: download -> extract -> build -> deploy"
	@echo "  make download   Download new PBAC PSDs"
	@echo "  make extract    Re-extract PSD data via Claude Haiku (--resume)"
	@echo "  make spend      Fetch PBS drug spend Excel"
	@echo "  make schedule   Backfill ATC codes from PBS Schedule API CSV bundle"
	@echo "  make embed      Embed PSDs via Voyage AI (--resume)"
	@echo "  make atc        Parse ATC class data"
	@echo "  make calendar   Parse PBS Cycle Timeframe PDFs"
	@echo "  make build      Build site_data.js"
	@echo "  make deploy     Push to Vercel production"
	@echo "  make clean      Remove generated CSV/JSON outputs"
	@echo ""

setup:
	pip install -e . --break-system-packages

refresh:
	$(PYTHON) refresh

download:
	$(PYTHON) download

extract:
	$(PYTHON) extract --resume

spend:
	$(PYTHON) spend

schedule:
	$(PYTHON) schedule

embed:
	$(PYTHON) embed --resume

atc:
	$(PYTHON) atc

calendar:
	$(PYTHON) calendar

build:
	$(PYTHON) build

deploy: build
	vercel --prod

clean:
	@echo "Removing generated CSV/JSON files from data/..."
	-del data\psd_extracted.csv 2>nul
	-del data\pbs_drug_spend.csv 2>nul
	-del data\atc_benefit.csv 2>nul
	-del data\atc_services.csv 2>nul
	-del data\psd_nearest.json 2>nul
	-del data\psd_embeddings.bin 2>nul
	-del data\psd_embeddings_meta.json 2>nul
	@echo "Done. Run 'make build' to regenerate."
