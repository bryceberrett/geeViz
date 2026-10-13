"""
Google Maps Platform client for geeViz.

Provides functions for ground-truthing and enriching remote sensing
analysis using Google Maps Platform APIs:

- **Geocoding** — address to coordinates and reverse
- **Places** — search, nearby, details, photos
- **Street View** — static images, panoramas, AI interpretation
- **Elevation** — terrain height at any location
- **Static Maps** — basemap images for reports
- **Air Quality** — current AQI and pollutants
- **Solar** — rooftop solar potential
- **Roads** — snap GPS traces to nearest roads

**24 public functions:**

- **Geocoding**: ``geocode``, ``reverse_geocode``, ``validate_address``
- **Places**: ``search_places``, ``search_nearby``, ``get_place_photo``
- **Street View**: ``streetview_metadata``, ``streetview_image``,
  ``streetview_images_cardinal``, ``streetview_panorama``, ``streetview_html``
- **AI Analysis**: ``interpret_image``, ``label_streetview``,
  ``segment_image``, ``segment_streetview``
- **Elevation**: ``get_elevation``, ``get_elevations``,
  ``get_elevation_along_path``
- **Environment**: ``get_air_quality``, ``get_solar_insights``,
  ``get_timezone``
- **Maps**: ``get_static_map``
- **Roads**: ``snap_to_roads``, ``nearest_roads``

Quick start::

    import geeViz.googleMapsLib as gm

    # Geocode an address
    result = gm.geocode("100 S 200 E, Salt Lake City, UT")

    # Street View panorama + AI interpretation
    pano = gm.streetview_panorama(-111.80, 40.68, fov=360)
    analysis = gm.interpret_image(pano)

    # Semantic segmentation (SegFormer)
    seg = gm.segment_image(pano, model_variant="b4")

    # Elevation, air quality, solar
    elev = gm.get_elevation(-111.80, 40.68)
    aq = gm.get_air_quality(-111.80, 40.68)
    solar = gm.get_solar_insights(-111.80, 40.68)

Requires a ``GOOGLE_MAPS_PLATFORM_API_KEY`` in your environment or ``.env``
file. Gemini AI features use ``GEMINI_API_KEY``.

Copyright 2026 Ian Housman

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from geeViz._ssrf import check_url as _check_url  # noqa: E402

# LLM usage reporting. Imported at module scope on purpose: a
# call-time import of this from inside a function deadlocked the MCP
# server, whose prewarm thread holds the geeViz import lock while a
# tool call is being served. llmUsage is stdlib-only and imports
# nothing from geeViz, so this is free and cannot cycle.
from geeViz import llmUsage as _lu

# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------

_API_KEY: str | None = None

# Key names to check, in priority order
_KEY_NAMES = (
    "GOOGLE_MAPS_PLATFORM_API_KEY",
    "MAPS_PLATFORM_API_KEY",
    "GOOGLE_API_KEY",
)


def _get_api_key() -> str:
    """Resolve the Google Maps Platform API key.

    Checks environment variables and ``.env`` in priority order:
    ``MAPS_PLATFORM_API_KEY``, ``GOOGLE_API_KEY``.
    """
    global _API_KEY
    if _API_KEY:
        return _API_KEY

    # Parse .env file first (env vars may have a different project's key).
    # The MCP sandbox blocks open() on .env paths, so tolerate PermissionError
    # here: the MCP server pre-loads .env into os.environ at startup, so the
    # fall-through to os.environ below still finds the key.
    env_keys: dict[str, str] = {}
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        env_keys[k.strip()] = v.strip().strip("'\"")
    except (PermissionError, OSError):
        pass

    # Check each key name in priority order across both sources
    for key_name in _KEY_NAMES:
        for source in (env_keys, os.environ):
            key = source.get(key_name)
            if key:
                _API_KEY = key
                return key

    raise RuntimeError(
        "No Google Maps API key found. Set GOOGLE_MAPS_PLATFORM_API_KEY "
        "in your environment or .env file."
    )


def _fetch_json(url: str, params: dict | None = None,
                method: str = "GET", body: dict | None = None,
                headers: dict | None = None) -> dict:
    """HTTP request returning parsed JSON."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body else None
    hdrs = {"User-Agent": "geeViz/googleMaps"}
    if headers:
        hdrs.update(headers)
    if data and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"
    _check_url(url)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_bytes(url: str, params: dict | None = None) -> bytes:
    """HTTP GET returning raw bytes."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "geeViz/googleMaps"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


###########################################################################
#  Geocoding API
###########################################################################

_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


def geocode(address: str) -> dict[str, Any] | None:
    """Geocode an address to coordinates using the Google Geocoding API.

    Args:
        address (str): Street address, place name, or location description.

    Returns:
        dict or None: Result with keys:

        - ``lat`` (float): Latitude.
        - ``lon`` (float): Longitude.
        - ``formatted_address`` (str): Full formatted address.
        - ``place_id`` (str): Google Place ID.
        - ``location_type`` (str): Accuracy — ``"ROOFTOP"``,
          ``"RANGE_INTERPOLATED"``, ``"GEOMETRIC_CENTER"``, or
          ``"APPROXIMATE"``.
        - ``address_components`` (list): Decomposed address parts.

        Returns ``None`` if no results found.

    Example:
        >>> result = geocode("100 S 200 E, Salt Lake City, UT")
        >>> if result:
        ...     print(f"{result['lat']}, {result['lon']}")
    """
    data = _fetch_json(_GEOCODE_URL, {
        "address": address,
        "key": _get_api_key(),
    })
    if data.get("status") != "OK" or not data.get("results"):
        return None
    r = data["results"][0]
    loc = r["geometry"]["location"]
    return {
        "lat": loc["lat"],
        "lon": loc["lng"],
        "formatted_address": r.get("formatted_address", ""),
        "place_id": r.get("place_id", ""),
        "location_type": r["geometry"].get("location_type", ""),
        "address_components": r.get("address_components", []),
    }


###########################################################################
#  Places API (New)
###########################################################################

_PLACES_BASE = "https://places.googleapis.com/v1"


def search_places(
    query: str,
    lat: float | None = None,
    lon: float | None = None,
    radius: float = 5000,
    max_results: int = 10,
    included_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search for places using the Google Places API (New) Text Search.

    Args:
        query (str): Search text (e.g. "coffee shops", "gas station",
            "Yellowstone visitor center").
        lat (float, optional): Latitude for location bias.
        lon (float, optional): Longitude for location bias.
        radius (float, optional): Bias radius in meters. Defaults to 5000.
        max_results (int, optional): Maximum results (1-20). Defaults to 10.
        included_types (list, optional): Place type filters (e.g.
            ``["restaurant"]``, ``["gas_station"]``).

    Returns:
        list of dict: Each dict has keys: ``name``, ``display_name``,
        ``address``, ``lat``, ``lon``, ``types``, ``rating``,
        ``place_id``, ``photo_name`` (first photo resource name, if any).

    Example:
        >>> places = search_places("fire station", lat=40.76, lon=-111.89)
        >>> for p in places:
        ...     print(f"{p['display_name']}: {p['address']}")
    """
    body: dict[str, Any] = {
        "textQuery": query,
        "pageSize": min(max_results, 20),
        "languageCode": "en",
    }
    if lat is not None and lon is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": radius,
            }
        }
    if included_types:
        body["includedType"] = included_types[0]  # API accepts one type

    field_mask = (
        "places.id,places.displayName,places.formattedAddress,"
        "places.location,places.types,places.rating,"
        "places.userRatingCount,places.photos"
    )

    data = _fetch_json(
        f"{_PLACES_BASE}/places:searchText",
        method="POST",
        body=body,
        headers={
            "X-Goog-Api-Key": _get_api_key(),
            "X-Goog-FieldMask": field_mask,
        },
    )

    results = []
    for p in data.get("places", []):
        loc = p.get("location", {})
        photos = p.get("photos", [])
        results.append({
            "name": p.get("id", ""),
            "display_name": p.get("displayName", {}).get("text", ""),
            "address": p.get("formattedAddress", ""),
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
            "types": p.get("types", []),
            "rating": p.get("rating"),
            "rating_count": p.get("userRatingCount"),
            "place_id": p.get("id", ""),
            "photo_name": photos[0].get("name") if photos else None,
        })
    return results


def search_nearby(
    lat: float,
    lon: float,
    radius: float = 1000,
    included_types: list[str] | None = None,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Search for places near a location using Nearby Search (New).

    Args:
        lat (float): Latitude.
        lon (float): Longitude.
        radius (float, optional): Search radius in meters (max 50000).
            Defaults to 1000.
        included_types (list, optional): Place type filters (e.g.
            ``["restaurant"]``).
        max_results (int, optional): Maximum results (1-20). Defaults to 10.

    Returns:
        list of dict: Same format as :func:`search_places`.

    Example:
        >>> nearby = search_nearby(40.76, -111.89, radius=2000,
        ...     included_types=["park"])
    """
    body: dict[str, Any] = {
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": min(radius, 50000),
            }
        },
        "maxResultCount": min(max_results, 20),
        "languageCode": "en",
    }
    if included_types:
        body["includedTypes"] = included_types

    field_mask = (
        "places.id,places.displayName,places.formattedAddress,"
        "places.location,places.types,places.rating,"
        "places.userRatingCount,places.photos"
    )

    data = _fetch_json(
        f"{_PLACES_BASE}/places:searchNearby",
        method="POST",
        body=body,
        headers={
            "X-Goog-Api-Key": _get_api_key(),
            "X-Goog-FieldMask": field_mask,
        },
    )

    results = []
    for p in data.get("places", []):
        loc = p.get("location", {})
        photos = p.get("photos", [])
        results.append({
            "name": p.get("id", ""),
            "display_name": p.get("displayName", {}).get("text", ""),
            "address": p.get("formattedAddress", ""),
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
            "types": p.get("types", []),
            "rating": p.get("rating"),
            "rating_count": p.get("userRatingCount"),
            "place_id": p.get("id", ""),
            "photo_name": photos[0].get("name") if photos else None,
        })
    return results


def get_place_photo(photo_name: str, max_width: int = 400,
                    max_height: int = 400) -> bytes | None:
    """Fetch a place photo by its resource name.

    Photo names come from :func:`search_places` or :func:`search_nearby`
    results (the ``photo_name`` field).

    Args:
        photo_name (str): Photo resource name from a Places API response.
        max_width (int, optional): Maximum width in pixels (1-4800).
        max_height (int, optional): Maximum height in pixels (1-4800).

    Returns:
        bytes or None: JPEG/PNG image bytes, or ``None`` on error.

    Example:
        >>> places = search_places("Arches National Park visitor center")
        >>> if places and places[0]['photo_name']:
        ...     photo = get_place_photo(places[0]['photo_name'])
    """
    if not photo_name:
        return None
    try:
        return _fetch_bytes(
            f"{_PLACES_BASE}/{photo_name}/media",
            {"key": _get_api_key(),
             "maxWidthPx": str(max_width),
             "maxHeightPx": str(max_height)},
        )
    except Exception:
        return None


###########################################################################
#  Street View Static API
###########################################################################

_SV_STATIC_URL = "https://maps.googleapis.com/maps/api/streetview"
_SV_METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
_SV_DEFAULT_SIZE = "640x480"
_SV_DEFAULT_FOV = 90


def streetview_metadata(
    lon: float,
    lat: float,
    radius: int = 50,
    source: str = "default",
) -> dict[str, Any]:
    """Check if Street View imagery exists at a location.

    This is a free call (no quota consumed).

    Args:
        lon (float): Longitude in decimal degrees.
        lat (float): Latitude in decimal degrees.
        radius (int, optional): Search radius in meters. Defaults to 50.
        source (str, optional): ``"default"`` or ``"outdoor"``.

    Returns:
        dict: Keys: ``status``, ``pano_id``, ``location``, ``date``,
        ``copyright``.

    Example:
        >>> meta = streetview_metadata(-111.89, 40.76)
        >>> if meta['status'] == 'OK':
        ...     print(f"Imagery from {meta['date']}")
    """
    _check_street_view_enabled("streetview_metadata")
    return _fetch_json(_SV_METADATA_URL, {
        "location": f"{lat},{lon}",
        "radius": str(radius),
        "source": source,
        "key": _get_api_key(),
    })


def streetview_image(
    lon: float,
    lat: float,
    heading: float = 0,
    pitch: float = 0,
    fov: float = _SV_DEFAULT_FOV,
    size: str = _SV_DEFAULT_SIZE,
    radius: int = 50,
    source: str = "default",
) -> bytes | None:
    """Fetch a Street View static image as JPEG bytes.

    Returns ``None`` if no imagery exists (checks metadata first).

    Args:
        lon (float): Longitude.
        lat (float): Latitude.
        heading (float, optional): Compass heading (0=N, 90=E, 180=S, 270=W).
        pitch (float, optional): Camera pitch (positive=up).
        fov (float, optional): Field of view (1-120). Defaults to 90.
        size (str, optional): Image size. Defaults to ``"640x480"``.
        radius (int, optional): Search radius. Defaults to 50.
        source (str, optional): ``"default"`` or ``"outdoor"``.

    Returns:
        bytes or None: JPEG image bytes.
    """
    _check_street_view_enabled("streetview_image")
    meta = streetview_metadata(lon, lat, radius=radius, source=source)
    if meta.get("status") != "OK":
        return None
    try:
        return _fetch_bytes(_SV_STATIC_URL, {
            "location": f"{lat},{lon}",
            "size": size,
            "heading": str(heading),
            "pitch": str(pitch),
            "fov": str(fov),
            "radius": str(radius),
            "source": source,
            "return_error_code": "true",
            "key": _get_api_key(),
        })
    except urllib.error.HTTPError:
        return None


def streetview_images_cardinal(
    lon: float,
    lat: float,
    pitch: float = 0,
    fov: float = _SV_DEFAULT_FOV,
    size: str = _SV_DEFAULT_SIZE,
    radius: int = 50,
    source: str = "default",
) -> dict[str, bytes] | None:
    """Fetch Street View images looking N, E, S, and W.

    Returns ``None`` if no imagery exists.

    Args:
        lon, lat, pitch, fov, size, radius, source: See :func:`streetview_image`.

    Returns:
        dict or None: ``{"N": bytes, "E": bytes, "S": bytes, "W": bytes}``.
    """
    _check_street_view_enabled("streetview_images_cardinal")
    meta = streetview_metadata(lon, lat, radius=radius, source=source)
    if meta.get("status") != "OK":
        return None
    results = {}
    for label, heading in {"N": 0, "E": 90, "S": 180, "W": 270}.items():
        img = streetview_image(lon, lat, heading=heading, pitch=pitch, fov=fov,
                               size=size, radius=radius, source=source)
        if img:
            results[label] = img
    return results if results else None


def streetview_panorama(
    lon: float,
    lat: float,
    heading: float = 0,
    fov: float = 360,
    pitch: float = 0,
    size: str = _SV_DEFAULT_SIZE,
    radius: int = 50,
    source: str = "default",
) -> bytes | None:
    """Fetch a wide-angle or full 360° Street View panorama as a stitched image.

    The Google Street View Static API caps FOV at 120°.  This function
    automatically splits wider requests into multiple 120° frames and
    stitches them horizontally using PIL.

    Args:
        lon (float): Longitude.
        lat (float): Latitude.
        heading (float, optional): Center compass heading of the panorama
            (0=North). The panorama spans ``heading - fov/2`` to
            ``heading + fov/2``.  Defaults to ``0``.
        fov (float, optional): Total horizontal field of view in degrees
            (1–360).  Values ≤ 120 are handled in a single frame.
            Defaults to ``360``.
        pitch (float, optional): Camera pitch. Defaults to ``0``.
        size (str, optional): Per-frame size as ``"WxH"``.
            Defaults to ``"640x480"``.
        radius (int, optional): Search radius. Defaults to ``50``.
        source (str, optional): ``"default"`` or ``"outdoor"``.

    Returns:
        bytes or None: JPEG bytes of the stitched panorama, or ``None``
        if no imagery exists.

    Example:
        >>> pano = streetview_panorama(-111.80, 40.68, heading=0, fov=360)
        >>> if pano:
        ...     with open("panorama_360.jpg", "wb") as f:
        ...         f.write(pano)
    """
    _check_street_view_enabled("streetview_panorama")
    from PIL import Image
    import io as _io

    meta = streetview_metadata(lon, lat, radius=radius, source=source)
    if meta.get("status") != "OK":
        return None

    fov = max(1, min(fov, 360))

    # Single frame if within API limit
    if fov <= 120:
        return streetview_image(lon, lat, heading=heading, pitch=pitch,
                                fov=fov, size=size, radius=radius, source=source)

    # Multiple frames: split into chunks ≤120°, fetch in parallel.
    # Each frame's FOV = step size so the frames tile exactly.
    # When pitch ≠ 0, we alpha-blend a thin seam zone to smooth
    # exposure differences between adjacent frames.
    import concurrent.futures
    import numpy as np

    _MAX_FRAME_FOV = 120
    n_frames = max(2, -(-int(fov) // _MAX_FRAME_FOV))  # ceil division
    frame_fov = fov / n_frames  # per-frame FOV = angular step
    start_heading = (heading - fov / 2 + frame_fov / 2) % 360
    headings = [(start_heading + i * frame_fov) % 360 for i in range(n_frames)]

    def _fetch_frame(h):
        """Fetch a single frame (metadata already verified)."""
        try:
            return _fetch_bytes(_SV_STATIC_URL, {
                "location": f"{lat},{lon}",
                "size": size,
                "heading": str(h),
                "pitch": str(pitch),
                "fov": str(frame_fov),
                "radius": str(radius),
                "source": source,
                "return_error_code": "true",
                "key": _get_api_key(),
            })
        except Exception:
            return None

    # Fetch all frames simultaneously
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_frames) as pool:
        raw_frames = list(pool.map(_fetch_frame, headings))

    frames = []
    for img_bytes in raw_frames:
        if img_bytes:
            frames.append(Image.open(_io.BytesIO(img_bytes)).convert("RGB"))

    if not frames:
        return None

    # Blend width: proportional to |pitch|, 0 at pitch=0
    # At pitch=30 ~8% of frame width, at pitch=45 ~12%
    blend_px = int(frames[0].size[0] * min(0.15, abs(pitch) / 300.0)) if abs(pitch) > 5 else 0

    fw, fh = frames[0].size
    total_w = fw * len(frames)
    pano = Image.new("RGB", (total_w, fh))

    # Place first frame
    pano.paste(frames[0], (0, 0))

    for i in range(1, len(frames)):
        x = fw * i
        curr = frames[i]

        if blend_px > 0:
            # Alpha-blend a thin strip at the left seam of this frame
            left_arr = np.array(pano.crop((x - blend_px, 0, x, fh))).astype(np.float32)
            right_arr = np.array(curr.crop((0, 0, blend_px, fh))).astype(np.float32)
            alpha = np.linspace(1, 0, blend_px).reshape(1, -1, 1)
            blended = (left_arr * alpha + right_arr * (1 - alpha)).astype(np.uint8)
            pano.paste(Image.fromarray(blended), (x - blend_px, 0))
            # Paste remainder of frame after blend zone
            pano.paste(curr.crop((blend_px, 0, curr.size[0], fh)), (x, 0))
        else:
            pano.paste(curr, (x, 0))

    buf = _io.BytesIO()
    pano.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# Prompt registry keyed by ``mode`` for :func:`interpret_image`.
# Each entry is the full analysis prompt for that view type. Add new
# modes here — no other code change needed.
_INTERPRET_PROMPTS: dict[str, str] = {
    "streetview": (
        "This is a Google Street View image (ground-level, human-eye perspective). "
        "Analyze it thoroughly.\n\n"
        "1. **Description**: Describe the scene in 2-3 sentences — the setting, "
        "land use, vegetation, infrastructure, and any notable features.\n\n"
        "2. **Object Inventory**: List every distinct object or feature you can "
        "identify with a count. Format as a markdown table with columns: "
        "| Object | Count | Notes |\n"
        "Include items like: buildings, houses, vehicles, trees, signs, driveways, "
        "fences, utility poles, sidewalks, mailboxes, etc. Be specific "
        "(e.g. 'brick ranch house' not just 'building').\n\n"
        "3. **Land Cover Assessment**: Estimate the approximate percentage of the "
        "visible area that is: impervious surface (road, driveway, roof), "
        "vegetation (lawn, trees), bare soil, sky."
    ),
    "satellite-map": (
        "This is a Google satellite image — a top-down (nadir) aerial view. Everything "
        "is seen from directly above; there is no ground-level perspective. Analyze it "
        "thoroughly.\n\n"
        "1. **Description**: Describe the scene in 2-3 sentences — the dominant land "
        "use (residential / commercial / agricultural / forest / water), road network "
        "pattern, and any large features visible from above.\n\n"
        "2. **Object Inventory**: List distinct features visible from above with a "
        "count. Format as a markdown table with columns: | Object | Count | Notes |\n"
        "Focus on roof-visible items: rooftops (by roughly-inferred building type — "
        "house, warehouse, barn), swimming pools, driveways, parking lots, road "
        "segments, cul-de-sacs, individual tree crowns (or grouped canopy patches), "
        "cropland fields, water bodies. Do NOT try to identify small ground-level "
        "objects (people, mailboxes, signs) — they aren't reliably resolvable at "
        "this scale.\n\n"
        "3. **Land Cover Assessment**: Estimate the approximate percentage of the "
        "visible area that is: impervious surface (roofs, roads, parking), tree "
        "canopy, grass/low vegetation, bare soil/agriculture, water."
    ),
    "hybrid-map": (
        "This is a Google hybrid map — a satellite (nadir) aerial view with road "
        "and place labels overlaid on top. Analyze it thoroughly.\n\n"
        "1. **Description**: Describe the scene in 2-3 sentences — the dominant land "
        "use, road network, and any labeled places (neighborhoods, businesses, "
        "landmarks) visible in the label overlay.\n\n"
        "2. **Object Inventory**: List distinct features with counts, treating the "
        "label overlay as an additional layer of information (not a physical object). "
        "Format as a markdown table with columns: | Object | Count | Notes |\n"
        "Include: rooftops, road segments, parking lots, tree canopy patches, water. "
        "Also include a row for each LABELED place name visible on the map with "
        "Count=1 and the label text in Notes.\n\n"
        "3. **Land Cover Assessment**: Estimate the approximate percentage of the "
        "visible area (physical, not counting labels) that is: impervious surface, "
        "tree canopy, grass/low vegetation, bare soil/agriculture, water."
    ),
    "roadmap": (
        "This is a Google roadmap — a cartographic street map (top-down, no imagery). "
        "It shows roads, place labels, and land-use tinting rather than real pixels. "
        "Analyze it as a MAP, not a photograph.\n\n"
        "1. **Description**: Describe the mapped area in 2-3 sentences — the dominant "
        "land use (from the tinting: park green, water blue, built-up gray/tan), the "
        "road hierarchy (highways vs. arterials vs. local streets), and any labeled "
        "places.\n\n"
        "2. **Feature Inventory**: List distinct MAP features with counts. Format as "
        "a markdown table with columns: | Feature | Count | Notes |\n"
        "Include: labeled highways, labeled arterials, labeled local streets, "
        "intersections, cul-de-sacs, labeled parks, labeled water bodies, labeled "
        "businesses or POIs, and any labeled neighborhood/district names.\n\n"
        "3. **Coverage Assessment**: Estimate the approximate percentage of the map "
        "area shown as: road right-of-way, park/green space, water, built-up "
        "(residential/commercial), other."
    ),
    "terrain": (
        "This is a Google terrain map — a topographic view (top-down) that shows "
        "elevation via shaded relief and contour tinting, plus major roads and place "
        "labels. Analyze it as a topographic map, not a photograph.\n\n"
        "1. **Description**: Describe the terrain in 2-3 sentences — the dominant "
        "landforms (mountain, valley, plateau, plain), the relief (steep vs. gentle), "
        "and any drainage features (rivers, lakes) visible.\n\n"
        "2. **Feature Inventory**: List distinct topographic and cultural features "
        "with counts. Format as a markdown table with columns: "
        "| Feature | Count | Notes |\n"
        "Include: named peaks or ridges, named valleys or drainages, rivers, lakes, "
        "labeled roads, labeled populated places.\n\n"
        "3. **Relief Assessment**: Roughly estimate the relative elevation range "
        "shown (low / moderate / high relief) and describe the dominant slope "
        "orientation if apparent (north-facing, south-facing, mixed, flat)."
    ),
}


# Per-mode detection body prompts for :func:`label_image`. The mode
# picks an ``image_context`` header + a body prompt tuned for that view.
# Consistent with :func:`interpret_image`'s ``_INTERPRET_PROMPTS``.
_LABEL_PROMPTS: dict[str, dict[str, str]] = {
    "streetview": {
        "image_context": "a Google Street View panorama (ground-level, human-eye perspective)",
        "body": (
            "Detect and label the {max_labels} most noteworthy features and objects.\n"
            "Be specific with labels (e.g. 'white SUV' not just 'car', "
            "'brick ranch house' not 'building').\n"
        ),
    },
    "satellite-map": {
        "image_context": "a Google satellite image (nadir, top-down aerial view)",
        "body": (
            "Detect and label the {max_labels} most noteworthy ROOF-VISIBLE "
            "features. Focus on things clearly resolvable from above: "
            "rooftops (be specific — house / warehouse / commercial / "
            "with-solar / with-pool), driveways, parking lots, road "
            "segments, individual tree crowns (or grouped canopy patches), "
            "swimming pools, cul-de-sacs, cropland fields, water bodies. "
            "Do NOT try to identify small ground-level objects (people, "
            "signs, mailboxes) — they aren't reliably resolvable at this "
            "scale and any label would be a guess.\n"
        ),
    },
    "hybrid-map": {
        "image_context": "a Google hybrid map (nadir satellite view with road and place labels overlaid)",
        "body": (
            "Detect and label the {max_labels} most noteworthy features. "
            "Include roof-visible physical features (rooftops by type, "
            "parking, driveways, tree canopy patches, water) AND the "
            "labeled place names visible on the label overlay. When "
            "boxing a place label, use its text bounding box.\n"
        ),
    },
    "roadmap": {
        "image_context": "a Google roadmap (cartographic street map, top-down, no imagery)",
        "body": (
            "Detect and label the {max_labels} most noteworthy MAP "
            "features — this is a cartographic map, not a photograph. "
            "Focus on: labeled roads (highways / arterials / local), "
            "labeled place names, labeled parks and water bodies, major "
            "intersections. Treat text labels as their own bounding "
            "boxes.\n"
        ),
    },
    "terrain": {
        "image_context": "a Google terrain map (topographic view with shaded relief, contours, and labels)",
        "body": (
            "Detect and label the {max_labels} most noteworthy features. "
            "Focus on named topographic features (peaks, ridges, valleys, "
            "drainages) and any labeled places or roads. Treat text "
            "labels as their own bounding boxes.\n"
        ),
    },
}


def _parse_gemini_detections(text: str | None) -> tuple[list[dict], str | None]:
    """Parse a Gemini bounding-box response.

    Gemini's ``response_mime_type="application/json"`` mode is usually
    but not always well-formed — it occasionally emits stray quotes
    before object opens, unterminated strings, or wraps everything in
    markdown fences. Fall back through progressively-more-lenient
    strategies before giving up.

    Returns ``(detections, parse_error)``. ``parse_error`` is ``None``
    when a clean parse succeeded and a short string describing the
    fallback path when repair was needed. Callers can surface this in
    a ``metadata`` dict so silent zero-detection failures become
    visible.
    """
    if not text or not text.strip():
        return [], "empty response"

    import json as _json
    import re as _re

    raw = text.strip()

    # 1. Straight JSON parse — the happy path
    try:
        parsed = _json.loads(raw)
        if isinstance(parsed, dict):
            for key in ("detections", "objects", "labels", "features", "results"):
                if key in parsed and isinstance(parsed[key], list):
                    return parsed[key], None
        elif isinstance(parsed, list):
            # Gemini sometimes emits a bare list of detections
            return parsed, None
    except _json.JSONDecodeError:
        pass

    # 2. Strip markdown code fences — ``` or ```json wrappers
    stripped = raw
    if stripped.startswith("```"):
        _lines = stripped.splitlines()
        if len(_lines) >= 3 and _lines[-1].strip().startswith("```"):
            stripped = "\n".join(_lines[1:-1])
        try:
            parsed = _json.loads(stripped)
            if isinstance(parsed, dict):
                for key in ("detections", "objects", "labels", "features", "results"):
                    if key in parsed and isinstance(parsed[key], list):
                        return parsed[key], "markdown-fence stripped"
            elif isinstance(parsed, list):
                return parsed, "markdown-fence stripped"
        except _json.JSONDecodeError:
            pass

    # 3. Repair common Gemini malformations before parsing.
    #    - Stray `"` before an object open: `, "{"label"` → `, {"label"`
    #    - Trailing commas before `]` or `}`
    repaired = _re.sub(r',\s*"\s*(\{\s*"label")', r', \1', stripped)
    repaired = _re.sub(r',(\s*[\]\}])', r'\1', repaired)
    try:
        parsed = _json.loads(repaired)
        if isinstance(parsed, dict):
            for key in ("detections", "objects", "labels", "features", "results"):
                if key in parsed and isinstance(parsed[key], list):
                    return parsed[key], "repair rules applied"
        elif isinstance(parsed, list):
            return parsed, "repair rules applied"
    except _json.JSONDecodeError:
        pass

    # 4. Regex fallback — extract each ``{"label": "...", "box_2d": [n,n,n,n]}``
    #    object individually. Tolerates arbitrary garbage between them.
    _OBJ_RE = _re.compile(
        r'\{\s*"label"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*,'
        r'\s*"box_2d"\s*:\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*,'
        r'\s*([\d.]+)\s*,\s*([\d.]+)\s*\]\s*\}'
    )
    dets = []
    for m in _OBJ_RE.finditer(raw):
        label = m.group(1)
        try:
            box = [float(m.group(2)), float(m.group(3)),
                   float(m.group(4)), float(m.group(5))]
        except ValueError:
            continue
        dets.append({"label": label, "box_2d": box})
    if dets:
        return dets, "regex-extracted objects (JSON was malformed)"

    # 5. Give up — return an empty list and describe the failure.
    return [], f"unparseable (starts with: {raw[:60]!r})"


def _extract_gemini_metadata(response, model: str, temperature: float,
                               mode: str | None, prompt_used: str,
                               source: str = "geeviz.maps") -> dict[str, Any]:
    """Pull tokens / model / temp out of a Gemini ``GenerateContentResponse``.

    Returns a stable metadata dict for the caller, and reports the usage
    half of it to :mod:`geeViz.llmUsage` so a host can account for what
    this call spent. Token fields are always present and always ints —
    they used to default to ``None``, which turned a missing count into
    a ``TypeError`` on whichever display path formatted it next.

    ``source`` names the surface for per-subtype reporting; it is what
    lets an admin see map labelling spend apart from chat spend.
    """
    meta: dict[str, Any] = {
        "temperature": temperature,
        "mode": mode,
        "prompt_used": prompt_used,
    }

    # Token counts, model and the de-dup id come from the one canonical
    # extractor so every geeViz surface reports the same field names as
    # the consumer that bills for them. This function used to spell them
    # ``input_tokens`` / ``output_tokens`` / ``thought_tokens`` — a
    # second vocabulary for the same numbers, and a silent field drop at
    # every hand-off between the two.
    usage = _lu.usage_from_response(response, model=model, source=source)
    meta.update(usage)
    _lu.report(usage)

    # Finish reason from the first candidate, if present.
    finish = None
    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            fr = getattr(candidates[0], "finish_reason", None)
            finish = str(fr) if fr is not None else None
    except Exception:
        pass
    meta["finish_reason"] = finish
    return meta


def interpret_image(
    image_bytes: bytes,
    mode: str = "streetview",
    prompt: str | None = None,
    model: str = _lu.DEFAULT_MODEL,
    temperature: float = 0.3,
    context: str | None = None,
) -> dict[str, Any]:
    """Interpret a Street View or static-map image using Google Gemini.

    Sends the image to Gemini with instructions to identify and count
    all notable features. The default prompt is chosen from
    :data:`_INTERPRET_PROMPTS` by ``mode``, so the same function handles
    ground-level Street View, top-down satellite, hybrid, roadmap, and
    terrain views without the caller needing to write a prompt.

    Args:
        image_bytes (bytes): JPEG or PNG image bytes.
        mode (str, optional): View type — picks the default prompt.
            One of ``"streetview"``, ``"satellite-map"``, ``"hybrid-map"``,
            ``"roadmap"``, ``"terrain"``. Defaults to ``"streetview"``.
        prompt (str, optional): Custom prompt to override the mode's
            default. When ``None``, uses ``_INTERPRET_PROMPTS[mode]``.
        model (str, optional): Gemini model name. Defaults to
            :data:`geeViz.llmUsage.DEFAULT_MODEL`.
        temperature (float, optional): Sampling temperature. Defaults to
            ``0.3``.
        context (str, optional): Additional context prepended to the
            prompt (e.g. location, date, purpose). Defaults to ``None``.

    Returns:
        dict: Keys:

        - ``description`` (str): Full text description of the image.
        - ``object_counts`` (str): Markdown table of object counts.
        - ``raw_response`` (str): Complete Gemini response text.
        - ``metadata`` (dict): Token counts (``prompt_tokens``,
          ``prompt_text_tokens``, ``prompt_image_tokens``,
          ``candidates_tokens``, ``thoughts_tokens``, ``cached_tokens``,
          ``total_tokens``), plus ``model``, ``temperature``, ``mode``,
          ``prompt_used``, and ``finish_reason``.

    Example:
        >>> img = streetview_image(-111.80, 40.68, heading=0)
        >>> result = interpret_image(img, mode="streetview")
        >>> print(result['description'])
        >>> print(result['metadata']['total_tokens'])
    """
    _check_gmaps_ai_enabled("interpret_image")
    from google import genai
    from google.genai import types

    api_key = _get_gemini_key()
    client = _lu.genai_client(api_key)

    if prompt is None:
        if mode not in _INTERPRET_PROMPTS:
            raise ValueError(
                f"Unknown mode {mode!r}. Valid modes: "
                f"{sorted(_INTERPRET_PROMPTS)}"
            )
        prompt = _INTERPRET_PROMPTS[mode]
    if context:
        prompt = f"Additional context: {context}\n\n{prompt}"

    image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")

    response = client.models.generate_content(
        model=model,
        contents=[prompt, image_part],
        config=_lu.genai_config(temperature=temperature),
    )

    raw = response.text

    # Parse out sections
    lines = raw.split("\n")
    in_table = False
    desc_lines = []
    table_lines = []

    for line in lines:
        if "|" in line and ("Object" in line or "Count" in line
                              or "Feature" in line or "---" in line):
            in_table = True
        if in_table:
            if "|" in line:
                table_lines.append(line)
            elif line.strip() == "":
                if table_lines:
                    in_table = False
            else:
                in_table = False
        elif not line.strip().startswith("#") and not line.strip().startswith("**Object"):
            desc_lines.append(line)

    description = "\n".join(desc_lines).strip()
    object_counts = "\n".join(table_lines).strip()

    return {
        "description": description,
        "object_counts": object_counts,
        "raw_response": raw,
        "metadata": _extract_gemini_metadata(
            response, model=model, temperature=temperature,
            mode=mode, prompt_used=prompt, source="geeviz.maps.describe",
        ),
    }


STREET_VIEW_OFF_RULE = (
    "- **Street View — not available here.** The `gm.streetview_*` helpers "
    "raise an error in this deployment, and the image tools' `streetview` "
    "mode has no Street View images to work on. When a user asks for Street "
    "View or a ground-level view, say it is not available here and offer "
    "satellite, aerial or map imagery of the place instead."
)


def street_view_instructions(text: str, enabled: bool) -> str:
    """Agent instructions with the Street View rule for this deployment.

    The shared instructions wrap their Street View rule in
    ``<!--STREETVIEW_BEGIN-->`` / ``<!--STREETVIEW_END-->``. On, the rule
    stays (markers removed); off, it is replaced by
    :data:`STREET_VIEW_OFF_RULE`, so the model never plans a call the
    runtime (:func:`_check_street_view_enabled`) will refuse.
    """
    import re
    pat = re.compile(r"<!--STREETVIEW_BEGIN-->\r?\n(.*?)\r?\n<!--STREETVIEW_END-->", re.S)
    text = pat.sub((lambda m: m.group(1)) if enabled else (lambda m: STREET_VIEW_OFF_RULE), text)
    return text.replace("<!--STREETVIEW_STATUS-->", "")


def street_view_enabled() -> bool:
    """The ``GEEVIZ_STREET_VIEW_ENABLED`` setting (absent means on)."""
    val = str(os.environ.get("GEEVIZ_STREET_VIEW_ENABLED", "1")).strip().lower()
    return val not in ("0", "false", "no", "off", "disabled")


def _check_street_view_enabled(tool_name: str) -> None:
    """Raise if Street View is turned off for the calling tenant.

    The geeViz_agent framework sets ``GEEVIZ_STREET_VIEW_ENABLED`` from
    ``tenant.tools.enable_street_view``; ``0`` / ``false`` / ``no`` /
    ``off`` turns every ``streetview_*`` helper off. Absent means on (the
    default outside the agent). Checked before any request, so a tenant
    whose Maps key does not allow Street View gets a clear message
    instead of a 403 from Google.
    """
    if not street_view_enabled():
        raise RuntimeError(
            f"Street View ('{tool_name}') is not available here. Use satellite, "
            f"aerial or map imagery instead (gm.static_map, NAIP or Sentinel-2 "
            f"through Earth Engine).")


def _check_gmaps_ai_enabled(tool_name: str) -> None:
    """Raise if the calling tenant has disabled Google-Maps AI tools.

    The geeViz_agent tenant framework sets ``GEEVIZ_GMAPS_AI_ENABLED``
    at startup from ``tenant.tools.enable_gmaps_ai_tools``. Any value
    of ``0`` / ``false`` / ``no`` (case-insensitive) disables all four
    AI-powered gmaps helpers (:func:`interpret_image`,
    :func:`label_image`, :func:`segment_image`, and
    :func:`geeViz.inventoryLib.inventory_area`). Absent / any other
    value → enabled (the default when running outside the agent).

    Called at the top of each protected function so the enforcement is
    at call site — no matter how the caller reached the function
    (direct import, ``run_code``, notebook), the tenant policy is
    honoured.
    """
    val = str(os.environ.get("GEEVIZ_GMAPS_AI_ENABLED", "1")).strip().lower()
    if val in ("0", "false", "no", "off", "disabled"):
        raise RuntimeError(
            f"Google Maps AI tool '{tool_name}' is disabled for this "
            f"tenant. Set tenant.tools.enable_gmaps_ai_tools=True in "
            f"tenants/<name>/tenant.yaml (or unset GEEVIZ_GMAPS_AI_ENABLED) "
            f"to enable."
        )




def _get_gemini_key() -> str:
    """Get the Gemini API key, separate from Maps Platform key."""
    # Sandbox tolerance: same rationale as _get_api_key — the MCP server
    # pre-loads .env into os.environ at startup, so a blocked open() here
    # falls through cleanly to the os.environ check below.
    _env = {}
    _env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        if os.path.exists(_env_path):
            with open(_env_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if "=" in _line and not _line.startswith("#"):
                        _k, _v = _line.split("=", 1)
                        _env[_k.strip()] = _v.strip().strip("'\"")
    except (PermissionError, OSError):
        pass
    # Check Gemini-specific key first, then general Google key
    for key_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        for source in (_env, os.environ):
            val = source.get(key_name)
            if val:
                return val
    return _get_api_key()  # last resort: use Maps Platform key


def label_image(
    image_bytes: bytes,
    mode: str = "streetview",
    prompt: str | None = None,
    image_context: str | None = None,
    location_str: str = "",
    model: str = _lu.DEFAULT_MODEL,
    temperature: float = 0.3,
    max_labels: int = 30,
    font_size: int = 12,
) -> dict[str, Any] | None:
    """Detect and label objects in an image using Gemini vision.

    Sends ``image_bytes`` to Gemini, asks for JSON-formatted bounding
    boxes, then draws labeled boxes on the image. Works for any image
    Gemini can see — Street View panoramas, satellite/nadir tiles,
    static maps, or arbitrary user-supplied photos.

    ``mode`` picks a preset from :data:`_LABEL_PROMPTS` — same modes as
    :func:`interpret_image` (``streetview`` / ``satellite-map`` /
    ``hybrid-map`` / ``roadmap`` / ``terrain``). Each preset supplies
    an ``image_context`` header (telling Gemini what kind of image
    this is) and a detection prompt tuned for that view. Nadir modes
    ask for roof-visible features and skip small ground-level objects
    that aren't resolvable from above.

    Args:
        image_bytes (bytes): JPEG or PNG bytes.
        mode (str, optional): View type. One of ``"streetview"``,
            ``"satellite-map"``, ``"hybrid-map"``, ``"roadmap"``,
            ``"terrain"``. Defaults to ``"streetview"``.
        prompt (str, optional): Custom detection body prompt — overrides
            the mode's body. Image context header and JSON-format
            footer are still added around it.
        image_context (str, optional): Override the mode's context
            header. Falls back to the mode's default when None.
        location_str (str, optional): "At X" location text for the
            header. Empty string skips it.
        model (str, optional): Gemini model. Defaults to
            :data:`geeViz.llmUsage.DEFAULT_MODEL`.
        temperature (float, optional): Sampling temperature. Defaults
            to ``0.3``.
        max_labels (int, optional): Maximum objects. Defaults to ``30``.
        font_size (int, optional): Label font size. Defaults to ``12``.

    Returns:
        dict or None: Keys: ``image`` (labeled JPEG bytes),
        ``detections`` (list), ``summary`` (markdown table),
        ``original`` (input bytes), ``metadata`` (token counts + model
        + temperature + finish_reason + ``parse_error`` if the JSON
        response needed repair or couldn't be parsed). Returns ``None``
        only if PIL can't decode the input.

    Example:
        >>> sat = get_static_map(-111.80, 40.68, maptype="satellite", zoom=18)
        >>> r = label_image(sat, mode="satellite-map",
        ...                  location_str="Salt Lake City")
        >>> print(r['summary'])
        >>> print(r['metadata']['total_tokens'])
    """
    _check_gmaps_ai_enabled("label_image")
    from PIL import Image, ImageDraw, ImageFont
    import io as _io
    from google import genai
    from google.genai import types

    if mode not in _LABEL_PROMPTS:
        raise ValueError(
            f"Unknown mode {mode!r}. Valid modes: {sorted(_LABEL_PROMPTS)}"
        )
    _preset = _LABEL_PROMPTS[mode]

    try:
        img = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None
    img_w, img_h = img.size

    # Build prompt: header + body + footer.
    # Mode's defaults fill in unless the caller overrode via image_context
    # or prompt.
    _what = image_context or _preset["image_context"]
    _at = f" at {location_str}" if location_str else ""
    _header = f"This is {_what}{_at}.\n"
    if prompt is None:
        _body = _preset["body"].format(max_labels=max_labels)
    else:
        _body = prompt + "\n"
    _footer = (
        "\nFor each detection, return the object label and its bounding box "
        "as [y_min, x_min, y_max, x_max] normalized to 0-1000.\n"
        "Do NOT identify or label Google watermarks, copyright text, or UI overlays.\n"
        "Return ONLY valid JSON:\n"
        '{"detections": [{"label": "object name", "box_2d": [y_min, x_min, y_max, x_max]}]}\n'
    )
    prompt_used = _header + _body + _footer

    # Call Gemini
    api_key = _get_gemini_key()
    client = _lu.genai_client(api_key)
    image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")

    response = client.models.generate_content(
        model=model,
        contents=[prompt_used, image_part],
        config=_lu.genai_config(
            temperature=temperature,
            response_mime_type="application/json",
        ),
    )

    # Parse — Gemini's JSON mode occasionally emits malformed output
    # (stray quotes before object opens, unterminated strings, extra
    # markdown fences). Fall back through progressively-more-lenient
    # strategies before giving up.
    detections, parse_error = _parse_gemini_detections(response.text)

    # One color per unique label
    import random as _rand, colorsys as _cs
    _rand.seed(42)
    unique_labels = list(dict.fromkeys(d.get("label", "?") for d in detections))
    label_colors: dict[str, tuple] = {}
    for i, lbl in enumerate(unique_labels):
        hue = (i / max(len(unique_labels), 1) + _rand.uniform(-0.03, 0.03)) % 1.0
        r, g, b = _cs.hsv_to_rgb(hue, 0.9, 0.95)
        label_colors[lbl] = (int(r * 255), int(g * 255), int(b * 255))

    # Draw boxes
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.load_default(size=font_size)
        except TypeError:
            font = ImageFont.load_default()

    parsed = []
    for det in detections:
        label = det.get("label", "?")
        box = det.get("box_2d", [])
        if len(box) != 4:
            continue
        color = label_colors.get(label, (0, 255, 100))
        y0, x0, y1, x1 = box
        px_x0 = int(x0 / 1000 * img_w)
        px_y0 = int(y0 / 1000 * img_h)
        px_x1 = int(x1 / 1000 * img_w)
        px_y1 = int(y1 / 1000 * img_h)

        # Dashed box
        for edge in [[(px_x0,px_y0),(px_x1,px_y0)], [(px_x1,px_y0),(px_x1,px_y1)],
                     [(px_x1,px_y1),(px_x0,px_y1)], [(px_x0,px_y1),(px_x0,px_y0)]]:
            (sx,sy),(ex,ey) = edge
            dx, dy = ex-sx, ey-sy
            length = max(1, (dx**2+dy**2)**0.5)
            for d in range(int(length/13)+1):
                sf = d*13/length
                ef = min((d*13+8)/length, 1.0)
                draw.line([(int(sx+dx*sf),int(sy+dy*sf)),
                           (int(sx+dx*ef),int(sy+dy*ef))], fill=color, width=2)

        # Label
        tb = draw.textbbox((0,0), label, font=font)
        tw, th = tb[2]-tb[0], tb[3]-tb[1]
        ly = max(0, px_y0-th-4)
        draw.rectangle([px_x0, ly, px_x0+tw+6, ly+th+4], fill=(0,0,0))
        draw.text((px_x0+3, ly+1), label, fill=color, font=font)

        parsed.append({"label": label, "box": [px_x0,px_y0,px_x1,px_y1], "color": color})

    # Summary
    lines = ["| # | Object | Box |", "|---|---|---|"]
    for i, d in enumerate(parsed):
        b = d["box"]
        lines.append(f"| {i+1} | {d['label']} | ({b[0]},{b[1]},{b[2]},{b[3]}) |")

    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=92)

    return {
        "image": buf.getvalue(),
        "detections": parsed,
        "summary": "\n".join(lines),
        "original": image_bytes,
        "metadata": {
            **_extract_gemini_metadata(
                response, model=model, temperature=temperature,
                mode=mode, prompt_used=prompt_used,
                source="geeviz.maps.detect",
            ),
            # Surfaced so a silent zero-detection result is visible
            # ("what happened?" → check metadata.parse_error).
            "parse_error": parse_error,
            "detections_count": len(parsed),
        },
    }


def label_streetview(
    lon: float,
    lat: float,
    prompt: str | None = None,
    heading: float = 0,
    fov: float = 360,
    pitch: float = 0,
    size: str = _SV_DEFAULT_SIZE,
    radius: int = 50,
    source: str = "default",
    model: str = _lu.DEFAULT_MODEL,
    temperature: float = 0.3,
    max_labels: int = 30,
    font_size: int = 12,
) -> dict[str, Any] | None:
    """Fetch a Street View panorama and label objects on it.

    Thin lon/lat wrapper around :func:`label_image`. Reverse-geocodes
    the point for the prompt header, fetches the panorama, calls
    ``label_image``, and adds ``location`` to the returned dict.

    Args, return dict, and metadata block match :func:`label_image`
    plus an extra ``location`` string. Returns ``None`` if no
    panorama is available at that point.
    """
    # Fetch the panorama
    pano_bytes = streetview_panorama(
        lon, lat, heading=heading, fov=fov, pitch=pitch,
        size=size, radius=radius, source=source,
    )
    if pano_bytes is None:
        return None

    # Get location info
    meta = streetview_metadata(lon, lat, radius=radius, source=source)
    location_str = ""
    if meta.get("status") == "OK":
        addr = reverse_geocode(lon, lat)
        location_str = addr.get("formatted_address", "") if addr else f"({lat:.4f}, {lon:.4f})"

    result = label_image(
        pano_bytes,
        mode="streetview",
        prompt=prompt,
        location_str=location_str,
        model=model,
        temperature=temperature,
        max_labels=max_labels,
        font_size=font_size,
    )
    if result is not None:
        result["location"] = location_str
    return result


def streetview_html(
    lon: float,
    lat: float,
    headings: list[float] | None = None,
    pitch: float = 0,
    fov: float = _SV_DEFAULT_FOV,
    size: str = "400x300",
    radius: int = 50,
    source: str = "default",
    title: str | None = None,
) -> str | None:
    """Generate an HTML panel with embedded Street View images.

    Args:
        lon, lat: Coordinates.
        headings (list, optional): Compass headings. Defaults to [0,90,180,270].
        pitch, fov, size, radius, source: See :func:`streetview_image`.
        title (str, optional): Title text. Auto-generated if None.

    Returns:
        str or None: Self-contained HTML string, or None if no imagery.
    """
    _check_street_view_enabled("streetview_html")
    meta = streetview_metadata(lon, lat, radius=radius, source=source)
    if meta.get("status") != "OK":
        return None
    if headings is None:
        headings = [0, 90, 180, 270]
    dir_labels = {0: "N", 45: "NE", 90: "E", 135: "SE",
                  180: "S", 225: "SW", 270: "W", 315: "NW"}
    if title is None:
        loc = meta.get("location", {})
        title = f"Street View at ({loc.get('lat', lat):.4f}, {loc.get('lng', lon):.4f}) — {meta.get('date', '?')}"
    tags = []
    for h in headings:
        img = streetview_image(lon, lat, heading=h, pitch=pitch, fov=fov,
                               size=size, radius=radius, source=source)
        if img:
            b64 = base64.b64encode(img).decode("ascii")
            label = dir_labels.get(int(h) % 360, f"{h}°")
            tags.append(
                f'<div style="text-align:center;margin:4px;">'
                f'<img src="data:image/jpeg;base64,{b64}" style="border-radius:4px;max-width:100%;"/>'
                f'<div style="font-size:12px;color:#aaa;">{label} ({h}°)</div></div>'
            )
    if not tags:
        return None
    cols = min(len(tags), 2)
    return (
        f'<div style="background:#1e1e1e;padding:12px;border-radius:8px;max-width:900px;font-family:sans-serif;">'
        f'<div style="color:#eee;font-size:14px;font-weight:bold;margin-bottom:8px;">{title}</div>'
        f'<div style="display:grid;grid-template-columns:repeat({cols},1fr);gap:6px;">{"".join(tags)}</div>'
        f'<div style="color:#666;font-size:10px;margin-top:6px;">{meta.get("copyright", "© Google")}</div></div>'
    )


###########################################################################
#  Elevation API
###########################################################################

_ELEVATION_URL = "https://maps.googleapis.com/maps/api/elevation/json"


def get_elevation(lon: float, lat: float) -> float | None:
    """Get elevation in meters at a geographic location.

    Args:
        lon (float): Longitude.
        lat (float): Latitude.

    Returns:
        float or None: Elevation in meters above sea level,
        or ``None`` on error.

    Example:
        >>> elev = get_elevation(-111.80, 40.68)
        >>> print(f"{elev:.0f} meters")
    """
    data = _fetch_json(_ELEVATION_URL, {
        "locations": f"{lat},{lon}",
        "key": _get_api_key(),
    })
    if data.get("status") == "OK" and data.get("results"):
        return data["results"][0].get("elevation")
    return None


def get_elevations(points: list[tuple[float, float]]) -> list[dict[str, Any]]:
    """Get elevations for multiple locations in one request.

    Args:
        points (list): List of ``(lon, lat)`` tuples. Max ~500 per request.

    Returns:
        list of dict: Each dict has ``lon``, ``lat``, ``elevation`` (meters),
        and ``resolution`` (meters).

    Example:
        >>> pts = [(-111.80, 40.68), (-111.81, 40.69), (-111.82, 40.70)]
        >>> elevs = get_elevations(pts)
        >>> for e in elevs:
        ...     print(f"{e['lat']:.4f}: {e['elevation']:.0f}m")
    """
    locations = "|".join(f"{lat},{lon}" for lon, lat in points)
    data = _fetch_json(_ELEVATION_URL, {
        "locations": locations,
        "key": _get_api_key(),
    })
    results = []
    if data.get("status") == "OK":
        for r in data.get("results", []):
            loc = r.get("location", {})
            results.append({
                "lon": loc.get("lng"),
                "lat": loc.get("lat"),
                "elevation": r.get("elevation"),
                "resolution": r.get("resolution"),
            })
    return results


def get_elevation_along_path(
    points: list[tuple[float, float]],
    samples: int = 100,
) -> list[dict[str, Any]]:
    """Get elevation profile along a path.

    Samples evenly-spaced points along the path defined by the
    input waypoints.

    Args:
        points (list): Path waypoints as ``(lon, lat)`` tuples.
        samples (int, optional): Number of sample points. Defaults to 100.

    Returns:
        list of dict: Sampled points with ``lon``, ``lat``, ``elevation``,
        ``resolution``.

    Example:
        >>> path = [(-111.80, 40.68), (-111.85, 40.72)]
        >>> profile = get_elevation_along_path(path, samples=50)
    """
    path_str = "|".join(f"{lat},{lon}" for lon, lat in points)
    data = _fetch_json(_ELEVATION_URL, {
        "path": path_str,
        "samples": str(samples),
        "key": _get_api_key(),
    })
    results = []
    if data.get("status") == "OK":
        for r in data.get("results", []):
            loc = r.get("location", {})
            results.append({
                "lon": loc.get("lng"),
                "lat": loc.get("lat"),
                "elevation": r.get("elevation"),
                "resolution": r.get("resolution"),
            })
    return results


###########################################################################
#  Static Maps API
###########################################################################

_STATIC_MAP_URL = "https://maps.googleapis.com/maps/api/staticmap"


def get_static_map(
    lon: float,
    lat: float,
    zoom: int = 14,
    size: str = "640x480",
    maptype: str = "satellite",
    markers: list[tuple[float, float]] | None = None,
    path_points: list[tuple[float, float]] | None = None,
    path_color: str = "red",
    format: str = "png",
) -> bytes | None:
    """Get a static map image centered on a location.

    Args:
        lon (float): Center longitude.
        lat (float): Center latitude.
        zoom (int, optional): Zoom level (1-21). Defaults to 14.
        size (str, optional): Image size. Defaults to ``"640x480"``.
        maptype (str, optional): ``"satellite"``, ``"roadmap"``,
            ``"terrain"``, or ``"hybrid"``. Defaults to ``"satellite"``.
        markers (list, optional): List of ``(lon, lat)`` marker positions.
        path_points (list, optional): List of ``(lon, lat)`` for a path overlay.
        path_color (str, optional): Path line color. Defaults to ``"red"``.
        format (str, optional): ``"png"`` or ``"jpg"``. Defaults to ``"png"``.

    Returns:
        bytes or None: Image bytes.

    Example:
        >>> img = get_static_map(-111.80, 40.68, zoom=16, maptype="hybrid")
        >>> with open("map.png", "wb") as f:
        ...     f.write(img)
    """
    params: dict[str, str] = {
        "center": f"{lat},{lon}",
        "zoom": str(zoom),
        "size": size,
        "maptype": maptype,
        "format": format,
        "key": _get_api_key(),
    }
    if markers:
        marker_str = "|".join(f"{la},{lo}" for lo, la in markers)
        params["markers"] = marker_str
    if path_points:
        path_str = "|".join(f"{la},{lo}" for lo, la in path_points)
        params["path"] = f"color:{path_color}|weight:3|{path_str}"

    try:
        return _fetch_bytes(_STATIC_MAP_URL, params)
    except Exception:
        return None


###########################################################################
#  Air Quality API
###########################################################################

_AQ_BASE = "https://airquality.googleapis.com/v1"


def get_air_quality(
    lon: float,
    lat: float,
) -> dict[str, Any] | None:
    """Get current air quality conditions at a location.

    Args:
        lon (float): Longitude.
        lat (float): Latitude.

    Returns:
        dict or None: Keys: ``aqi`` (US AQI), ``category``,
        ``dominant_pollutant``, ``pollutants`` (list), ``date``.

    Example:
        >>> aq = get_air_quality(-111.89, 40.76)
        >>> if aq:
        ...     print(f"AQI: {aq['aqi']} ({aq['category']})")
    """
    try:
        data = _fetch_json(
            f"{_AQ_BASE}/currentConditions:lookup",
            method="POST",
            body={
                "location": {"latitude": lat, "longitude": lon},
                "extraComputations": ["DOMINANT_POLLUTANT_CONCENTRATION"],
                "languageCode": "en",
            },
            headers={
                "X-Goog-Api-Key": _get_api_key(),
                "Content-Type": "application/json",
            },
        )
    except Exception:
        return None

    indexes = data.get("indexes", [])
    us_aqi = next((i for i in indexes if i.get("code") == "uaqi"), None)
    if not us_aqi:
        us_aqi = indexes[0] if indexes else {}

    pollutants = []
    for p in data.get("pollutants", []):
        pollutants.append({
            "code": p.get("code"),
            "name": p.get("displayName"),
            "concentration": p.get("concentration", {}).get("value"),
            "units": p.get("concentration", {}).get("units"),
        })

    return {
        "aqi": us_aqi.get("aqi"),
        "category": us_aqi.get("category"),
        "dominant_pollutant": us_aqi.get("dominantPollutant"),
        "color": us_aqi.get("color"),
        "pollutants": pollutants,
        "date": data.get("dateTime"),
    }


###########################################################################
#  Solar API
###########################################################################

_SOLAR_BASE = "https://solar.googleapis.com/v1"


def get_solar_insights(
    lon: float,
    lat: float,
    quality: str = "MEDIUM",
) -> dict[str, Any] | None:
    """Get rooftop solar potential for the nearest building.

    Args:
        lon (float): Longitude.
        lat (float): Latitude.
        quality (str, optional): Image quality — ``"LOW"``, ``"MEDIUM"``,
            or ``"HIGH"``. Defaults to ``"MEDIUM"``.

    Returns:
        dict or None: Keys: ``max_panels``, ``max_capacity_watts``,
        ``max_annual_kwh``, ``roof_area_m2``, ``max_sunshine_hours``,
        ``carbon_offset_kg``.

    Example:
        >>> solar = get_solar_insights(-111.80, 40.68)
        >>> if solar:
        ...     print(f"Capacity: {solar['max_capacity_watts']:.0f}W")
        ...     print(f"Annual: {solar['max_annual_kwh']:.0f} kWh")
    """
    try:
        data = _fetch_json(
            f"{_SOLAR_BASE}/buildingInsights:findClosest",
            {
                "location.latitude": str(lat),
                "location.longitude": str(lon),
                "requiredQuality": quality,
                "key": _get_api_key(),
            },
        )
    except Exception:
        return None

    if "error" in data:
        return None

    solar_info = data.get("solarPotential", {})
    panels = solar_info.get("solarPanelConfigs", [])
    best = panels[-1] if panels else {}

    return {
        "max_panels": best.get("panelsCount", 0),
        "max_capacity_watts": solar_info.get("maxArrayPanelsCount", 0) * solar_info.get("panelCapacityWatts", 400),
        "max_annual_kwh": best.get("yearlyEnergyDcKwh", 0),
        "roof_area_m2": solar_info.get("wholeRoofStats", {}).get("areaMeters2", 0),
        "max_sunshine_hours": solar_info.get("maxSunshineHoursPerYear", 0),
        "carbon_offset_kg": solar_info.get("carbonOffsetFactorKgPerMwh", 0) * best.get("yearlyEnergyDcKwh", 0) / 1000,
        "panel_capacity_watts": solar_info.get("panelCapacityWatts", 0),
        "imagery_date": data.get("imageryDate", {}),
    }


###########################################################################
#  Roads API
###########################################################################

_ROADS_BASE = "https://roads.googleapis.com/v1"


def snap_to_roads(
    points: list[tuple[float, float]],
    interpolate: bool = False,
) -> list[dict[str, Any]]:
    """Snap GPS points to the nearest road segments.

    Args:
        points (list): GPS trace as ``(lon, lat)`` tuples. Max 100 points.
        interpolate (bool, optional): If True, interpolate additional
            points along the road between snapped locations.
            Defaults to ``False``.

    Returns:
        list of dict: Snapped points with ``lon``, ``lat``, ``place_id``,
        and ``original_index`` (which input point this snapped from).

    Example:
        >>> gps = [(-111.80, 40.68), (-111.81, 40.69), (-111.82, 40.70)]
        >>> snapped = snap_to_roads(gps)
        >>> for s in snapped:
        ...     print(f"({s['lat']:.5f}, {s['lon']:.5f})")
    """
    path = "|".join(f"{lat},{lon}" for lon, lat in points)
    data = _fetch_json(f"{_ROADS_BASE}/snapToRoads", {
        "path": path,
        "interpolate": str(interpolate).lower(),
        "key": _get_api_key(),
    })
    results = []
    for pt in data.get("snappedPoints", []):
        loc = pt.get("location", {})
        results.append({
            "lon": loc.get("longitude"),
            "lat": loc.get("latitude"),
            "place_id": pt.get("placeId"),
            "original_index": pt.get("originalIndex"),
        })
    return results


def nearest_roads(
    lon: float,
    lat: float,
) -> list[dict[str, Any]]:
    """Find the nearest road segments to a point.

    Args:
        lon (float): Longitude.
        lat (float): Latitude.

    Returns:
        list of dict: Nearby road points with ``lon``, ``lat``,
        ``place_id``.

    Example:
        >>> roads = nearest_roads(-111.80, 40.68)
        >>> for r in roads:
        ...     print(f"Road at ({r['lat']:.5f}, {r['lon']:.5f})")
    """
    data = _fetch_json(f"{_ROADS_BASE}/nearestRoads", {
        "points": f"{lat},{lon}",
        "key": _get_api_key(),
    })
    results = []
    for pt in data.get("snappedPoints", []):
        loc = pt.get("location", {})
        results.append({
            "lon": loc.get("longitude"),
            "lat": loc.get("latitude"),
            "place_id": pt.get("placeId"),
        })
    return results


###########################################################################
#  Address Validation API
###########################################################################

_ADDR_VALIDATION_URL = "https://addressvalidation.googleapis.com/v1:validateAddress"


def validate_address(address: str, region_code: str = "US") -> dict[str, Any] | None:
    """Validate and standardize an address.

    Args:
        address (str): Address to validate.
        region_code (str, optional): ISO country code. Defaults to ``"US"``.

    Returns:
        dict or None: Keys: ``formatted_address``, ``lat``, ``lon``,
        ``verdict`` (address quality), ``components`` (parsed parts),
        ``usps_data`` (USPS-standardized for US addresses).

    Example:
        >>> result = validate_address("100 S 200 E, SLC, UT")
        >>> print(result['formatted_address'])
    """
    try:
        data = _fetch_json(
            _ADDR_VALIDATION_URL,
            method="POST",
            body={
                "address": {"addressLines": [address], "regionCode": region_code},
            },
            headers={
                "X-Goog-Api-Key": _get_api_key(),
                "Content-Type": "application/json",
            },
        )
    except Exception:
        return None

    r = data.get("result", {})
    addr = r.get("address", {})
    geo = r.get("geocode", {})
    loc = geo.get("location", {})
    verdict = r.get("verdict", {})
    usps = r.get("uspsData", {})

    return {
        "formatted_address": addr.get("formattedAddress"),
        "lat": loc.get("latitude"),
        "lon": loc.get("longitude"),
        "verdict": {
            "input_granularity": verdict.get("inputGranularity"),
            "validation_granularity": verdict.get("validationGranularity"),
            "address_complete": verdict.get("addressComplete", False),
            "has_inferred_components": verdict.get("hasInferredComponents", False),
        },
        "components": [
            {
                "type": c.get("componentType"),
                "value": c.get("componentName", {}).get("text"),
                "confirmed": c.get("confirmationLevel") == "CONFIRMED",
            }
            for c in addr.get("addressComponents", [])
        ],
        "usps_data": {
            "standardized_address": usps.get("standardizedAddress", {}),
            "delivery_point_code": usps.get("deliveryPointCode"),
        } if usps else None,
        "place_id": geo.get("placeId"),
    }


###########################################################################
#  Time Zone API
###########################################################################

_TIMEZONE_URL = "https://maps.googleapis.com/maps/api/timezone/json"


def get_timezone(lon: float, lat: float, timestamp: int = 0) -> dict[str, Any] | None:
    """Get timezone information for a location.

    Args:
        lon (float): Longitude.
        lat (float): Latitude.
        timestamp (int, optional): Unix timestamp for DST calculation.
            Defaults to ``0`` (current time).

    Returns:
        dict or None: Keys: ``timezone_id``, ``timezone_name``,
        ``utc_offset_seconds``, ``dst_offset_seconds``.

    Example:
        >>> tz = get_timezone(-111.80, 40.68)
        >>> print(tz['timezone_id'])  # 'America/Denver'
    """
    import time as _time
    if timestamp == 0:
        timestamp = int(_time.time())
    data = _fetch_json(_TIMEZONE_URL, {
        "location": f"{lat},{lon}",
        "timestamp": str(timestamp),
        "key": _get_api_key(),
    })
    if data.get("status") != "OK":
        return None
    return {
        "timezone_id": data.get("timeZoneId"),
        "timezone_name": data.get("timeZoneName"),
        "utc_offset_seconds": data.get("rawOffset"),
        "dst_offset_seconds": data.get("dstOffset"),
    }


###########################################################################
#  Reverse Geocoding
###########################################################################


def reverse_geocode(lon: float, lat: float) -> dict[str, Any] | None:
    """Convert coordinates to an address (reverse geocoding).

    Args:
        lon (float): Longitude.
        lat (float): Latitude.

    Returns:
        dict or None: Keys: ``formatted_address``, ``place_id``,
        ``types``, ``address_components``.

    Example:
        >>> result = reverse_geocode(-111.80, 40.68)
        >>> print(result['formatted_address'])
    """
    data = _fetch_json(_GEOCODE_URL, {
        "latlng": f"{lat},{lon}",
        "key": _get_api_key(),
    })
    if data.get("status") != "OK" or not data.get("results"):
        return None
    r = data["results"][0]
    return {
        "formatted_address": r.get("formatted_address", ""),
        "place_id": r.get("place_id", ""),
        "types": r.get("types", []),
        "address_components": r.get("address_components", []),
    }


###########################################################################
#  Semantic Segmentation (SegFormer)
###########################################################################

# ADE20K class names (150 classes)
_ADE20K_CLASSES = [
    "wall", "building", "sky", "floor", "tree", "ceiling", "road", "bed",
    "windowpane", "grass", "cabinet", "sidewalk", "person", "earth",
    "door", "table", "mountain", "plant", "curtain", "chair", "car",
    "water", "painting", "sofa", "shelf", "house", "sea", "mirror",
    "rug", "field", "armchair", "seat", "fence", "desk", "rock",
    "wardrobe", "lamp", "bathtub", "railing", "cushion", "base",
    "box", "column", "signboard", "chest of drawers", "counter",
    "sand", "sink", "skyscraper", "fireplace", "refrigerator", "grandstand",
    "path", "stairs", "runway", "case", "pool table", "pillow", "screen door",
    "stairway", "river", "bridge", "bookcase", "blind", "coffee table",
    "toilet", "flower", "book", "hill", "bench", "countertop", "stove",
    "palm", "kitchen island", "computer", "swivel chair", "boat", "bar",
    "arcade machine", "hovel", "bus", "towel", "light", "truck", "tower",
    "chandelier", "awning", "streetlight", "booth", "television", "airplane",
    "dirt track", "apparel", "pole", "land", "bannister", "escalator",
    "ottoman", "bottle", "buffet", "poster", "stage", "van", "ship",
    "fountain", "conveyer belt", "canopy", "washer", "plaything",
    "swimming pool", "stool", "barrel", "basket", "waterfall", "tent",
    "bag", "minibike", "cradle", "oven", "ball", "food", "step", "tank",
    "trade name", "microwave", "pot", "animal", "bicycle", "lake",
    "dishwasher", "screen", "blanket", "sculpture", "hood", "sconce",
    "vase", "traffic light", "tray", "ashcan", "fan", "pier", "crt screen",
    "plate", "monitor", "bulletin board", "shower", "radiator", "glass",
    "clock", "flag",
]

# Broad category mapping for land cover analysis
_ADE20K_LAND_COVER = {
    "sky": ["sky"],
    "vegetation": ["tree", "grass", "plant", "flower", "palm", "field", "hill"],
    "impervious": ["road", "sidewalk", "path", "floor", "runway", "dirt track"],
    "building": ["building", "house", "wall", "skyscraper", "tower", "hovel",
                  "booth", "awning", "canopy"],
    "vehicle": ["car", "bus", "truck", "van", "boat", "ship", "airplane",
                "bicycle", "minibike"],
    "water": ["water", "sea", "river", "lake", "swimming pool", "fountain",
              "waterfall"],
    "person": ["person"],
    "terrain": ["mountain", "rock", "earth", "sand", "land"],
    "furniture": ["fence", "railing", "bench", "pole", "streetlight",
                  "signboard", "traffic light", "flag"],
}


# ── Nadir aerial taxonomies ──────────────────────────────────────────────
#
# These match the OFFICIAL label order from each dataset's original
# release, so any HuggingFace checkpoint fine-tuned on that dataset will
# emit class indices consistent with the lists below. If you pick a
# checkpoint that reorders classes, override with an explicit
# ``class_names=`` on the call.

# ISPRS Potsdam / Vaihingen — 6 classes, ~9 cm aerial RGB+DSM.
# Good match for Google Static Maps satellite tiles at zoom 18-20.
_POTSDAM_CLASSES = [
    "impervious",        # roads, parking lots, sidewalks
    "building",          # roofs
    "low_vegetation",    # lawns, low crops
    "tree",              # tree canopies
    "car",               # vehicles
    "clutter",           # background / unclassified
]
_POTSDAM_LAND_COVER = {
    "impervious": ["impervious"],
    "building": ["building"],
    "vegetation": ["low_vegetation", "tree"],
    "vehicle": ["car"],
    "other": ["clutter"],
}

# LandCover.ai — 5 classes, ~25 cm–50 cm Polish aerial RGB.
# Good for rural/mixed landscapes (buildings + woodland + water + roads).
_LANDCOVERAI_CLASSES = [
    "background",
    "building",
    "woodland",
    "water",
    "road",
]
_LANDCOVERAI_LAND_COVER = {
    "impervious": ["road"],
    "building": ["building"],
    "vegetation": ["woodland"],
    "water": ["water"],
    "other": ["background"],
}

# DeepGlobe Land Cover — 7 classes, 50 cm satellite RGB.
# Good for broader landscape zooms (Google satellite 14-17) with mixed
# urban / agriculture / natural cover.
_DEEPGLOBE_CLASSES = [
    "urban_land",
    "agriculture_land",
    "rangeland",
    "forest_land",
    "water",
    "barren_land",
    "unknown",
]
_DEEPGLOBE_LAND_COVER = {
    "urban": ["urban_land"],
    "agriculture": ["agriculture_land", "rangeland"],
    "forest": ["forest_land"],
    "water": ["water"],
    "bare": ["barren_land"],
    "other": ["unknown"],
}

# Palettes — one per taxonomy. Colors match each dataset's official
# reference palette where one exists (Potsdam / DeepGlobe do publish
# official RGBs), otherwise picked to be distinguishable.
_POTSDAM_COLOR_MAP = {
    "impervious":     (255, 255, 255),
    "building":       (  0,   0, 255),
    "low_vegetation": (  0, 255, 255),
    "tree":           (  0, 255,   0),
    "car":            (255, 255,   0),
    "clutter":        (255,   0,   0),
}
_LANDCOVERAI_COLOR_MAP = {
    "background": ( 60,  60,  60),
    "building":   (178, 102,  51),
    "woodland":   ( 34, 139,  34),
    "water":      ( 30, 144, 255),
    "road":       (128, 128, 128),
}
_DEEPGLOBE_COLOR_MAP = {
    "urban_land":       (  0, 255, 255),
    "agriculture_land": (255, 255,   0),
    "rangeland":        (255,   0, 255),
    "forest_land":      (  0, 255,   0),
    "water":            (  0,   0, 255),
    "barren_land":      (255, 255, 255),
    "unknown":          (  0,   0,   0),
}

# Single-class specialist taxonomies — used with any binary SegFormer
# checkpoint fine-tuned for the specific feature (buildings, tree
# canopy, roads, etc.). Class order (background=0, foreground=1) is
# the near-universal convention for binary semantic segmentation
# datasets on HuggingFace.
_BUILDING_CLASSES  = ["background", "building"]
_BUILDING_LAND_COVER = {
    "building":   ["building"],
    "background": ["background"],
}
_BUILDING_COLOR_MAP = {
    "background": ( 40,  40,  50),
    "building":   (255, 140,   0),   # bright orange — pops on gray satellite
}

_TREE_CLASSES = ["background", "tree"]
_TREE_LAND_COVER = {
    "tree":       ["tree"],
    "background": ["background"],
}
_TREE_COLOR_MAP = {
    "background": ( 40,  30,  20),
    "tree":       ( 34, 139,  34),   # forest green
}


# Preset registry keyed by ``mode`` for :func:`segment_image`.
#
# Each entry describes: the intended imagery orientation, the class
# taxonomy the checkpoint should emit, a broad-category rollup, a
# color palette, and — where a stable checkpoint exists — a template
# for auto-picking a HuggingFace ``model_id`` from ``model_variant``.
#
# ``model_id_template`` is ``None`` for nadir modes because community
# checkpoint IDs on HF vary and rename over time — hard-coding one
# would rot. Pass ``model_id="user/checkpoint"`` to override. Suggested
# HF search patterns:
#   aerial-urban:     "segformer potsdam", "isprs vaihingen segmentation"
#   aerial-landcover: "segformer landcoverai", "landcover.ai segmentation"
#   aerial-mixed:     "segformer deepglobe", "deepglobe land cover"
_SEGMENT_PRESETS: dict[str, dict] = {
    "streetview": {
        "description": "Ground-level scenes (Street View panoramas, "
                       "oblique photos). SegFormer trained on ADE20K.",
        "orientation": "ground",
        "classes": _ADE20K_CLASSES,
        "broad_categories": _ADE20K_LAND_COVER,
        # ADE20K color map is built inline below (150 classes).
        "color_map": None,
        # NVIDIA official checkpoints — verified: B0-B4 use 512-512, B5 uses 640-640.
        "model_id_template": "nvidia/segformer-{variant}-finetuned-ade-{res}",
    },
    "aerial-urban": {
        "description": "Top-down urban aerial (Google satellite zoom "
                       "18-20). Fine-tuned on ISPRS Potsdam. Pass "
                       "``model_id=`` to pick a specific checkpoint.",
        "orientation": "nadir",
        "classes": _POTSDAM_CLASSES,
        "broad_categories": _POTSDAM_LAND_COVER,
        "color_map": _POTSDAM_COLOR_MAP,
        "model_id_template": None,  # user must supply model_id
    },
    "aerial-landcover": {
        "description": "Rural/mixed aerial (Google satellite zoom 15-19). "
                       "Fine-tuned on LandCover.ai. Pass ``model_id=`` "
                       "to pick a specific checkpoint.",
        "orientation": "nadir",
        "classes": _LANDCOVERAI_CLASSES,
        "broad_categories": _LANDCOVERAI_LAND_COVER,
        "color_map": _LANDCOVERAI_COLOR_MAP,
        "model_id_template": None,
    },
    "aerial-mixed": {
        "description": "Broad landscape satellite (Google satellite "
                       "zoom 12-16, ~50 cm equivalent). Fine-tuned on "
                       "DeepGlobe Land Cover. Pass ``model_id=`` to "
                       "pick a specific checkpoint.",
        "orientation": "nadir",
        "classes": _DEEPGLOBE_CLASSES,
        "broad_categories": _DEEPGLOBE_LAND_COVER,
        "color_map": _DEEPGLOBE_COLOR_MAP,
        "model_id_template": None,
    },
    "buildings": {
        "description": "Building rooftop extraction — binary segmentation "
                       "(background vs. building). Pass ``model_id=`` to a "
                       "HuggingFace SegFormer fine-tuned on any building "
                       "dataset (SpaceNet / INRIA / WHU / Microsoft "
                       "Building Footprints derivatives). Suggested "
                       "search: 'segformer building segmentation'.",
        "orientation": "nadir",
        "classes": _BUILDING_CLASSES,
        "broad_categories": _BUILDING_LAND_COVER,
        "color_map": _BUILDING_COLOR_MAP,
        "model_id_template": None,
    },
    "trees": {
        "description": "Tree-canopy extraction — binary segmentation "
                       "(background vs. tree canopy). Works on any "
                       "orientation the checkpoint was trained for. "
                       "Pass ``model_id=`` — suggested: "
                       "``restor/tcd-segformer-mit-b5`` (Restor "
                       "Foundation Tree Crown Delineation, real + "
                       "actively maintained, trained on aerial RGB).",
        "orientation": "nadir",
        "classes": _TREE_CLASSES,
        "broad_categories": _TREE_LAND_COVER,
        "color_map": _TREE_COLOR_MAP,
        "model_id_template": None,
    },
}


# Cached model/processor — keyed by the loaded ``model_id`` string so
# switching modes doesn't repeatedly re-download.
_segformer_cache: dict[str, tuple] = {}
_segformer_model = None
_segformer_processor = None


def segment_image(
    image_bytes: bytes,
    mode: str = "streetview",
    model_variant: str = "b4",
    broad_categories: bool = False,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Perform pixel-level semantic segmentation on an RGB image.

    Uses a SegFormer checkpoint (via ``transformers``). The ``mode``
    argument picks a preset — class taxonomy + broad-category rollup +
    color palette matched to what the checkpoint emits.

    **Modes**

    - ``"streetview"`` (default, ground-level) — SegFormer B0–B5 on
      ADE20K (150 classes). Works out-of-the-box; used for Street View
      panoramas and oblique photos.
    - ``"aerial-urban"`` (nadir) — Potsdam / Vaihingen 6-class taxonomy
      (impervious / building / low_vegetation / tree / car / clutter).
      Good match for Google Static Maps satellite zoom 18-20.
    - ``"aerial-landcover"`` (nadir) — LandCover.ai 5-class taxonomy
      (background / building / woodland / water / road). Good for
      rural/mixed landscapes at zoom 15-19.
    - ``"aerial-mixed"`` (nadir) — DeepGlobe 7-class taxonomy
      (urban_land / agriculture_land / rangeland / forest_land /
      water / barren_land / unknown). Good for broader landscape at
      zoom 12-16.

    All nadir modes require ``model_id="user/checkpoint"`` — community
    HuggingFace fine-tunes on these datasets exist but their IDs rot,
    so nothing is hard-coded. Suggested HF search terms are in the
    error message you'll get if you forget.

    Args:
        image_bytes (bytes): JPEG or PNG image bytes.
        mode (str, optional): Preset — one of ``"streetview"``,
            ``"aerial-urban"``, ``"aerial-landcover"``,
            ``"aerial-mixed"``. Defaults to ``"streetview"``.
        model_variant (str, optional): SegFormer size — ``"b0"`` (fast,
            3.8M params) through ``"b5"`` (best, 82M params). Only
            affects the ``streetview`` preset's auto model_id; ignored
            when ``model_id`` is set explicitly. Defaults to ``"b4"``.
        broad_categories (bool, optional): If True, roll fine-grained
            classes into broad land-cover categories per the mode's
            preset. Defaults to ``False``.
        model_id (str, optional): HuggingFace checkpoint override. If
            None, the preset's default is used (only ``streetview`` has
            one; nadir modes require this).

    Returns:
        dict: Keys:

        - ``class_map`` (numpy.ndarray): ``(H, W)`` array of class IDs.
        - ``class_names`` (list): Class name for each ID.
        - ``colored_image`` (bytes): JPEG with colored overlay + legend.
        - ``legend`` (dict): ``{class_name: hex_color}`` for classes present.
        - ``summary`` (str): Markdown table of area percentages.
        - ``area_pct`` (dict): ``{class_name: float}`` area percentages.
        - ``metadata`` (dict): ``mode``, ``model_id``, ``model_variant``,
          ``orientation`` (ground/nadir), ``classes_count``, and
          ``broad_categories`` flag.

    Example:
        >>> pano = streetview_panorama(-111.80, 40.68, fov=360)
        >>> seg = segment_image(pano)                     # streetview default
        >>> sat = get_static_map(-111.80, 40.68, maptype="satellite", zoom=19)
        >>> seg2 = segment_image(sat, mode="aerial-urban",
        ...                        model_id="user/segformer-potsdam-b4")
        >>> print(seg['summary'])
    """
    _check_gmaps_ai_enabled("segment_image")
    import numpy as np
    from PIL import Image
    import io as _io

    # ── Pick preset ──
    if mode not in _SEGMENT_PRESETS:
        raise ValueError(
            f"Unknown mode {mode!r}. Valid modes: {sorted(_SEGMENT_PRESETS)}"
        )
    preset = _SEGMENT_PRESETS[mode]

    # ── Resolve model_id ──
    # Priority: explicit model_id arg > preset's model_id_template.
    if model_id is None:
        _tpl = preset.get("model_id_template")
        if _tpl is None:
            raise ValueError(
                f"mode={mode!r} has no built-in default checkpoint — pass "
                f"`model_id='user/checkpoint'` from a HuggingFace fine-tune "
                f"on the {mode.split('-', 1)[-1]} dataset. "
                f"Try searching HF for: '{preset['description'].split('.')[-2].strip()}'."
            )
        # ADE20K checkpoint uses different resolutions for B0-B4 vs B5.
        _res = "640-640" if model_variant == "b5" else "512-512"
        model_id = _tpl.format(variant=model_variant, res=_res)

    # ── Load model (per-model_id cache) ──
    global _segformer_model, _segformer_processor
    cached = _segformer_cache.get(model_id)
    if cached is None:
        from transformers import AutoImageProcessor, AutoModelForSemanticSegmentation
        import torch
        proc = AutoImageProcessor.from_pretrained(model_id)
        mdl = AutoModelForSemanticSegmentation.from_pretrained(model_id)
        mdl.eval()
        if torch.cuda.is_available():
            mdl = mdl.to("cuda")
        _segformer_cache[model_id] = (proc, mdl)
    else:
        proc, mdl = cached
    # Backward-compat globals — kept in sync with the last-loaded model
    # so older code that peeks at them still works.
    _segformer_processor = proc
    _segformer_model = mdl

    import torch

    # ── Inference ──
    image = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
    img_w, img_h = image.size

    inputs = proc(images=image, return_tensors="pt")
    if torch.cuda.is_available():
        inputs = {k: v.to("cuda") for k, v in inputs.items()}

    with torch.no_grad():
        outputs = mdl(**inputs)

    class_map = proc.post_process_semantic_segmentation(
        outputs, target_sizes=[(img_h, img_w)]
    )[0].cpu().numpy()

    # ── Build class name list and optional broad-category remapping ──
    _preset_classes = preset["classes"]
    _preset_broad = preset["broad_categories"]
    if broad_categories:
        _reverse = {}
        for cat, cls_names in _preset_broad.items():
            for name in cls_names:
                if name in _preset_classes:
                    _reverse[_preset_classes.index(name)] = cat
        broad_names = sorted(set(_preset_broad.keys()) | {"other"})
        broad_id_map = {name: i for i, name in enumerate(broad_names)}
        remapped = np.full_like(class_map, broad_id_map["other"])
        for src_idx, cat in _reverse.items():
            remapped[class_map == src_idx] = broad_id_map[cat]
        class_map = remapped
        class_names = broad_names
    else:
        class_names = _preset_classes

    # Calculate area percentages
    total_px = class_map.size
    unique, counts = np.unique(class_map, return_counts=True)
    area_pct = {}
    for cls_id, count in zip(unique, counts):
        if cls_id < len(class_names):
            name = class_names[cls_id]
            pct = (count / total_px) * 100
            if pct > 0.1:  # skip tiny classes
                area_pct[name] = round(pct, 1)

    # Sort by area descending
    area_pct = dict(sorted(area_pct.items(), key=lambda x: -x[1]))

    # Generate colored overlay
    # Broad-category palette — shared across all modes. Roll-up names
    # (impervious / vegetation / water / etc.) are common enough that
    # one palette is fine here.
    _CATEGORY_COLORS = {
        "sky":           (135, 206, 235),
        "vegetation":    ( 34, 139,  34),
        "impervious":    (128, 128, 128),
        "building":      (178, 102,  51),
        "vehicle":       (220,  20,  60),
        "water":         ( 30, 144, 255),
        "person":        (255, 165,   0),
        "terrain":       (139, 119, 101),
        "furniture":     (160,  82, 165),
        # Nadir broad-category rollups
        "urban":         ( 90,  90,  95),
        "agriculture":   (218, 165,  32),
        "forest":        ( 34, 139,  34),
        "bare":          (188, 143, 143),
        "other":         ( 80,  80,  80),
    }

    if not broad_categories and mode == "streetview":
        # Sensible colors for ADE20K 150 classes — keyed by class name
        _ADE20K_COLOR_MAP = {
            # Sky & atmosphere
            "sky": (135, 206, 250), "ceiling": (200, 200, 220),
            # Vegetation
            "tree": (34, 139, 34), "grass": (124, 252, 0), "plant": (0, 128, 0),
            "flower": (255, 105, 180), "palm": (50, 160, 50), "field": (144, 238, 144),
            "hill": (107, 142, 35),
            # Ground / impervious
            "road": (128, 128, 128), "sidewalk": (180, 180, 190),
            "floor": (190, 180, 170), "path": (160, 160, 150),
            "runway": (100, 100, 100), "dirt track": (139, 119, 101),
            "earth": (155, 118, 83), "sand": (238, 214, 175),
            "rock": (136, 138, 133), "land": (170, 140, 100),
            # Buildings & structures
            "building": (178, 102, 51), "house": (188, 120, 65),
            "wall": (190, 153, 107), "fence": (139, 90, 43),
            "skyscraper": (105, 105, 120), "tower": (120, 110, 130),
            "bridge": (150, 140, 130), "door": (120, 80, 50),
            "windowpane": (173, 216, 230), "stairs": (160, 150, 140),
            "railing": (110, 100, 90), "awning": (180, 160, 140),
            "canopy": (160, 180, 140),
            # Vehicles
            "car": (220, 20, 60), "bus": (255, 140, 0), "truck": (200, 60, 60),
            "van": (230, 100, 50), "boat": (65, 105, 225), "ship": (50, 80, 180),
            "airplane": (180, 180, 200), "bicycle": (255, 215, 0),
            "minibike": (255, 165, 0), "train": (160, 32, 240),
            # Water
            "water": (30, 144, 255), "sea": (0, 80, 160), "river": (50, 120, 200),
            "lake": (70, 130, 180), "swimming pool": (64, 164, 223),
            "fountain": (100, 180, 255), "waterfall": (80, 160, 220),
            # People & furniture
            "person": (255, 105, 0), "bench": (139, 90, 60),
            "chair": (160, 82, 45), "table": (139, 69, 19),
            "signboard": (255, 255, 100), "pole": (100, 100, 80),
            "streetlight": (255, 230, 130), "traffic light": (255, 60, 60),
            "lamp": (255, 240, 180), "flag": (200, 50, 50),
            # Terrain
            "mountain": (119, 136, 153), "countertop": (180, 170, 160),
            # Default
            "cabinet": (150, 130, 110), "bed": (180, 140, 160),
            "sofa": (160, 140, 130), "curtain": (180, 160, 180),
            "rug": (160, 120, 100), "mirror": (200, 220, 240),
        }
        _colors = np.zeros((150, 3), dtype=np.uint8)
        for i, name in enumerate(_ADE20K_CLASSES):
            _colors[i] = _ADE20K_COLOR_MAP.get(name, (
                # Fallback: deterministic color from class index
                int(80 + (i * 137) % 160),
                int(80 + (i * 89) % 160),
                int(80 + (i * 53) % 160),
            ))
    elif broad_categories:
        # Broad-category rollup — one palette shared across all modes.
        _colors = np.zeros((len(class_names), 3), dtype=np.uint8)
        for i, name in enumerate(class_names):
            _colors[i] = _CATEGORY_COLORS.get(name, (80, 80, 80))
    else:
        # Nadir mode, per-class output — use the preset's own palette.
        _preset_cm = preset.get("color_map") or {}
        _colors = np.zeros((len(class_names), 3), dtype=np.uint8)
        for i, name in enumerate(class_names):
            _colors[i] = _preset_cm.get(name, (
                int(80 + (i * 137) % 160),
                int(80 + (i * 89) % 160),
                int(80 + (i * 53) % 160),
            ))

    # Create colored mask
    color_mask = _colors[class_map]  # (H, W, 3)
    color_img = Image.fromarray(color_mask.astype(np.uint8), "RGB")

    # Blend with original image
    blended = Image.blend(image, color_img, alpha=0.45)

    # Add legend text
    from PIL import ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("arial.ttf", 13)
    except (OSError, IOError):
        try:
            font = ImageFont.load_default(size=13)
        except TypeError:
            font = ImageFont.load_default()

    # Build legend as a separate panel to the right of the image
    legend_w = 200
    row_h = 20
    n_entries = len(area_pct)
    legend_content_h = n_entries * row_h + 16

    legend_panel = Image.new("RGB", (legend_w, img_h), (20, 20, 20))
    ld = ImageDraw.Draw(legend_panel)

    ly = 8
    legend = {}
    for name, pct in area_pct.items():
        cls_id = class_names.index(name) if name in class_names else 0
        color = tuple(_colors[cls_id].tolist())
        legend[name] = "#{:02x}{:02x}{:02x}".format(*color)
        ld.rectangle([8, ly + 2, 22, ly + 14], fill=color)
        ld.text((28, ly), f"{name}: {pct:.1f}%", fill=(230, 230, 230), font=font)
        ly += row_h

    # Combine: image + legend panel side by side
    combined = Image.new("RGB", (img_w + legend_w, img_h), (20, 20, 20))
    combined.paste(blended, (0, 0))
    combined.paste(legend_panel, (img_w, 0))

    # Encode
    buf = _io.BytesIO()
    combined.save(buf, format="JPEG", quality=92)

    # Build summary table
    summary_lines = ["| Category | Area (%) |", "|---|---|"]
    for name, pct in area_pct.items():
        summary_lines.append(f"| {name} | {pct:.1f}% |")

    return {
        "class_map": class_map,
        "class_names": class_names,
        "colored_image": buf.getvalue(),
        "legend": legend,
        "summary": "\n".join(summary_lines),
        "area_pct": area_pct,
        "metadata": {
            "mode": mode,
            "model_id": model_id,
            "model_variant": model_variant,
            "orientation": preset["orientation"],
            "classes_count": len(class_names),
            "broad_categories": broad_categories,
        },
    }


def segment_streetview(
    lon: float,
    lat: float,
    heading: float = 0,
    fov: float = 360,
    pitch: float = 0,
    size: str = _SV_DEFAULT_SIZE,
    radius: int = 50,
    source: str = "default",
    model_variant: str = "b4",
    broad_categories: bool = True,
) -> dict[str, Any] | None:
    """Fetch a Street View panorama and segment it with SegFormer.

    Convenience wrapper that combines :func:`streetview_panorama` and
    :func:`segment_image`.

    Args:
        lon, lat: Coordinates.
        heading, fov, pitch, size, radius, source: See
            :func:`streetview_panorama`.
        model_variant (str, optional): SegFormer size. Defaults to ``"b4"``.
        broad_categories (bool, optional): Merge into land cover categories.
            Defaults to ``True``.

    Returns:
        dict or None: Same as :func:`segment_image` plus ``original``
        (raw panorama bytes) and ``location`` (address string).
        Returns ``None`` if no Street View coverage.

    Example:
        >>> result = segment_streetview(-111.80, 40.68, fov=360)
        >>> if result:
        ...     print(result['summary'])
        ...     with open("segmented.jpg", "wb") as f:
        ...         f.write(result['colored_image'])
    """
    pano = streetview_panorama(
        lon, lat, heading=heading, fov=fov, pitch=pitch,
        size=size, radius=radius, source=source,
    )
    if pano is None:
        return None

    result = segment_image(pano, model_variant=model_variant,
                           broad_categories=broad_categories)

    # Add location info
    addr = reverse_geocode(lon, lat)
    result["original"] = pano
    result["location"] = addr.get("formatted_address", "") if addr else f"({lat:.4f}, {lon:.4f})"

    return result


# ── inventory_area re-export ─────────────────────────────────────────────
# The rigorous image-inventory function lives in geeViz.inventoryLib for
# organizational reasons. Re-exported here so the agent's usual
# `gm.inventory_area(...)` call works.
try:
    from geeViz.inventoryLib import inventory_area  # noqa: F401
except ImportError:
    # inventoryLib not shipped or missing optional dep — leave gm without it
    pass
