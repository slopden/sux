.PHONY: all single executable test clean

all: single executable

single: build/sux

build/sux: $(shell find src/sux -type f -not -path '*/__pycache__/*') src/build_single_file.py
	uv run src/build_single_file.py
	uv run ruff check --fix build/sux
	uv run ruff format build/sux

executable: build/sux
	PYTHONPATH=src uv run -m nuitka --standalone --onefile \
		--output-dir=build \
		--output-filename=sux-bin \
		--include-package=sux \
		--include-data-dir=src/sux/resources=sux/resources \
		src/sux/__main__.py

test: build/sux
	uv run ruff check --fix src/
	uv run ruff format --check src/
	uv run ruff check build/sux
	uv run ruff format --check build/sux
	uv run python -c "import ast; ast.parse(open('build/sux').read())"
	build/sux --help >/dev/null
	PYTHONPATH=src uv run pytest tests/ -v
	build/sux --help | grep -q "sandboxing"
	@echo "All checks passed"

.PHONY: install
install: build/sux
	install -D -m 0755 build/sux $(HOME)/.local/bin/sux
	@echo "Installed $(HOME)/.local/bin/sux"

clean:
	rm -rf build
