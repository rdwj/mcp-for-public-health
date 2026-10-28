# MCP Server Template

Start by scaffolding your server:

```bash
fips-agents create mcp-server your-domain-server
```

Then add tools following the **enrichment pattern** shown in `example-tool.py`.

## The Enrichment Pattern

The key design principle: every tool response should include structured metadata alongside the raw data. This metadata feeds the agent's 6-step reasoning protocol and makes the agent's reasoning auditable.

Every tool response should include these fields:

- **results**: The actual data (measurements, statistics, records)
- **data_kind**: What sort of data this is -- `survey` (sampled estimates with confidence intervals), `administrative` (records of transactions or awards, e.g. a grants database), `registry` (enumerated cases), or `sensor` (instrument readings). The agent uses this to judge confidence: survey data is only fully trustworthy with its methodology; administrative and registry records are authoritative on their own terms but answer different questions (a grant's existence says nothing about its findings)
- **methodology**: How the data was collected -- sampling design, measurement instruments, known biases, population coverage
- **geographic_context**: What geographic resolution is available, why (sampling design, sensor placement), and what alternatives exist at other levels
- **cross_dataset_context**: What other data sources cover this topic, how they differ, and when each is more appropriate
- **supported_conclusions**: What the data CAN tell you (levels, trends, comparisons at the available resolution)
- **unsupported_conclusions**: What the data CANNOT tell you (causal claims, health effects, predictions, data at unavailable resolutions)
- **terminology_note**: How the user's query terms were mapped to domain-specific indicator names
- **data_freshness**: Dataset name, source URL, data year, last update date
- **citation**: Proper attribution for the data source
- **caveats**: Any data quality warnings, suppression notes, or known issues

## Why Enrichment Matters

Without enrichment, the agent receives raw numbers and must either fabricate context (unreliable) or present data without methodology context (dangerous). With enrichment, every piece of context the agent needs is right there in the tool response -- the agent just needs to surface it in its reasoning.

This pattern also makes the agent's reasoning **auditable**: a reviewer can check the tool response metadata against the agent's reasoning block to verify that the agent actually engaged with the methodology rather than making things up.

## Example

See `example-tool.py` for a fully annotated tool that demonstrates the enrichment pattern using a fictional air quality monitoring scenario.
