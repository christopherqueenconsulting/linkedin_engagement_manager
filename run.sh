#!/bin/bash

# Load the .env file (Docker Compose also loads this automatically)
export $(grep -v '^#' .env | xargs)

NGROK_PLAN="${NGROK_PLAN:-off}"
UI_SRC_DIR="src/cqc_lem/ui/src"
UI_DIST_REF="src/cqc_lem/ui/dist/index.html"

# ─── Current container status ────────────────────────────────────────────────
printf "Current container status:\n"
if docker compose ps 2>/dev/null | grep -q "running\|Up\|healthy"; then
    docker compose ps
else
    printf "  (no containers running)\n"
fi
printf "\n"

# ─── Step 1: Build Docker image ──────────────────────────────────────────────
read -p "Do you want to build the latest Docker image(s)? (y/n): " build_image
if [ "$build_image" == "y" ]; then
    echo "Building Docker images (${DOCKER_IMAGE_NAME}:latest)..."
    docker compose build
    read -p "Do you want to push the built Docker image(s) to Docker Hub? (y/n): " push_image
    if [ "$push_image" == "y" ]; then
      # Push the main app image (shared by web_app, api, celery_worker, celery_beat, flower)
      docker compose push web_app
    fi
fi

# ─── Step 1b: React UI — rebuild if source changed since last build ──────────
# dist/ is volume-mounted (./src:/app/src) so a local npm build is picked up
# by the running container immediately — no Docker rebuild needed.
NEEDS_UI_BUILD=false
if [ ! -f "$UI_DIST_REF" ]; then
    printf "\nWarning: React dist/ not found — the UI will be blank until it is built.\n"
    NEEDS_UI_BUILD=true
elif [ -d "$UI_SRC_DIR" ] && \
     find "$UI_SRC_DIR" -newer "$UI_DIST_REF" \
         \( -name "*.tsx" -o -name "*.ts" -o -name "*.css" -o -name "*.json" \) \
         2>/dev/null | grep -q .; then
    printf "\nReact UI source has changed since the last build.\n"
    NEEDS_UI_BUILD=true
fi

if [ "$NEEDS_UI_BUILD" = "true" ]; then
    read -p "Rebuild React UI now? (y/n): " rebuild_ui
    if [ "$rebuild_ui" = "y" ]; then
        printf "Building React UI...\n"
        (cd src/cqc_lem/ui && npm run build)
    fi
fi

# ─── Step 2: Start containers ────────────────────────────────────────────────
echo "Starting Docker Containers..."
docker compose up -d --remove-orphans

# ─── Step 2b: Status after startup ───────────────────────────────────────────
printf "\nContainer status:\n"
docker compose ps
printf "\n"

# ─── Step 2c: Celery — restart if task files changed since worker started ────
# Python API changes auto-reload via uvicorn --reload (no action needed).
# Celery workers do NOT auto-reload; this detects stale task code and prompts.
# Both workers (main + selenium) share the same codebase so we restart both.
_celery_needs_restart() {
    local container="$1"
    docker ps -q -f "name=^${container}$" 2>/dev/null | grep -q . || return 1
    docker inspect --format='{{.State.StartedAt}}' "$container" 2>/dev/null | \
        python3 -c "
import sys, os
from datetime import datetime, timezone
try:
    ts = sys.stdin.read().strip()[:19]
    start = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
    for root, _, files in os.walk('src/cqc_lem/app'):
        for f in files:
            if f.endswith('.py') and os.path.getmtime(os.path.join(root, f)) > start:
                print('yes'); sys.exit(0)
except Exception:
    pass
" 2>/dev/null | grep -q "yes"
}

if _celery_needs_restart celery_worker || _celery_needs_restart celery_worker_selenium; then
    printf "Celery task files in src/cqc_lem/app/ changed since a worker last started.\n"
    read -p "Restart celery_worker + celery_worker_selenium now? (y/n): " restart_celery
    if [ "$restart_celery" = "y" ]; then
        docker compose restart celery_worker celery_worker_selenium
    fi
fi

# ─── Step 3: Clean up old images and build cache ─────────────────────────────
echo "Cleaning up old Docker images older than 7 days..."
docker image prune -a --filter "until=168h" -f

echo "Cleaning up Docker build cache older than 7 days..."
docker builder prune --filter "until=168h" -f

printf "\nExecution completed!\n\n"

# ─── Step 4 + 5: NGrok tunnels ───────────────────────────────────────────────
if [ "$NGROK_PLAN" != "off" ]; then
    if [ -z "$NGROK_AUTH_TOKEN" ]; then
        printf "Warning: NGROK_PLAN=%s but NGROK_AUTH_TOKEN is not set — skipping NGrok.\n" "$NGROK_PLAN"
        NGROK_PLAN="off"
    else
        # Generate the config for the selected plan
        ./generate-ngrok-config.sh

        printf "Starting NGrok Tunnels (plan: %s)...\n" "$NGROK_PLAN"
        mkdir -p ./logs
        touch ./logs/ngrok.log
        pkill -f ngrok || true
        sleep 3  # wait for cloud session to deregister (ERR_NGROK_108)
        ngrok start --config ./ngrok-config.yml --all --log='stdout' > ./logs/ngrok.log 2>&1 &
        NGROK_PID=$!

        # Brief wait to catch immediate startup errors
        sleep 3
        if ! kill -0 "$NGROK_PID" 2>/dev/null; then
            printf "\nWarning: NGrok exited unexpectedly. Relevant errors from ./logs/ngrok.log:\n"
            grep -E "lvl=(eror|crit)|^ERROR" ./logs/ngrok.log | tail -5 | sed 's/^/  /'
            printf "\nFalling back to local URLs.\n\n"
            NGROK_PLAN="off"
        else
            printf "NGrok running (PID %s). Tunnel URLs below or at http://localhost:%s\n\n" "$NGROK_PID" "$NGROK_UI_PORT"
        fi
    fi
fi

# ─── Step 6: Build URL display ───────────────────────────────────────────────

# For the free plan, the Flower tunnel has no reserved domain so it gets a random URL.
# Query the ngrok API to surface the actual live URL instead of sending users to the UI.
FLOWER_NGROK_URL=""
if [ "$NGROK_PLAN" = "free" ]; then
    FLOWER_NGROK_URL=$(curl -sf "http://localhost:${NGROK_UI_PORT}/api/tunnels" 2>/dev/null | \
        python3 -c "
import sys, json
try:
    tunnels = json.load(sys.stdin).get('tunnels', [])
    for t in tunnels:
        if 'flower' in t.get('name', '').lower():
            print(t['public_url'])
            break
except Exception:
    pass
" 2>/dev/null || echo "")
fi

titles=("React Web App" "API Docs" "Flower Celery Monitoring" "Docker Chrome VNC")

case "$NGROK_PLAN" in
  paid)
    urls=(
      "https://${NGROK_CUSTOM_DOMAIN}"
      "https://${NGROK_CUSTOM_DOMAIN}/api/docs"
      "https://${NGROK_FLOWER_PREFIX}.${NGROK_FREE_DOMAIN}"
      "https://${NGROK_CHROME_PREFIX}.${NGROK_FREE_DOMAIN}"
    )
    titles+=("Ngrok Web Interface")
    urls+=("http://localhost:${NGROK_UI_PORT}")
    ;;
  free)
    # Free plan: lem-app uses static domain; lem-flower gets a dynamic URL (fetched above).
    # lem-chrome is excluded to stay within the 3-tunnel free-plan limit.
    urls=(
      "https://${NGROK_CUSTOM_DOMAIN}"
      "https://${NGROK_CUSTOM_DOMAIN}/api/docs"
      "${FLOWER_NGROK_URL:-Dynamic — see http://localhost:${NGROK_UI_PORT}}"
      "http://localhost:7900 (local only)"
    )
    titles+=("Ngrok Web Interface")
    urls+=("http://localhost:${NGROK_UI_PORT}")
    ;;
  *)
    urls=(
      "http://localhost:${API_PORT}"
      "http://localhost:${API_PORT}/api/docs"
      "http://localhost:${CELERY_FLOWER_PORT}"
      "http://localhost:7900"
    )
    ;;
esac

# Reference URLs (always shown)
reference_titles=("PostHog (observability)" "OpenAI (usage cost)" "RunwayML (video gen)" "Replicate (image gen)")
reference_urls=("https://us.posthog.com/" "https://platform.openai.com/usage" "https://app.runwayml.com/" "https://replicate.com/")
titles+=("${reference_titles[@]}")
urls+=("${reference_urls[@]}")

# Function to print the titles and URLs with a dynamic separator line
print_urls() {
    local -a titles=("${!1}")
    local -a urls=("${!2}")

    local max_length=0
    local print_contents=()
    local max_title_length=0

    for title in "${titles[@]}"; do
        if (( ${#title} > max_title_length )); then
            max_title_length=${#title}
        fi
    done

    for i in "${!titles[@]}"; do
        local line
        line=$(printf "%-${max_title_length}s\t%s" "${titles[$i]}" "${urls[$i]}")
        local length=${#line}
        if (( length > max_length )); then
            max_length=$length
        fi
        print_contents+=("$line")
    done

    (( max_length += 10 ))

    local separator=$(printf "%${max_length}s\n" | tr ' ' '=')

    local service_padding=$(( ((max_title_length + 4 ) / 2) - (7/2) ))
    local service_padding_right=$(( max_title_length - service_padding + 5 ))
    local url_remainder=$(( max_length - service_padding - service_padding_right - 7 ))
    local url_padding=$(( ((max_length - url_remainder ) / 2) + (3/2) ))
    local url_padding_right=$(( url_remainder - url_padding  ))

    local header
    header=$(printf "| %*s%-*s | %*s%-*s |" $service_padding " " $service_padding_right "Service" $url_padding " " $url_padding_right "URL")

    printf "\n\n%s\n%s\n%s\n%s\n%s\n\n\n" "$separator" "$header" "$separator" "$(printf "%s\n" "${print_contents[@]}")" "$separator"
}

print_urls "titles[@]" "urls[@]"

# ─── Dev workflow quick reference ────────────────────────────────────────────
printf "Dev workflow (no Docker rebuild needed):\n"
printf "  Python changes  → auto-reloaded by uvicorn  (no action needed)\n"
printf "  React UI change → cd src/cqc_lem/ui && npm run build\n"
printf "  Celery changes  → docker compose restart celery_worker celery_worker_selenium\n"
printf "  View logs       → docker compose logs -f <service>\n"
printf "  Stop all        → docker compose down\n\n"

# Remove Dangling Images
docker image prune -f --filter "dangling=true"
