# Travel Planner Kyushu - Makefile
# Usage: make <target>

.PHONY: help mock-demo ui api test lint clean

help:
	@echo "Travel Planner Agent (Kyushu 5-day Self-Drive) - Available targets:"
	@echo "  make mock-demo     Run the mock end-to-end demo (prints plan summary)"
	@echo "  make ui            Launch Streamlit UI (MOCK_TOOLS=1 recommended)"
	@echo "  make api           Launch FastAPI dev server"
	@echo "  make test          Run pytest (unit + integration)"
	@echo "  make lint          Run ruff check + format check"
	@echo "  make clean         Remove caches and build artifacts"

mock-demo:
	@echo "🚗 Running MOCK mode 5-day Kyushu demo..."
	MOCK_TOOLS=1 python -m src.core.graph --demo

ui:
	@echo "🖥️  Starting Streamlit UI (Phase 1 PoC)..."
	@echo "   建議先執行：MOCK_TOOLS=1 streamlit run ui/streamlit_app.py"
	streamlit run ui/streamlit_app.py --server.port 8501

api:
	@echo "🌐 Starting FastAPI server..."
	uvicorn api.main:app --reload --port 8000

test:
	pytest tests/ -v --tb=short

lint:
	ruff check .
	ruff format --check .

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf build dist *.egg-info 2>/dev/null || true
	@echo "Cleaned caches."