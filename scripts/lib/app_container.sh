# shellcheck shell=bash
# The container a host cron execs python into (#2160). Source it; do not run it.
#
# In prod `web_app` is the nginx edge and has no python, so `docker exec web_app python …` fails.
# deploy.sh records the colour the tunnel is routed to in $LEM_ROOT/.active_color; that web_api
# container is the one guaranteed to be up. An unreadable or unexpected value falls back to blue —
# the file's content is never echoed back, because it becomes a `docker exec` target.
active_api_container(){
  local color
  color="$(tr -d '[:space:]' < "${LEM_ROOT:-/opt/lem}/.active_color" 2>/dev/null || true)"
  case "$color" in
    blue|green) echo "web_api_$color" ;;
    *) echo "web_api_blue" ;;
  esac
}
