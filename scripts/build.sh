#!/bin/bash
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

usage() {
    echo "Usage: $0 {build|up|down|logs|shell-lisa}"
    echo
    echo "Commands:"
    echo "  build       Build all Docker images"
    echo "  up          Start all services"
    echo "  down        Stop all services"
    echo "  logs        Tail logs for all services"
    echo "  shell-lisa  Open a shell in the lisa container"
    exit 1
}

case "${1:-}" in
    build)
        echo "Building base image..."
        docker build -t ai-dev-team-base -f docker/Dockerfile.base docker/

        echo "Building playwright image..."
        docker build -t ai-dev-team-playwright -f docker/Dockerfile.playwright docker/

        echo "Building compose services..."
        docker compose build
        ;;
    up)
        docker compose up -d
        echo "Services started. Use '$0 logs' to view output."
        ;;
    down)
        docker compose down
        ;;
    logs)
        docker compose logs -f
        ;;
    shell-lisa)
        docker compose exec lisa /bin/bash
        ;;
    *)
        usage
        ;;
esac
