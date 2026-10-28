# Deploying the Stack on OpenShift Local (CRC)

The README assumes you have an OpenShift cluster with OpenShift AI. You can
also run the whole stack -- MCP server(s), agent, gateway and UI -- on a
laptop with [OpenShift Local](https://developers.redhat.com/products/openshift-local/overview)
(CRC) and a hosted model endpoint. This page covers the differences and the
problems you will hit on the way.

The accompanying script, `scripts/deploy-local-cluster.sh`, runs the whole
sequence; the sections below explain what it does and why.

## What changes on a local cluster

| Concern | Full cluster | OpenShift Local |
|---|---|---|
| Model | vLLM on a GPU node | **Remote endpoint** -- CRC has no GPU, so point `MODEL_ENDPOINT` at gpt-oss-20b on vLLM elsewhere (an OpenShift AI cluster, a hosted vLLM), or at a hosted API via LiteLLM |
| Images | Registry you push to | **In-cluster binary builds** (`oc new-build --binary`); nothing is pushed from the laptop |
| CPU architecture | n/a | Irrelevant with in-cluster builds -- the build runs on the node, so an Apple Silicon laptop never produces an ARM image by accident |
| Resources | Per node pool | One node for everything: give the VM **>= 16 GB RAM and 6 CPUs** (`crc config set memory 16384`, `crc config set cpus 6`, before `crc start`) |
| `oc` | On PATH | `eval $(crc oc-env)` |

## Topology

```
Browser -> ui (Route) -> gateway (Route) -> agent (Service) -> mcp-server (Service, other namespace) -> upstream API
```

Each MCP server is deployed with the `openshift.yaml` + `deploy.sh` that
`fips-agents create mcp-server` generates, into its own namespace. The agent,
gateway and UI go into one namespace with their Helm charts. The agent reaches
each MCP server by fully qualified service DNS:

```
http://mcp-server.<mcp-namespace>.svc.cluster.local:8080/mcp/
```

## The sequence

Deploy bottom-up. Each layer's startup checks the layer below it.

```bash
export CTX=$(oc config current-context)       # pin every oc/helm call to CRC
export MODEL_ENDPOINT="https://<your-vllm>/v1" # gpt-oss-20b on vLLM, reachable from inside CRC
export MODEL_API_KEY="not-required"            # or the endpoint's token
./scripts/deploy-local-cluster.sh mcp          # 1. MCP server(s)
./scripts/deploy-local-cluster.sh agent        # 2. agent  (Secret + build + Helm)
./scripts/deploy-local-cluster.sh gateway      # 3. gateway
./scripts/deploy-local-cluster.sh ui           # 4. UI, prints the URL to open
```

### 1. MCP server

`deploy.sh <namespace>` applies `openshift.yaml` (BuildConfig, ImageStream,
Deployment, Service, Route), uploads the source as a binary build, and waits
for the rollout. Verify with the streamable-http handshake:

```bash
ROUTE=$(oc get route mcp-server -n <mcp-namespace> --context="$CTX" -o jsonpath='{.spec.host}')
curl -sk "https://$ROUTE/mcp/" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -D /tmp/h.txt \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
SESSION=$(grep -i mcp-session-id /tmp/h.txt | tr -d '\r' | awk '{print $2}')
curl -sk "https://$ROUTE/mcp/" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Responses are SSE: take the last `data:` line. A `tools/call` returns two
events (a log notification, then the result).

### 2. Agent

Three things differ from the README's `helm install`:

**The model credential goes in a Secret, not the ConfigMap.** Everything
under `config.*` lands in a ConfigMap and is visible to anyone who can read
the namespace. Create a Secret and reference it from the chart's `env` list:

```bash
oc create secret generic model-credentials --from-literal=OPENAI_API_KEY="$MODEL_API_KEY" -n <agent-ns>
helm upgrade --install my-agent chart/ \
  --set image.repository="$IMAGE" --set image.tag=latest --set image.pullPolicy=Always \
  --set config.MODEL_PROVIDER=openai \
  --set config.MODEL_ENDPOINT="$MODEL_ENDPOINT" \
  --set config.MODEL_NAME="gpt-oss-20b" \
  --set config.MCP_SERVER_URL="http://mcp-server.<mcp-namespace>.svc.cluster.local:8080/mcp/" \
  --set 'env[0].name=OPENAI_API_KEY' \
  --set 'env[0].valueFrom.secretKeyRef.name=model-credentials' \
  --set 'env[0].valueFrom.secretKeyRef.key=OPENAI_API_KEY' \
  --set route.enabled=true -n <agent-ns> --kube-context="$CTX"
```

If the vLLM endpoint uses HTTPS with a private CA (OpenShift AI's service
CA, for instance), also set
`--set config.SSL_CERT_FILE=/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt`
when the endpoint is on the same cluster, or mount the CA bundle otherwise.

For a hosted API instead, switch the three model values and the key name:
`MODEL_PROVIDER=litellm`, `MODEL_NAME=anthropic/claude-...`, and
`env[0].name=ANTHROPIC_API_KEY` (see the provider notes in
`templates/agent/agent.yaml`).

**Check the build phase before deploying.** `oc start-build --follow` does
not reliably return non-zero when the build fails, and the phase can still read
`Running` for a few seconds after the push log ends. Poll
`oc get build -l buildconfig=<name> -o jsonpath='{.items[-1:].status.phase}'`
until it is `Complete` (or `Failed`), then run Helm. Installing a chart
against an image that was never built produces a confusing
`1 old replicas are pending termination` timeout.

**Verify tool discovery, not just health.** The agent connects to its MCP
servers at startup; if a server is down the agent starts anyway, with fewer
tools. After the rollout:

```bash
ROUTE=$(oc get route my-agent -n <agent-ns> --context="$CTX" -o jsonpath='{.spec.host}')
curl -sk "https://$ROUTE/v1/agent-info" | python3 -c 'import json,sys;print([t["name"] for t in json.load(sys.stdin)["tools"]])'
oc logs deployment/my-agent -n <agent-ns> --context="$CTX" | grep "Connected to MCP"
```

If the tools are missing, fix the MCP server and `oc rollout restart` the
agent. (The route may return an HTML 503 for the first ~10 s after a rollout
while the router picks up the new endpoint -- retry before debugging.)

### 3-4. Gateway and UI

Build in-cluster (`make build-openshift PROJECT=<agent-ns>` creates the
BuildConfig from `build/buildconfig.yaml`), resolve the ImageStream, and
install the chart with the **in-cluster** URL of the layer below:

```bash
--set config.BACKEND_URL=http://my-agent:8080     # gateway -> agent
--set config.API_URL=http://my-gateway:8080       # ui -> gateway
```

Never point these at the HTTPS Routes: that terminates TLS at the router and
breaks from inside the pod even though it works from your laptop.

**Raise the route timeout.** The OpenShift router's default is 30 s; an answer
that chains two or three tool calls plus a reasoning-rubric call takes longer,
and the client sees a 504 while the agent finishes successfully. Annotate every
externally used route:

```bash
oc annotate route my-gateway haproxy.router.openshift.io/timeout=180s --overwrite -n <agent-ns>
oc annotate route my-ui      haproxy.router.openshift.io/timeout=180s --overwrite -n <agent-ns>
```

## Problems you will hit

**`Permission denied: 'requirements.txt'` inside the build.** The UBI Python
image runs `pip install` as UID 1001. Files created with mode `600` (common
when files are written by tooling rather than an editor) are unreadable by
that user, and the generated `deploy.sh` only fixes `src/*.py`. Make the
whole build context readable before uploading:

```bash
chmod -R a+rX "$BUILD_DIR"     # in deploy.sh, after assembling the context
```

or fix the source tree once: `find . -path ./.git -prune -o -type f -perm 600 -exec chmod 644 {} +`.

**`COPY skills/ skills/: no such file or directory`.** The agent Containerfile
copies `skills/` and `rules/`, but git does not keep empty directories, so a
clone may lack them. Add a `README.md` to each.

**A folder name containing `:` breaks `python -m venv`** ("Refusing to create a
venv ... because it contains the PATH separator"). Rename the folder.

**Redeploying after a code change** is `oc start-build <name> --from-dir=. --follow`
followed by `oc rollout restart deployment/<name>`; the Service keeps the same
address so the layers above need no change.

## Presenting the stack

The UI's header, tab title and settings panel show whatever
`/v1/agent-info` reports as the model name. For a demo you may want a fixed
title and a model label that describes the target deployment. Expose three
environment variables from the UI server through its `/api/config` endpoint
and read them in the page before `agent-info` loads:

| Variable | Shown |
|---|---|
| `UI_TITLE` | browser tab and header |
| `UI_MODEL_LABEL` | header subtitle and Settings > Model > Name |
| `UI_MODEL_SERVED_BY` | appended to the subtitle ("served by ..."); Settings > Model > Served by |

Make them Helm values so a redeploy of the same image can change the
presentation; leave them blank to fall back to the live model name. They are
labels only -- the model the agent calls is still set in `agent.yaml`.

## Cleaning up

```bash
oc delete project <mcp-namespace> <agent-ns> --context="$CTX"
```
