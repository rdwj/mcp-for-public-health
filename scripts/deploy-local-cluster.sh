#!/usr/bin/env bash
# =============================================================================
# deploy-local-cluster.sh -- deploy the full stack to OpenShift Local (CRC)
# =============================================================================
# Bottom-up: MCP server -> agent -> gateway -> UI. Every image is built
# in-cluster (binary BuildConfig), so the laptop's CPU architecture never
# matters. See docs/deploying-on-openshift-local.md for the reasoning.
#
# Expected layout (override any path with the *_DIR variables):
#   <root>/my-mcp-server/   scaffolded with `fips-agents create mcp-server`
#   <root>/my-agent/        scaffolded with `fips-agents create agent`
#   <root>/my-gateway/      scaffolded with `fips-agents create gateway`
#   <root>/my-ui/           scaffolded with `fips-agents create ui`
#
# Usage:
#   export MODEL_API_KEY="..."                 # credential for the model endpoint
#   ./scripts/deploy-local-cluster.sh          # everything
#   ./scripts/deploy-local-cluster.sh mcp|agent|gateway|ui
#
# Variables (all optional):
#   CTX              kube context (default: current)
#   MCP_NS           namespace for the MCP server      (default: my-mcp-server)
#   AGENT_NS         namespace for agent/gateway/ui    (default: my-agent)
#   MCP_DIR, AGENT_DIR, GATEWAY_DIR, UI_DIR
#   MODEL_PROVIDER   litellm | openai | anthropic      (default: litellm)
#   MODEL_NAME       e.g. anthropic/claude-...  or  openai/<vllm-model-id>
#   MODEL_ENDPOINT   OpenAI-compatible URL for provider=openai (unused for litellm+anthropic)
#   MODEL_KEY_ENV    env var name the provider SDK reads (default: ANTHROPIC_API_KEY)
# =============================================================================
set -euo pipefail

ONLY="${1:-all}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX="${CTX:-$(oc config current-context)}"
MCP_NS="${MCP_NS:-my-mcp-server}"
AGENT_NS="${AGENT_NS:-my-agent}"
MCP_DIR="${MCP_DIR:-$ROOT/my-mcp-server}"
AGENT_DIR="${AGENT_DIR:-$ROOT/my-agent}"
GATEWAY_DIR="${GATEWAY_DIR:-$ROOT/my-gateway}"
UI_DIR="${UI_DIR:-$ROOT/my-ui}"
AGENT_NAME="$(basename "$AGENT_DIR")"
GATEWAY_NAME="$(basename "$GATEWAY_DIR")"
UI_NAME="$(basename "$UI_DIR")"
MODEL_PROVIDER="${MODEL_PROVIDER:-litellm}"
MODEL_NAME="${MODEL_NAME:-anthropic/claude-sonnet-4-5}"
MODEL_ENDPOINT="${MODEL_ENDPOINT:-}"
MODEL_KEY_ENV="${MODEL_KEY_ENV:-ANTHROPIC_API_KEY}"
SECRET_NAME="model-credentials"
ROUTE_TIMEOUT="180s"

want () { [[ "$ONLY" == "all" || "$ONLY" == "$1" ]]; }
ensure_ns () { oc get project "$1" --context="$CTX" >/dev/null 2>&1 || oc new-project "$1" --context="$CTX" >/dev/null; }

for t in oc helm; do command -v "$t" >/dev/null || { echo "Error: $t not found in PATH (eval \$(crc oc-env); brew install helm)"; exit 1; }; done
oc whoami --context="$CTX" >/dev/null || { echo "Error: not logged in (oc login first)"; exit 1; }
if want agent; then : "${MODEL_API_KEY:?Set MODEL_API_KEY for the model endpoint}"; fi
echo "==> context: $CTX"

# ---------------------------------------------------------------------------
# Wait for the newest build of a BuildConfig to finish; fail on anything but
# Complete. `oc start-build --follow` does not reliably propagate failure, and
# the phase can read Running for a few seconds after the push log ends.
# ---------------------------------------------------------------------------
wait_for_build () {   # name namespace
  local phase=""
  for _ in $(seq 1 90); do
    phase=$(oc get build -l buildconfig="$1" -n "$2" --context="$CTX" \
      --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].status.phase}')
    case "$phase" in
      Complete) return 0 ;;
      Failed|Error|Cancelled) echo "Error: [$1] build phase is '$phase'"; return 1 ;;
      *) sleep 2 ;;
    esac
  done
  echo "Error: [$1] build still '$phase' after 3 min"; return 1
}

# ---------------------------------------------------------------------------
# In-cluster build + Helm install for a scaffolded agent/gateway/ui project.
# ---------------------------------------------------------------------------
build_and_deploy () {   # name dir [helm --set args...]
  local name="$1" dir="$2"; shift 2
  [[ -d "$dir" ]] || { echo "Error: $dir not found"; exit 1; }
  echo; echo "==> [$name] -> $AGENT_NS"
  ensure_ns "$AGENT_NS"
  ( cd "$dir"
    if ! oc get bc "$name" -n "$AGENT_NS" --context="$CTX" >/dev/null 2>&1; then
      if [[ -f build/buildconfig.yaml ]]; then        # gateway / ui templates
        sed "s/PLACEHOLDER/$name/g" build/buildconfig.yaml | oc apply -n "$AGENT_NS" --context="$CTX" -f -
      else                                             # agent template
        oc new-build --binary --name="$name" --strategy=docker -n "$AGENT_NS" --context="$CTX"
        oc patch bc/"$name" --type=json \
          -p '[{"op":"replace","path":"/spec/strategy/dockerStrategy/dockerfilePath","value":"Containerfile"}]' \
          -n "$AGENT_NS" --context="$CTX"
      fi
    fi
    # The in-cluster build runs as a non-root UID; 600-mode files break it.
    find . -path ./.git -prune -o -type f -perm 600 -exec chmod 644 {} + 2>/dev/null || true
    oc start-build "$name" --from-dir=. --follow -n "$AGENT_NS" --context="$CTX" || true
    wait_for_build "$name" "$AGENT_NS"
    local image
    image=$(oc get is "$name" -n "$AGENT_NS" --context="$CTX" -o jsonpath='{.status.dockerImageRepository}')
    echo "==> [$name] image: $image:latest"
    helm upgrade --install "$name" chart/ \
      --set image.repository="$image" --set image.tag=latest --set image.pullPolicy=Always \
      "$@" -n "$AGENT_NS" --kube-context="$CTX"
    oc rollout restart deployment/"$name" -n "$AGENT_NS" --context="$CTX" >/dev/null 2>&1 || true
    oc rollout status  deployment/"$name" -n "$AGENT_NS" --context="$CTX" --timeout=300s
    oc annotate route "$name" haproxy.router.openshift.io/timeout="$ROUTE_TIMEOUT" \
      --overwrite -n "$AGENT_NS" --context="$CTX" >/dev/null 2>&1 || true
  )
}

# ---------------------------------------------------------------------------
# 1. MCP server (its own namespace; uses the generated deploy.sh)
# ---------------------------------------------------------------------------
if want mcp; then
  [[ -d "$MCP_DIR" ]] || { echo "Error: $MCP_DIR not found"; exit 1; }
  echo; echo "==> [mcp-server] -> $MCP_NS"
  ensure_ns "$MCP_NS"
  # The generated deploy.sh takes no --context flag; pin the current context.
  ( cd "$MCP_DIR" && oc config use-context "$CTX" >/dev/null && ./deploy.sh "$MCP_NS" )
fi

# ---------------------------------------------------------------------------
# 2. Agent: credential Secret + build + Helm
# ---------------------------------------------------------------------------
if want agent; then
  ensure_ns "$AGENT_NS"
  oc create secret generic "$SECRET_NAME" --from-literal="$MODEL_KEY_ENV=$MODEL_API_KEY" \
    -n "$AGENT_NS" --context="$CTX" --dry-run=client -o yaml | oc apply --context="$CTX" -f - >/dev/null
  extra=()
  [[ -n "$MODEL_ENDPOINT" ]] && extra+=(--set "config.MODEL_ENDPOINT=$MODEL_ENDPOINT")
  build_and_deploy "$AGENT_NAME" "$AGENT_DIR" \
    --set config.MODEL_PROVIDER="$MODEL_PROVIDER" \
    --set config.MODEL_NAME="$MODEL_NAME" \
    --set config.MCP_SERVER_URL="http://mcp-server.$MCP_NS.svc.cluster.local:8080/mcp/" \
    --set "env[0].name=$MODEL_KEY_ENV" \
    --set "env[0].valueFrom.secretKeyRef.name=$SECRET_NAME" \
    --set "env[0].valueFrom.secretKeyRef.key=$MODEL_KEY_ENV" \
    --set route.enabled=true \
    "${extra[@]}"
fi

# ---------------------------------------------------------------------------
# 3. Gateway, 4. UI -- in-cluster URLs, never the Routes
# ---------------------------------------------------------------------------
if want gateway; then
  build_and_deploy "$GATEWAY_NAME" "$GATEWAY_DIR" --set config.BACKEND_URL="http://$AGENT_NAME:8080"
fi
if want ui; then
  build_and_deploy "$UI_NAME" "$UI_DIR" --set config.API_URL="http://$GATEWAY_NAME:8080"
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
echo; echo "==> verify"
oc get pods -n "$MCP_NS"   --context="$CTX" 2>/dev/null | grep -v build || true
oc get pods -n "$AGENT_NS" --context="$CTX" 2>/dev/null | grep -v build || true
AG=$(oc get route "$AGENT_NAME"   -n "$AGENT_NS" --context="$CTX" -o jsonpath='{.spec.host}' 2>/dev/null || true)
GW=$(oc get route "$GATEWAY_NAME" -n "$AGENT_NS" --context="$CTX" -o jsonpath='{.spec.host}' 2>/dev/null || true)
UI=$(oc get route "$UI_NAME"      -n "$AGENT_NS" --context="$CTX" -o jsonpath='{.spec.host}' 2>/dev/null || true)
if [[ -n "$AG" ]]; then
  echo -n "agent tools:  "
  curl -sk --retry 5 --retry-delay 3 --retry-all-errors "https://$AG/v1/agent-info" \
    | python3 -c 'import json,sys;print([t["name"] for t in json.load(sys.stdin)["tools"]])' \
    || echo "(agent-info not ready yet; retry in a few seconds)"
fi
[[ -n "$GW" ]] && { echo -n "gateway:      "; curl -sk "https://$GW/healthz"; echo; }
[[ -n "$UI" ]] && { echo; echo "Open the chat UI:  https://$UI"; }
