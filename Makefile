# script.report Makefile
# Usage: make <target>
# On Windows, install make via: winget install GnuWin32.Make
# Or just run the Python commands directly (see refresh.py)

PYTHON = python

.PHONY: help setup refresh extract spend embed build deploy clean

help:
	@echo ""
	@echo "  script.report — available targets"
	@echo ""
	@echo "  make setup      Install Python dependencies"
	@echo "  make refresh    Full pipeline: download → extract → build → deploy"
	@echo "  make extract    Re-extract PSD data via Claude Haiku (--resume)"
	@echo "  make spend      Fetch PBS drug spend Excel"
	@echo "  make embed      Embed PSDs via Voyage AI (--resume)"
	@echo "  make build      Build site_data.js from CSVs"
	@echo "  make deploy     Push to Vercel production"
	@echo "  make clean      Remove generated data files (NOT PDFs)"
	@echo ""

setup:
	pip install -r requirements.txt --break-system-packages

refresh:
	$(PYTHON) refresh.py

extract:
	$(PYTHON) extract_psd_text.py --resume

spend:
	$(PYTHON) fetch_pbs_drug_spend.py

embed:
	$(PYTHON) embed_psds.py --resume

build:
	$(PYTHON) build_site_data.py

deploy: build
	vercel --prod

clean:
	@echo "Removing generated CSV files from data/..."
	-del data\psd_extracted.csv 2>nul
	-del data\pbs_drug_spend.csv 2>nul
	-del data\atc_benefit.csv 2>nul
	-del data\atc_services.csv 2>nul
	@echo "Done. Run 'make build' to regenerate."
