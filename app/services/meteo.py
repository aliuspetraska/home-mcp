"""meteo.lt — Lithuanian Hydrometeorological Service public API.

Free, no auth. Docs: https://api.meteo.lt/
"""

import httpx
from fastmcp import FastMCP

from app.config import Settings


class MeteoClient:
    def __init__(self, settings: Settings) -> None:
        self._http = httpx.Client(
            base_url=settings.meteo_base_url,
            timeout=settings.http_timeout_seconds,
            headers={"Accept": "application/json"},
        )

    def places(self) -> list[dict]:
        return self._http.get("/places").json()

    def forecast(self, place_code: str) -> dict:
        resp = self._http.get(f"/places/{place_code}/forecasts/long-term")
        resp.raise_for_status()
        return resp.json()


def register(mcp: FastMCP, settings: Settings) -> None:
    client = MeteoClient(settings)

    @mcp.tool(tags={"weather", "meteo"})
    def meteo_find_places(search: str = "") -> list[dict]:
        """Search meteo.lt place codes. Pass a Lithuanian city name (e.g. 'vilnius')
        or empty string to list all places. Returns code + name for use with
        meteo_forecast."""
        places = client.places()
        if search:
            needle = search.casefold()
            places = [
                p
                for p in places
                if needle in p.get("name", "").casefold()
                or needle in p.get("code", "").casefold()
            ]
        return [
            {
                "code": p.get("code"),
                "name": p.get("name"),
                "administrativeDivision": p.get("administrativeDivision"),
            }
            for p in places
        ]

    @mcp.tool(tags={"weather", "meteo"})
    def meteo_forecast(place_code: str) -> dict:
        """Get long-term weather forecast for a meteo.lt place code
        (e.g. 'vilnius', 'kaunas'). Use meteo_find_places to resolve names."""
        data = client.forecast(place_code)
        return {
            "place": data.get("place"),
            "forecastCreationTimeUtc": data.get("forecastCreationTimeUtc"),
            "forecastTimestamps": data.get("forecastTimestamps", []),
        }
