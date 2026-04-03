.PHONY: all single executable test clean

all: single executable

single: build/sux

build/sux: $(wildcard src/sux/*.py) src/build_single_file.py
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

clean:
	rm -rf build
