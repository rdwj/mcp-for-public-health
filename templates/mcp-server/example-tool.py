"""Example MCP tool demonstrating the enrichment pattern.

This tool returns PM2.5 air quality data from a fictional integration
with the EPA Air Quality System (AQS). The key pattern is that the tool
response includes not just the raw measurement data, but structured
metadata that feeds the agent's 6-step reasoning protocol.

Replace this with your own domain's data source and indicators.
"""

from datetime import datetime, timezone

from fastmcp.tools import tool


@tool(description="Query PM2.5 air quality data from monitoring stations.")
async def query_air_quality(
    location: str,
    start_date: str = "",
    end_date: str = "",
    query_type: str = "current",
) -> dict:
    """Retrieve PM2.5 measurements with methodology and context metadata.

    Args:
        location: City, state, county, or monitoring station ID.
        start_date: Start date for historical queries (YYYY-MM-DD).
        end_date: End date for historical queries (YYYY-MM-DD).
        query_type: One of 'current', 'trend', 'comparison'. Default: 'current'.
    """
    # -------------------------------------------------------------------
    # In a real implementation, this section would query your data source.
    # The example below shows the RESPONSE STRUCTURE your tool should return.
    # -------------------------------------------------------------------

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        # -- Raw data: the measurements the user asked for --
        "results": [
            {
                "station_id": "41-051-0080",
                "station_name": "Portland - SE Lafayette",
                "latitude": 45.4967,
                "longitude": -122.6026,
                "parameter": "PM2.5",
                "value": 8.2,
                "unit": "ug/m3",
                "aqi": 34,
                "aqi_category": "Good",
                "timestamp": "2026-05-08T14:00:00Z",
                "year": 2026,
            }
        ],

        # -- Methodology: how this data was collected --
        # survey | administrative | registry | sensor -- see README.
        "data_kind": "sensor",
        "methodology": (
            "PM2.5 measured via Federal Reference Method (FRM) gravimetric "
            "analysis at EPA AQS monitoring stations. Hourly values are "
            "instrument readings; daily values are 24-hour averages per "
            "40 CFR Part 50. AQI calculated using EPA breakpoints "
            "(revised 2024). Station siting follows 40 CFR Part 58 "
            "criteria -- monitors are placed to represent population "
            "exposure, which means industrial and rural areas may be "
            "under-represented."
        ),

        # -- Geographic context: what resolution is available and why --
        "geographic_context": {
            "resolution": "point (monitoring station)",
            "why": (
                "AQS provides measurements at specific monitor locations. "
                "Readings represent the air quality at that point, not the "
                "entire city. Interpolated regional estimates are available "
                "from AirNow but are model-derived, not direct measurements."
            ),
            "available_levels": [
                "monitoring station (point)",
                "CBSA/metro area (aggregated)",
                "county (aggregated daily)",
                "state (aggregated annual)",
            ],
            "alternatives_at_other_levels": (
                "For neighborhood-scale coverage, PurpleAir sensors provide "
                "denser spatial coverage but with lower measurement accuracy. "
                "For county-wide or regional estimates, satellite-derived AOD "
                "products provide complete spatial coverage at daily resolution."
            ),
        },

        # -- Cross-dataset context: what else covers this topic --
        "cross_dataset_context": {
            "this_dataset": "EPA AQS",
            "alternatives": [
                {
                    "name": "PurpleAir",
                    "strengths": "Dense spatial coverage, real-time, low cost",
                    "weaknesses": (
                        "Consumer-grade laser particle counters; requires EPA "
                        "correction factor; readings can be unreliable in high "
                        "humidity or wildfire smoke"
                    ),
                    "when_to_use": (
                        "Hyperlocal questions where no AQS monitor is nearby"
                    ),
                },
                {
                    "name": "Satellite AOD (e.g., MODIS/VIIRS)",
                    "strengths": "Complete spatial coverage, daily resolution",
                    "weaknesses": (
                        "Requires model calibration to convert AOD to surface "
                        "PM2.5; cloud cover causes data gaps; daily averages only"
                    ),
                    "when_to_use": (
                        "Regional or rural coverage where no ground monitors exist"
                    ),
                },
            ],
        },

        # -- Supported and unsupported conclusions --
        "supported_conclusions": [
            "Current and historical PM2.5 levels at monitored locations",
            "AQI category and health advisory level",
            "Trends over time at specific stations",
            "Comparisons between monitored locations",
        ],
        "unsupported_conclusions": [
            "Causal attribution to specific emission sources",
            "Health effects on individuals (requires epidemiological studies)",
            "Air quality at locations between monitors (requires modeling)",
            "Future air quality predictions",
        ],

        # -- Terminology mapping --
        "terminology_note": (
            "'air quality' was mapped to PM2.5 (particulate matter "
            "2.5 micrometers and smaller). AQS also monitors O3, NO2, SO2, "
            "CO, and Pb at this location. Specify the parameter name to "
            "query other pollutants."
        ),

        # -- Data freshness --
        "data_freshness": {
            "dataset_name": "EPA Air Quality System (AQS)",
            "dataset_url": "https://aqs.epa.gov/aqsweb/airdata/download_files.html",
            "dataset_updated": now,
            "data_year": "2026",
        },

        # -- Citation --
        "citation": {
            "source": "EPA Air Quality System (AQS)",
            "url": "https://aqs.epa.gov/aqsweb/airdata/download_files.html",
        },

        # -- Caveats (display verbatim in agent response) --
        "caveats": [
            "Hourly readings are preliminary and subject to quality assurance review.",
            "Station PM2.5 readings represent the air quality at the monitor location "
            "and may not reflect conditions at other nearby locations.",
        ],
    }
