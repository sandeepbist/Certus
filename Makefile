.PHONY: setup dev build check smoke clean docker-up docker-down docker-logs docker-reset migrate help

# Default target
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# === Development ===

setup: ## Complete local setup (dependencies, infrastructure, migrations)
	@bash scripts/setup-dev.sh

dev: ## Start all services in development mode
	@echo "🔧 Starting Certus in development mode..."
	@bun run dev

dev-web: ## Start only the web service
	@bun run dev:web

dev-gateway: ## Start only the gateway service
	@bun run dev:gateway

build: ## Build all services
	@bun run build

check: ## Run lint, types, unit tests, and Python contracts
	@bun run check

smoke: ## Probe the running local application and infrastructure
	@bun run smoke

clean: ## Clean generated build and test artifacts (keeps dependencies)
	@bun run clean

# === Docker ===

docker-up: ## Start all infrastructure services
	@docker compose up -d
	@echo "🐳 Infrastructure services started"

docker-down: ## Stop all infrastructure services
	@docker compose down
	@echo "🐳 Infrastructure services stopped"

docker-logs: ## Follow infrastructure logs
	@docker compose logs -f

docker-reset: ## Reset all infrastructure (WARNING: destroys data)
	@docker compose down -v
	@$(MAKE) docker-up
	@sleep 5
	@$(MAKE) migrate
	@echo "🔄 Infrastructure reset complete"

# === Database ===

migrate: ## Run database migrations
	@bash scripts/migrate-db.sh
	@bash scripts/migrate-neo4j.sh
