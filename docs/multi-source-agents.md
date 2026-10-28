# Agents That Span Several Data Sources

The templates assume one MCP server. In practice a public health agent soon
needs two or three: a surveillance source (prevalence, trends), a funding or
administrative source (who is studying what), a registry. This page is the
pattern for wiring several servers into one agent without the model losing
track of which tool answers which question.

## Configuration

List every server under `mcp_servers:` in `agent.yaml`. Tools from all
servers land in one registry, so **tool names must be unique across servers**
-- prefix them by source (`cdc_query`, `nih_search_projects`). The agent
connects to each at startup and logs `Connected to MCP server ... -- N tool(s)`
per server; if one is down the agent starts without its tools, so check
`/v1/agent-info` after deploying, not just `/healthz`.

```yaml
mcp_servers:
  - url: ${MCP_SURVEILLANCE_URL:-http://mcp-server.surveillance.svc.cluster.local:8080/mcp/}
  - url: ${MCP_FUNDING_URL:-http://mcp-server.funding.svc.cluster.local:8080/mcp/}
```

Give each server its own `data_kind` in tool responses (see
`templates/mcp-server/README.md`): the confidence card treats survey
estimates and administrative records differently.

## Prompting: tool families, not tool lists

The single-source prompt describes three tools. With several sources, the
model needs a **routing rule** more than it needs descriptions -- the tool
schemas carry the parameters. Structure the "Your Tools" section as families
with a one-line rule for choosing between them:

```markdown
## Your Tools

Pick the tool family that matches the question:
- "how common is X / is it rising / where is it worst"  -> surveillance tools
- "who is funded to study X / how much / what did it produce" -> funding tools
Some questions legitimately need both; answer each part from its own source
and say which is which.

### Surveillance -- `cdc_query`, `cdc_describe_datasets`, `cdc_get_methodology`
...one paragraph per family, not per tool...

### Funding -- `nih_search_projects`, `nih_get_project`, `nih_find_publications`
...parameter hints that the schema cannot express (fiscal-year conventions,
name formats, institute abbreviations)...
```

Keep the reasoning protocol the same for every family, and extend the two
steps that are source-specific:

- **Cross-dataset reasoning** -- for an administrative source, "which dataset"
  means which tool and which filters (why institute X rather than free text).
- **Methodology awareness** -- administrative data is not a survey. Say so:
  amounts are per fiscal year, search matches titles and abstracts, links to
  publications depend on acknowledgement. **Funding is not findings.**

Put the source-specific lines for the freshness block next to the generic
ones ("for funding answers: Source: NIH RePORTER, Fiscal Year(s): ...") so
the model has a template for each.

## Agent code

The tool-call insistor's message should name families, not individual tools,
or the model will call a tool from the wrong family just to satisfy it:

```python
_TOOL_INSIST_MSG = (
    "... call at least one tool (a surveillance tool for health statistics, "
    "or a funding tool for grants) to retrieve real data before answering."
)
```

The confidence card needs no per-source code if every tool declares
`data_kind`. A response with `results`/`records` and
`data_kind: administrative` scores HIGH on an "authoritative record"
basis; survey data still needs `methodology` to reach HIGH.

## Cross-source questions

"How prevalent is diabetes in Ohio, and what NIH diabetes research is funded
there?" should produce one call per source and an answer with two clearly
separated parts. Two things make this work reliably:

1. The routing rule above explicitly allows "both".
2. The reasoning rubric's `cross_dataset` entry is never N/A, so the model is
   forced to say which sources it used for which part.

Test with a question from each family and one that needs both; check the
tool trace in the UI, not just the prose.

## Deploying

Each MCP server is its own namespace and Deployment; only the agent's
ConfigMap changes when you add a source (`MCP_*_URL`). Deploy the new server
first, then `oc rollout restart` the agent so it rediscovers tools. See
`docs/deploying-on-openshift-local.md`.
