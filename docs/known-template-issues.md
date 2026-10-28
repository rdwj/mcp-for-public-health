# Known Issues with the fips-agents Templates

The templates this project builds on move quickly, and projects scaffolded a
few months apart can drift from the framework they install. These are the
mismatches we have hit and how to deal with them. Where an issue belongs
upstream, it is noted; this page exists so you lose minutes rather than hours
in the meantime.

Versions observed: `fipsagents` 0.32, `fastmcp` 4.x, `fips-agents-cli` 0.17,
agent template 0.12-0.17, MCP template 0.1.7-0.8.

## Agent template

**`provider: anthropic` is routed to a sidecar that does not exist.**
In `fipsagents` 0.32 the framework still treats `anthropic` (with `bedrock`
and `azure`) as an "off-platform" provider and rewrites the endpoint to
`http://localhost:8081/v1`, the old adapter sidecar, regardless of what you
configured. Use `provider: litellm` with a prefixed model name
(`anthropic/claude-...`) instead; calls then go directly to the Anthropic API
with `ANTHROPIC_API_KEY`. The same applies to any provider LiteLLM supports.

**Reasoning models reject the template's `temperature: 0.3`.** Models with
built-in reasoning may accept only one temperature (Claude Fable: `1`);
LiteLLM raises `UnsupportedParamsError` before the request is sent. Set the
temperature in `agent.yaml`'s `model:` block -- the `temperature` in the
prompt file's front matter is informational and is not what the model
receives.

**Private framework attributes are not stable.** Older projects patched
`self.llm._acompletion` / `_base_kwargs` (for example, to force
`stream=False` around a vLLM streaming bug). Those attributes were removed
when provider backends were introduced; the patch raises `AttributeError` in
`setup()` and the pod crash-loops. Before upgrading `fipsagents`, grep your
agent for `self.llm._`.

**`COPY skills/ skills/` fails in the container build.** The Containerfile
copies `skills/` and `rules/` but git does not track empty directories.
Add a `README.md` to each so clones have them.

**Framework unit tests shipped inside the project.** Template 0.12 copied the
framework's own tests (`tests/test_llm.py`, `test_agent.py`, `test_tools.py`,
`test_rules.py`) into the project. They test framework internals, fail after
a framework upgrade, and test nothing about your agent. Delete them; newer
templates do not include them.

**Prompt layers.** Newer templates assemble the system prompt from
`identity.md` + `personality.md` + `rules/` + `skills/` when
`prompt_assembly:` is present in `agent.yaml`. Editing only
`prompts/system.md` then changes nothing the model sees -- edit `identity.md`
too, or remove the `prompt_assembly:` block.

## MCP server template

**`@tool` no longer returns a `FunctionTool`.** FastMCP 2.x decorators
returned an object with `.fn`; FastMCP 3+ returns the plain function. Tests
written as `my_tool.fn(...)` fail at import. Portable form:

```python
my_tool_fn = getattr(my_tool, "fn", my_tool)
```

**`remove_examples.sh` leaves the example tests behind.** It deletes
`src/*/examples/` but not `tests/test_server_e2e.py`, which still asserts
that `echo` and `analyze_text` exist. Delete or rewrite that file after
removing examples; the HTTP transport itself is fine.

**`httpx` is missing from `requirements.txt`** in some template versions even
though `tests/test_auth_pipeline.py` imports it. Add `httpx>=0.27`.

**`deploy.sh` only fixes permissions on `src/*.py`.** The in-cluster build
runs `pip install` as UID 1001; a `requirements.txt` (or `pyproject.toml`)
with mode 600 fails with `Permission denied`. Make the whole build context
readable:

```bash
chmod -R a+rX "$BUILD_DIR"
```

**Streamable-http responses are SSE, and `tools/call` returns two events**
(a log notification from `ctx.info`, then the result). When scripting against
the server, parse the *last* `data:` line.

## Gateway / UI templates

**`go.mod` says `go 1.22`, the Containerfile uses `ubi9/go-toolset:1.22`.**
Consistent today; if you bump one, bump the other or the in-cluster build
fails on `go.mod requires go >= ...`.

**`oc start-build --follow` exit status.** Does not reliably reflect a failed
build, and the build phase can still read `Running` for a few seconds after
the log ends. Poll the phase before deploying (see
`scripts/deploy-local-cluster.sh`).
