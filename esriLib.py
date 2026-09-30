"""
ArcGIS / Esri REST services client for geeViz. **DEPRECATED.**

.. deprecated::
   Use :mod:`georest` instead. This module now delegates to it and will
   be removed in a future release.

   ``georest`` is the maintained implementation of everything here that
   talks to an Esri REST endpoint, it is stdlib-only (no runtime
   dependencies), and it does considerably more than this module ever
   did — ``exportImage``, ``identifyPixelValue``, ``getSamples``,
   ``computeStatisticsHistograms``, ``queryBoundary``, and a full client
   for the USFS Enterprise Data Warehouse.

   The move, function by function::

       geeViz.esriLib.searchPortal        -> georest.restesri.portal.searchPortal
       geeViz.esriLib.getServiceMetadata  -> georest.restesri.portal.getServiceMetadata

   The signatures match, so the change is the import line.

   The ``addEsri*Service`` functions are NOT going to georest: they add
   layers to a geeViz ``Map``, which is geeViz's concern, not a REST
   client's. They stay available here (and as ``Map.addEsri*``).

   **This module is a patch over georest, not a fork of it.** What is
   left here is only what georest should not have:

   * the geeViz ``Map`` calls -- ``addLayer``, ``addTileLayer``,
     ``addDynamicMapService`` -- and the naming and viz-key handling
     around them;
   * :func:`geeViz._ssrf.check_url`, which is this package's policy and
     has to be applied on THIS side of every delegated call, because
     georest has none;
   * the exception contract callers already depend on: georest reports
     an unreachable host or a bad status as ``RuntimeError``, and this
     module has always raised ``ConnectionError``, so it translates at
     the boundary rather than rewriting what callers catch.

   Everything else delegates. ``_resolve_portal``,
   ``_detect_service_type`` and ``_resolve_url`` were byte-identical
   copies of georest's and now call them; :data:`PORTALS` is georest's
   own dict rather than a copy, so a name added at runtime through
   either module resolves in both; a plain feature-service draw (no
   thinning, ``max_features`` within one server response) is one call to
   ``georest.restesri.services.queryFeatureService``; the ``{z}/{y}/{x}``
   tile template comes from ``getImageServiceTileUrl``; and every other
   request goes out through georest's ``_http.fetch_json``.

   **What georest 0.3.0 does not do, and so still lives here** (each
   marked ``LOCAL PATCH`` where it is added, and small enough to move
   into georest later):

   * a retry ladder -- georest makes one attempt, and hazards.fema.gov
     resets about a quarter of TLS handshakes, in bursts;
   * ``maxAllowableOffset`` (server-side thinning), which
     ``queryFeatureService`` has no parameter for;
   * paging past a layer's ``maxRecordCount`` -- ``queryFeatureService``
     returns the first page of a truncated answer without saying so, and
     its ``f=json`` fallback drops the ``exceededTransferLimit`` flag, so
     the count is taken here and compared with what came back;
   * renaming popup fields, ``visible``, and the one-fetch classed draw,
     which are geeViz map concerns rather than REST ones.

Bridges three Esri service types into the existing geeViz viewer with no
JavaScript changes required.  The viewer already supports both
``tileMapService`` (for raster tiles) and ``geoJSONVector`` (for vector
features) layer types.

======================================  ==================================================================================
Service type                            Mechanism
======================================  ==================================================================================
Image Service                           ``Map.addTileLayer("<url>/tile/{z}/{y}/{x}")``
Map Service (cached)                    ``Map.addTileLayer(...)`` — same tile path
Feature Service (≤ ``max_features``)    Fetch ``<url>/query?f=geojson`` → ``Map.addLayer(geojson_dict)``
Feature Service (> ``max_features``)    ``ValueError`` with remediation message
======================================  ==================================================================================

**Public API** — 7 functions + 1 constant::

    import geeViz.esriLib as el

    # Discover data on any ArcGIS Portal
    results = el.searchPortal("naip 2023")                  # IIPP (default)
    results = el.searchPortal("naip 2023", portal="agol")   # ArcGIS Online
    results = el.searchPortal("naip 2023",
                              portal="https://myagency.gov/portal")

    # Available portals
    el.PORTALS.keys()   # iipp, agol, usgs, noaa, usfs, nasa

    # Inspect any service
    meta = el.getServiceMetadata("https://.../ImageServer")

    # Add to the geeViz map (auto-dispatches by service type)
    el.addEsriService(result_or_url)

    # Or call the typed helpers directly
    el.addEsriImageService("https://.../ImageServer", name="NAIP 2023")
    el.addEsriFeatureService("https://.../FeatureServer/0",
                             max_features=2000, where="STATE='UT'")
    el.addEsriMapService("https://.../MapServer")

Token-gated portals::

    # Obtain a token first:
    #   POST <portal>/sharing/rest/generateToken
    #     username=...&password=...&client=requestip&expiration=60&f=json
    token = "..."
    el.searchPortal("classified data", token=token)
    el.addEsriFeatureService(url, token=token)

Copyright 2026 Ian Housman

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0
"""

from __future__ import annotations

import json
import warnings
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from geeViz._ssrf import check_url as _check_url  # noqa: E402

# ---------------------------------------------------------------------------
# Known public portals
# ---------------------------------------------------------------------------

#: THE SAME OBJECT georest uses, not a copy of it.
#:
#: This dict is documented below as runtime-editable, and
#: ``_resolve_portal`` now delegates to georest -- so a copy would
#: mean ``esriLib.PORTALS["mine"] = ...`` was accepted and then
#: silently ignored, because the lookup happens against the other
#: dict. Aliasing keeps one source of truth and makes an edit
#: through either name work.
from georest.restesri.portal import PORTALS  # noqa: E402

# LOCAL PATCH georest fallback v1 (2026-09-30): georest 0.3.0's
# queryFeatureService, when its f=geojson request fails, re-asks in f=json
# and converts client-side with services._esri_geometry_to_geojson - which
# hands Esri's flat ring list straight to one GeoJSON Polygon, so the
# separate parts of a multipart polygon become HOLES in its first part, and
# services._sanitize_geojson overwrites every feature id with "0".."n". The
# same package already has the right versions in edw.py (winding-aware
# rings, ids filled from OBJECTID and never overwritten). Point the fallback
# at those. Remove when georest's services.py uses them itself (reported to
# Ryan). A FEMA handshake reset is enough to take this path.
try:
    from georest.restesri import edw as _gr_edw, services as _gr_services
    if hasattr(_gr_edw, "_esri_rings_to_geojson"):
        _gr_services._esri_geometry_to_geojson = _gr_edw._esri_geometry_to_geojson
        _gr_services._sanitize_geojson = _gr_edw._sanitize_geojson
except ImportError:                              # pragma: no cover
    pass
"""Module-level dict mapping short names to portal base URLs.

Add your own at runtime::

    from geeViz.esriLib import PORTALS
    PORTALS["myagency"] = "https://gis.myagency.gov/portal"
"""

# Non-data item types that clutter portal search results.  Applied when
# data_only=True (the default).  This mirrors the exclusion list used by
# the IIPP search UI.
_DATA_ONLY_EXCLUSIONS: list[str] = [
    "Style",
    "Layer",
    "Map Document",
    "Map Package",
    "Basemap",
    "Mobile Basemap Package",
    "Web Scene",
    "CityEngine Web Scene",
    "Pro Map",
    "Project Package",
    "Task File",
    "Operations Dashboard Add In",
    "Application",
    "Web Mapping Application",
    "Mobile Application",
    "Code Sample",
    "Symbol Set",
    "Color Set",
    "Windows Viewer Add In",
    "Windows Viewer Configuration",
    "Map Area",
    "Insights Workbook",
    "Insights Page",
    "Insights Model",
    "Hub Initiative",
    "Hub Site Application",
    "Hub Page",
    "Hub Project",
    "Experience Builder Widget",
    "Dashboard",
    "StoryMap",
    "Survey123 Add In",
    "Compact Tile Package",
]

# ---------------------------------------------------------------------------
# HTTP helpers (no third-party dependencies — stdlib only)
# ---------------------------------------------------------------------------

# LOCAL PATCH retry ladder v2 (2026-09-21): a SUCCESSFUL city-scale count
# over FEMA NFHL took 18 s (measured 2026-09-21). Timing that out turns a
# slow layer into a missing one. 45 s is what esri_paging already allows the
# data path. Passed to georest per call (its own default is 60 s).
_TIMEOUT = 45  # seconds

#: LOCAL PATCH (2026-09-01) + retry ladder v2 (2026-09-21): the waits between
#: attempts. georest makes ONE attempt. Measured on hazards.fema.gov: 2 of 8
#: TLS handshakes reset (WinError 10054), in bursts - 24 attempts one second
#: apart had 7 successes and a longest run of 7 failures spanning 7.7 s. Five
#: attempts with 1, 2, 4, 8 s between them outlast that burst; three with
#: 1 + 2 s gave up inside it, and since a reset comes back in 0.1 s they were
#: not waiting out anything.
_RETRY_DELAYS = (1, 2, 4, 8)

#: HTTP statuses worth another attempt. Anything else (400, 403, 404) is an
#: answer rather than a hiccup, and retrying it only delays the error.
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)


def _is_transient(exc: BaseException | None) -> bool:
    """Would another attempt plausibly succeed?

    georest reports a bad HTTP status or an unreachable host as
    ``RuntimeError`` raised FROM the urllib error -- sometimes wrapped
    twice, since its count pre-flight re-raises with the URL attached -- so
    the verdict is on the cause chain. A reset or timeout while READING a
    response is not caught by georest at all and arrives here raw. Anything
    else, ``ValueError`` above all (an Esri error body, a non-JSON body, the
    overflow guard, the SSRF guard), is not transient.
    """
    for _ in range(6):
        if exc is None:
            return False
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in _TRANSIENT_STATUSES
        if isinstance(exc, (urllib.error.URLError, ConnectionResetError,
                            TimeoutError)):
            return True
        if not isinstance(exc, RuntimeError):
            return False
        exc = exc.__cause__
    return False


def _with_retries(call):
    """``call()``, retried on transient failures along :data:`_RETRY_DELAYS`.

    Wraps a georest call rather than reimplementing its request: what is
    added is only the ladder. Non-transient failures raise at once.
    """
    last: BaseException | None = None
    for delay in (0,) + _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return call()
        except Exception as exc:                          # noqa: BLE001
            if not _is_transient(exc):
                raise
            last = exc
    raise last


#: Functions already warned about, so a loop calling one does not emit
#: the same notice a thousand times. A deprecation is a message to the
#: person reading the code, not a running cost.
_WARNED: set[str] = set()


def _deprecated(name: str, replacement: str) -> None:
    """Warn once that ``name`` has moved to ``replacement``.

    ``DeprecationWarning`` is hidden by default in scripts, which is
    right: this must not spam a notebook that happens to call a geeViz
    map helper. Anyone running with ``-W default`` or pytest sees it.
    """
    if name in _WARNED:
        return
    _WARNED.add(name)
    warnings.warn(
        f"geeViz.esriLib.{name} is deprecated and now delegates to "
        f"{replacement}. geeViz.esriLib will be removed in a future "
        f"release; import georest directly.",
        DeprecationWarning,
        stacklevel=3,
    )


def _fetch_json(url: str, params: dict | None = None) -> dict:
    """GET a URL and return parsed JSON, via :mod:`georest`.

    The request itself — urlopen, the JSON decode with the response body
    in the error — is georest's, and georest is the maintained copy.
    georest 0.3.0 does NOT retry, so the retry the 2026-09-01 patch added
    stays here, as a wrapper around georest's call rather than a copy of
    it. Every request the ``addEsri*`` helpers make that is not a
    georest high-level call comes through here, so the SSRF guard and
    the ladder cover all of them.

    Raises ``ConnectionError`` on network failure (after the retry
    ladder), ``ValueError`` on a non-JSON response.

    LOCAL PATCH retry ladder v2 (2026-09-21): georest makes one attempt;
    this makes up to five, on transient failures only (see
    :data:`_RETRY_DELAYS`), with the booth's 45 s timeout.
    """
    from georest.restesri import _http as _gh

    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    # _check_url stays on this side: it is geeViz's SSRF guard, and the
    # url is fully built by the time it runs.
    _check_url(url)
    try:
        return _with_retries(lambda: _gh.fetch_json(url, timeout=_TIMEOUT))
    except ValueError:
        # A non-JSON body. Same meaning on both sides — pass it through
        # rather than flattening it into the network case.
        raise
    except RuntimeError as exc:
        # georest reports an unreachable host or a bad HTTP status as
        # RuntimeError; this module has always documented and raised
        # ConnectionError, and callers catch that. Delegation must not
        # silently change which exception a caller has to handle, so
        # translate at the boundary rather than rewriting the contract
        # of a module people already depend on.
        raise ConnectionError(str(exc)) from exc
    except OSError as exc:
        # urllib's URLError, and a reset or timeout while READING the body,
        # which georest does not catch and so arrives raw.
        raise ConnectionError(str(exc)) from exc


def _build_params(base: dict, token: str | None) -> dict:
    """Merge ``token`` into a params dict if supplied. Delegates."""
    from georest.restesri import _http as _gh
    return _gh.build_params(base, token)


def _resolve_portal(portal: str) -> str:
    """Resolve a portal short name or URL to a base URL. Delegates.

    The body was a byte-identical copy of georest's, which is how the
    two would have drifted. :data:`PORTALS` is aliased to georest's own
    dict above, so a name added at runtime resolves here too.
    """
    from georest.restesri import portal as _gp
    return _gp._resolve_portal(portal)

def searchPortal(
    query: str,
    portal: str = "iipp",
    limit: int = 20,
    data_only: bool = True,
    raw_q: str | None = None,
    token: str | None = None,
    **filters: Any,
) -> list[dict[str, Any]]:
    """Search any ArcGIS Portal for hosted services.

    Uses the standard ``/sharing/rest/search`` endpoint present on ArcGIS
    Online, IIPP, and any ArcGIS Enterprise install.

    Args:
        query (str): Free-text search query (e.g. ``"naip 2023"``,
            ``"fire perimeter"``).
        portal (str, optional): Either a short name from :data:`PORTALS`
            (``"iipp"``, ``"agol"``, ``"usgs"``, ``"noaa"``, ``"usfs"``,
            ``"nasa"``) or a full portal base URL.  Defaults to ``"iipp"``.
        limit (int, optional): Maximum results to return (1–100).
            Defaults to 20.
        data_only (bool, optional): When ``True`` (default), appends a
            bundled exclusion list that filters out non-data items (styles,
            web apps, dashboards, etc.) so results are datasets only.
            Set to ``False`` to search without restrictions.
        raw_q (str, optional): If supplied, overrides the assembled query
            string entirely — ignores ``query``, ``data_only``, and
            ``filters``.  Use for portal query DSL power users.
        token (str, optional): ArcGIS token for secured portals.  Omit for
            public services.  Obtain via
            ``POST <portal>/sharing/rest/generateToken``.
        **filters: Extra ArcGIS search filters forwarded verbatim as query
            params (e.g. ``sortField="title"``, ``sortOrder="asc"``,
            ``bbox="-120,35,-110,42"``).

    Returns:
        list of dict: Parsed portal items.  Each dict includes:

        - ``id`` (str): Item ID.
        - ``title`` (str): Item title.
        - ``type`` (str): Esri item type (e.g. ``"Image Service"``,
          ``"Feature Service"``).
        - ``snippet`` (str): Short description.
        - ``tags`` (list of str): Associated tags.
        - ``url`` (str): Service endpoint URL (may be ``""`` if not set).
        - ``owner`` (str): Portal username of the owner.
        - ``created`` (int): Unix timestamp (ms) of item creation.
        - ``modified`` (int): Unix timestamp (ms) of last modification.
        - ``thumbnail`` (str or None): Thumbnail URL, or ``None`` if absent.
        - ``_raw`` (dict): Full raw portal item dict for advanced access.

    Example::

        import geeViz.esriLib as el

        # Search IIPP for NAIP imagery (default portal)
        results = el.searchPortal("naip 2023", limit=10)
        for r in results:
            print(r["title"], r["type"], r["url"])

        # ArcGIS Online
        results = el.searchPortal("wildfire perimeter", portal="agol")

        # Custom Enterprise portal
        results = el.searchPortal("hydrology",
                                  portal="https://gis.mystate.gov/portal")

        # Raw portal query DSL (bypasses data_only and filters)
        results = el.searchPortal("", raw_q='type:"Feature Service" owner:USGS')
    """
    _deprecated("searchPortal", "georest.restesri.portal.searchPortal")
    from georest.restesri import portal as _gp
    return _gp.searchPortal(
        query, portal=portal, limit=limit, data_only=data_only,
        raw_q=raw_q, token=token, **filters)


def getServiceMetadata(url: str, token: str | None = None) -> dict[str, Any]:
    """Fetch and return the JSON metadata for any ArcGIS REST service.

    Appends ``?f=json`` to the URL and returns the parsed response.  Works
    for ImageServer, FeatureServer, MapServer, and any sub-layer URL
    (e.g. ``/FeatureServer/0``).

    Args:
        url (str): ArcGIS service endpoint, e.g.::

            "https://naip.services.arcgis.com/.../ImageServer"
            "https://services.arcgis.com/.../FeatureServer/0"
            "https://server.arcgisonline.com/.../MapServer"

        token (str, optional): ArcGIS token for secured services.

    Returns:
        dict: Parsed service metadata.  Common keys vary by service type:

        - ``name`` (str): Service name.
        - ``type`` (str): Layer geometry type (Feature Services).
        - ``fields`` (list): Schema fields (Feature Services).
        - ``extent`` (dict): Spatial extent.
        - ``spatialReference`` (dict): Spatial reference info.
        - ``minScale``, ``maxScale`` (int): Scale range.
        - ``capabilities`` (str): Comma-separated capabilities string.

    Raises:
        ConnectionError: If the URL is unreachable.
        ValueError: If the response is not valid JSON.

    Example::

        import geeViz.esriLib as el

        meta = el.getServiceMetadata("https://.../ImageServer")
        print(meta["name"])
        print(meta["extent"])

        # FeatureServer layer 0
        meta = el.getServiceMetadata("https://.../FeatureServer/0")
        print([f["name"] for f in meta.get("fields", [])])
    """
    _deprecated("getServiceMetadata",
                "georest.restesri.portal.getServiceMetadata")
    from georest.restesri import portal as _gp
    _check_url(url)
    try:
        return _gp.getServiceMetadata(url, token=token)
    except (RuntimeError, urllib.error.URLError) as exc:
        # Same translation as _fetch_json, and for the same reason: this
        # function has always raised ConnectionError for an unreachable
        # service, and delegating must not change what a caller catches.
        raise ConnectionError(str(exc)) from exc


def _detect_service_type(url: str, meta: dict | None = None) -> str:
    """Return the ArcGIS service type for *url*. Delegates.

    ``"ImageServer"``, ``"FeatureServer"``, ``"MapServer"`` or
    ``"Unknown"``. The body was a byte-identical copy of georest's --
    URL-segment match first, then metadata keys, then the ``fields`` /
    ``bandCount`` shape sniff.
    """
    from georest.restesri import portal as _gp
    return _gp._detect_service_type(url, meta)

def _resolve_url(url_or_result: str | dict) -> str:
    """A service URL from a string or a ``searchPortal`` result. Delegates.

    Another byte-identical copy, raising the same ``TypeError`` for a
    non-string/dict and ``ValueError`` for a result with no ``url``.
    """
    from georest.restesri import portal as _gp
    return _gp._resolve_url(url_or_result)

def addEsriImageService(
    url_or_result: str | dict,
    viz_params: dict | None = None,
    name: str | None = None,
    token: str | None = None,
    target_map=None
) -> None:
    """Add an ArcGIS Image Service as an XYZ tile layer to the geeViz map.

    Constructs the ArcGIS tile URL pattern
    ``<service_url>/tile/{z}/{y}/{x}`` and calls
    ``geeViz.geeView.Map.addTileLayer``.

    .. note::
        ArcGIS tile URLs use ``{z}/{y}/{x}`` order (y before x), not the
        XYZ standard ``{z}/{x}/{y}``.  This function emits the correct
        ArcGIS order automatically.

    Args:
        url_or_result (str or dict): Either:

            - A bare service URL, e.g.
              ``"https://naip.services.arcgis.com/.../ImageServer"``
            - A :func:`searchPortal` result dict (the ``"url"`` key is used).

        viz_params (dict, optional): Forwarded to ``addTileLayer`` as
            keyword arguments.  Supported keys: ``opacity`` (float),
            ``visible`` (bool), ``max_zoom`` (int).
        name (str, optional): Layer name shown in the geeViz layer list.
            Defaults to the last segment of the service URL.
        token (str, optional): ArcGIS token appended to tile requests as
            ``?token=<>``.

    Example::

        import geeViz.esriLib as el
        import geeViz.geeView as gv

        el.addEsriImageService(
            "https://naip.services.arcgis.com/.../ImageServer",
            name="NAIP 2022",
            viz_params={"opacity": 0.85},
        )
        gv.Map.centerObject(gv.ee.Geometry.Point([-111.89, 40.77]), 12)
        gv.Map.view()
    """
    import geeViz.geeView as gv

    url = _resolve_url(url_or_result)
    if name is None:
        name = url.rstrip("/").split("/")[-2] if url.endswith(("ImageServer", "imageserver")) else url.rstrip("/").split("/")[-1]

    # The {z}/{y}/{x} template -- ArcGIS order, y before x, not the XYZ
    # standard -- and the token quoting are georest's. The body here was
    # identical to it line for line, which is the kind of copy that gets
    # a fix in one place and not the other.
    from georest.restesri import services as _gs
    tile_url = _gs.getImageServiceTileUrl(url, token=token)

    kw: dict[str, Any] = {}
    if viz_params:
        if "opacity" in viz_params:
            kw["opacity"] = float(viz_params["opacity"])
        if "visible" in viz_params:
            kw["visible"] = bool(viz_params["visible"])
        if "max_zoom" in viz_params:
            kw["max_zoom"] = int(viz_params["max_zoom"])

    print(f"Adding Esri Image Service: {name}")
    (target_map or gv.Map).addTileLayer(tile_url, name=name, **kw)


# ---------------------------------------------------------------------------
# addEsriMapService
# ---------------------------------------------------------------------------

def addEsriMapService(
    url_or_result: str | dict,
    name: str | None = None,
    token: str | None = None,
    viz_params: dict | None = None,
    target_map=None,
) -> None:
    """Add a cached ArcGIS Map Service as an XYZ tile layer to the geeViz map.

    Cached Map Services expose the same ``/tile/{z}/{y}/{x}`` tile endpoint
    as Image Services and are handled identically.  Dynamic (non-cached) Map
    Services do not serve tiles this way; for those, use
    :func:`addEsriFeatureService` on the individual sub-layer.

    Args:
        url_or_result (str or dict): Service URL or :func:`searchPortal`
            result dict.
        name (str, optional): Layer name.  Defaults to last URL segment.
        token (str, optional): ArcGIS token for secured services.
        viz_params (dict, optional): ``opacity``, ``visible``, ``max_zoom``.

    Example::

        import geeViz.esriLib as el

        el.addEsriMapService(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer",
            name="ESRI World Imagery",
        )
    """
    # ── Preflight: cached or dynamic? Route accordingly. ──
    # CACHED MapServers (``singleFusedMapCache: true``) expose
    # ``/tile/{z}/{y}/{x}`` — same shape as an ImageServer, handled by
    # addEsriImageService below.
    # DYNAMIC MapServers (``singleFusedMapCache: false``) don't serve
    # pre-rendered tiles; they respond to ``/export?bbox=…&f=image``.
    # Route those through ``gv.Map.addDynamicMapService``, which
    # bridges to the viewer's ``addDynamicToMap`` code path (Google
    # Maps GroundOverlay per viewport). Real incident 2026-07-30: FEMA
    # NFHL is dynamic; passing it to addEsriMapService without this
    # branch broke the map silently.
    import geeViz.geeView as _gv
    url = _resolve_url(url_or_result)
    try:
        _meta = getServiceMetadata(url, token=token)
    except Exception as _meta_err:
        # Metadata fetch failed — could be a bad URL, an auth wall, or
        # a transient network hiccup. Print a warning so the agent (and
        # `testLayers` output) sees WHY we can't detect cached-vs-dynamic
        # and can suggest a fix; still fall through to the tile path in
        # case the caller knows the service IS cached.
        print(
            f"WARNING: addEsriMapService could not fetch service metadata for "
            f"{url!r} ({_meta_err}). Proceeding as if cached; if the layer "
            f"fails to render, verify the URL (a common issue is a wrong "
            f"prefix like '/gis/...' vs '/arcgis/rest/services/...')."
        )
        _meta = None
    if _meta is not None and _meta.get("singleFusedMapCache") is False:
        _name = name or url.rstrip("/").split("/")[-2]
        print(f"Adding Dynamic Esri Map Service: {_name}  ({url})")
        (target_map or _gv.Map).addDynamicMapService(
            url,
            name=_name,
            visible=(viz_params or {}).get("visible", True),
            token=token,
        )
        return
    # Cached — same tile URL shape as ImageServer
    addEsriImageService(url_or_result, viz_params=viz_params, name=name, token=token, target_map=target_map)


# ---------------------------------------------------------------------------
# addEsriFeatureService
# ---------------------------------------------------------------------------

_FEATURE_QUERY_SUFFIX = "/query"

#: How many features one ArcGIS response is trusted to hold without asking
#: the layer: 1,000 is the lowest DEFAULT ``maxRecordCount`` ArcGIS ships
#: (map services; feature services default to 2,000). Two things use it:
#:
#: * a draw capped at this many features, with no thinning, is handed to
#:   georest's one-shot ``queryFeatureService`` -- the plain case georest
#:   was built for;
#: * a larger count looks up the layer's page size first, so a page that
#:   paging would only discard is never fetched (paging v3).
#:
#: Neither is an assumption about correctness. A service configured with a
#: smaller page still gets caught by the count check after the fetch and
#: paged; it just costs one request more.
_ONE_RESPONSE = 1000

# ---------------------------------------------------------------------------
# LOCAL PATCH esri paging v1 (2026-09-21)
# ---------------------------------------------------------------------------
#: Hard stop on the paging loop. 200 pages at a 2,000 maxRecordCount is
#: 400,000 features - far past anything this viewer can draw, so reaching it
#: means the server is misbehaving and the loop must not run forever.
_MAX_PAGES = 200

#: LOCAL PATCH esri paging v2 (2026-09-21): how many pages of one
#: layer to fetch at the same time. Four, not more: the gain is in
#: not waiting on round trips, and hazards.fema.gov already resets
#: about a third of our handshakes without being crowded.
_PAGE_WORKERS = 4


def _exceeded_transfer(payload: dict) -> bool:
    """True when ArcGIS says it withheld rows.

    The flag sits at the top level of a GeoJSON response and under
    ``properties`` in the Esri JSON one. Both shapes reach here, because
    ``f=geojson`` is not honoured by every service version.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("exceededTransferLimit"):
        return True
    props = payload.get("properties")
    return bool(isinstance(props, dict) and props.get("exceededTransferLimit"))


#: LOCAL PATCH esri paging v3 (2026-09-21): one layer's metadata, fetched once.
#: A classed source draws one sub-layer per class - six, for FEMA flood
#: zones - and every one of them was asking the SAME layer the same
#: question. Keyed on the URL and whether a token was used, never on the
#: token itself.
_CAPS_CACHE: dict = {}


def _paging_caps(layer_url: str, token: str | None) -> dict:
    """``{"paginates", "orders", "oid", "max_record"}`` for a layer.

    ``objectIdField`` is present on FeatureServer layers and often absent on
    MapServer ones, so the OID field is also looked for by TYPE. Without an
    OID neither paging strategy is safe and the caller reports partial.

    ``max_record`` is the server's own page size, which lets the caller skip
    a fetch it would only discard - see LOCAL PATCH esri paging v3 (2026-09-21).

    Read raw rather than through georest's ``getLayerInfo``: that fills a
    missing ``maxRecordCount`` with 2,000, and "the layer did not say" has
    to stay distinguishable from "the layer said 2,000".
    """
    cache_key = (layer_url, bool(token))
    if cache_key in _CAPS_CACHE:
        return _CAPS_CACHE[cache_key]
    caps = {"paginates": False, "orders": True, "oid": None,
            "max_record": None}
    try:
        meta = _fetch_json(layer_url, _build_params({"f": "json"}, token))
    except Exception:                                    # noqa: BLE001
        return caps
    if not isinstance(meta, dict) or "error" in meta:
        return caps
    adv = meta.get("advancedQueryCapabilities") or {}
    caps["paginates"] = bool(adv.get("supportsPagination"))
    caps["orders"] = bool(adv.get("supportsOrderBy", True))
    oid = meta.get("objectIdField")
    if not oid:
        for field in meta.get("fields") or []:
            if (field or {}).get("type") == "esriFieldTypeOID":
                oid = field.get("name")
                break
    caps["oid"] = oid
    try:
        caps["max_record"] = int(meta.get("maxRecordCount") or 0) or None
    except (TypeError, ValueError):
        caps["max_record"] = None
    _CAPS_CACHE[cache_key] = caps
    return caps


def _feature_id(feat: dict, oid: str | None):
    """A feature's stable identity, or None when it has none.

    GeoJSON from ArcGIS carries the OID as ``id``; when ``outFields`` brought
    the field back it is in ``properties`` too. Either will do - what matters
    is that repeated rows can be recognised, because a server that ignores
    the paging parameters answers every page identically.
    """
    fid = feat.get("id")
    if fid is None and oid:
        fid = (feat.get("properties") or {}).get(oid)
    return fid


def _page_features(query_url: str, params: dict, matched: int,
                   layer_url: str, token: str | None, first_page: list) -> list:
    """Every feature the query matches, or as many as can be paged safely.

    Returns ``first_page`` unchanged when no safe strategy exists - the
    caller then names the layer as partial rather than drawing a slice that
    looks whole.
    """
    caps = _paging_caps(layer_url, token)
    oid = caps["oid"]
    if not oid or not caps["orders"]:
        return first_page
    strategy = "offset" if caps["paginates"] else "oid_window"

    # The first page was fetched with NO orderByFields, so its row order is
    # whatever the server felt like. Offsetting into a different order skips
    # and repeats rows, so that page is thrown away and paging restarts at 0
    # under an explicit ORDER BY. One wasted request buys a correct layer.
    base_where = params.get("where") or "1=1"
    features: list = []
    seen: set = set()
    last = None

    def _harvest(rows) -> int:
        """Add the rows that are new. Returns how many, or -1 when the rows
        carry no identity - a server ignoring the paging parameters cannot
        be told from one honouring them, so that case must stop the loop
        rather than collect duplicates as if complete."""
        nonlocal last
        added = 0
        for feat in rows:
            fid = _feature_id(feat, oid)
            if fid is None:
                return -1
            if fid in seen:
                continue
            seen.add(fid)
            last = fid
            features.append(feat)
            added += 1
        return added

    def _page(extra: dict):
        page = dict(params)
        page["orderByFields"] = oid
        page.update(extra)
        try:
            got = _fetch_json(query_url, page)
        except Exception:                                # noqa: BLE001
            return None
        if not isinstance(got, dict) or "error" in got:
            return None
        return got.get("features") or []

    # LOCAL PATCH esri paging v2 (2026-09-21): the pages are independent
    # under offset paging, and waiting for each in turn was the whole cost -
    # measured over the Houston urban area, 16 geometry pages took 62.9 s of
    # a 71.5 s draw, about 4 s per request whatever its size. Once the first
    # page has shown how big a page is, the remaining offsets are arithmetic
    # and can be fetched at the same time. Four at a time: the point is to
    # stop waiting on round trips, not to hammer a federal server that
    # already resets about a third of our handshakes.
    # The first page carries `resultOffset` only under offset paging. A
    # layer without `supportsPagination` IGNORES the parameter, and sending
    # it there would hide that fact from anyone reading the requests.
    rows = _page({"resultOffset": "0"} if strategy == "offset" else {})
    if not rows:
        return first_page
    page_size = len(rows)
    if _harvest(rows) < 0:
        return features or first_page

    if strategy == "offset" and page_size and len(features) < matched:
        offsets = list(range(page_size, min(matched, page_size * _MAX_PAGES),
                             page_size))
        if offsets:
            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=_PAGE_WORKERS) as pool:
                    for got in pool.map(
                            lambda off: _page({"resultOffset": str(off)}),
                            offsets):
                        if not got:
                            continue
                        if _harvest(got) < 0:
                            break
            except Exception:                            # noqa: BLE001
                # Threads unavailable or the pool blew up: fall through to
                # the sequential loop below, which finishes the job slowly
                # rather than returning a layer that looks whole.
                pass

    # Sequential finish. It completes an OID-window layer, and it also picks
    # up anything the parallel pass missed - a page that failed every retry
    # leaves a hole, and a hole is exactly what must not be drawn as whole.
    for _ in range(_MAX_PAGES):
        if len(features) >= matched:
            break
        if strategy == "offset":
            extra = {"resultOffset": str(len(features))}
        elif last is not None:
            extra = {"where": f"({base_where}) AND {oid} > {last}"}
        else:
            extra = {}
        rows = _page(extra)
        if not rows:
            break
        added = _harvest(rows)
        if added <= 0:
            break
    # Paging that went backwards is a bug, not an improvement.
    return features if len(features) >= len(first_page) else first_page


def _bbox_params(bbox: str | None) -> dict:
    """The envelope filter, or ``{}``.

    LOCAL PATCH (2026-08-28): area filter. Applied to the COUNT as well as
    the fetch, so max_features guards the area asked about rather than the
    whole layer - FEMA NFHL is 5.8M features nationally, 52 in a 2 km box.
    """
    if not bbox:
        return {}
    return {"geometry": bbox, "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}


def _count_features(query_url: str, where: str, bbox_params: dict,
                    token: str | None, max_features: int) -> int:
    """How many features the draw will ask for, refused above ``max_features``.

    The count georest's ``queryFeatureService`` takes is internal to it and
    never returned. This one is kept, because it is the only way to tell a
    whole layer from the first page of one: ``exceededTransferLimit`` is
    not always sent, and georest's ``f=json`` fallback drops it.
    """
    from georest.restesri import _http as _gh

    params: dict[str, Any] = {"where": where, "returnCountOnly": "true",
                              "f": "json"}
    params.update(bbox_params)
    if token:
        params["token"] = token
    try:
        resp = _fetch_json(query_url, params)
    except ConnectionError as exc:
        raise ConnectionError(
            f"Could not reach Feature Service at {query_url!r}: {exc}"
        ) from exc
    if "error" in resp:
        raise ValueError(f"Feature Service returned an error: "
                         f"{_gh.format_esri_error(resp['error'])}")
    count = resp.get("count", 0)
    if count > max_features:
        # georest's wording, so the message does not depend on which path
        # the draw took. It names only remedies that exist.
        raise ValueError(
            f"Service layer has {count:,} matching features "
            f"(max_features={max_features:,}).\n"
            f"Increase max_features OR pass a `where` clause to filter, "
            f"e.g. where=\"STATE_FIPS='06'\"."
        )
    return count


def _query_params(where: str, bbox_params: dict, out_fields: str | None,
                  max_allowable_offset: float | None,
                  token: str | None) -> dict:
    """The GeoJSON query, as the paging and one-fetch paths send it."""
    params: dict[str, Any] = {
        "where": where,
        # LOCAL PATCH readable popup v1 (2026-09-21)
        "outFields": out_fields or "*",
        "outSR": "4326",           # always WGS84 so the viewer renders it natively
        "f": "geojson",
    }
    params.update(bbox_params)
    # LOCAL PATCH esri generalise v1 (2026-09-17): only when asked. The
    # count query is deliberately left alone - it returns no geometry, so
    # there is nothing to thin. georest's queryFeatureService has no
    # parameter for this, which is why a thinned draw comes this way.
    if max_allowable_offset:
        params["maxAllowableOffset"] = str(max_allowable_offset)
    if token:
        params["token"] = token
    return params


def _fetch_features(layer_url: str, query_url: str, query_params: dict,
                    matched: int, token: str | None) -> dict:
    """The GeoJSON for a draw georest's one-shot call cannot make: thinned,
    or bigger than one response. Paged to completion where that is safe.
    """
    from georest.restesri import _http as _gh

    # LOCAL PATCH esri paging v3 (2026-09-21): when the count already exceeds
    # the server's own page size, the plain fetch returns one page that the
    # paging below immediately discards - it is unordered, so offsetting
    # into it would skip and repeat rows. Measured: two of every five round
    # trips a classed city-scale draw made were this and the metadata
    # lookup. Skipping it is safe only when the layer HAS said what its
    # page size is and can be paged; otherwise the original path runs.
    caps = _paging_caps(layer_url, token) if matched > _ONE_RESPONSE else {}
    skip_first = bool(
        caps.get("max_record") and matched > caps["max_record"]
        and caps.get("oid") and caps.get("orders"))
    if skip_first:
        # `exceededTransferLimit` is the honest description of what this
        # stands in for: the whole layer is known not to fit in one
        # response, which is exactly what the flag means.
        geojson = {"type": "FeatureCollection", "features": [],
                   "exceededTransferLimit": True}
    else:
        try:
            geojson = _fetch_json(query_url, query_params)
        except ConnectionError as exc:
            raise ConnectionError(
                f"Could not fetch features from {query_url!r}: {exc}"
            ) from exc
        if "error" in geojson:
            raise ValueError(f"Feature Service query returned an error: "
                             f"{_gh.format_esri_error(geojson['error'])}")

    # LOCAL PATCH esri paging v1 (2026-09-21): ArcGIS caps ONE response at the
    # layer's maxRecordCount (2,000 on every service this project maps),
    # and this drew whatever came back. Measured on the shipped statewide
    # fault draw: 10,245 matched, 2,000 drawn, exceededTransferLimit true
    # in the response and nothing reading it.
    features = geojson.get("features") or []
    if _exceeded_transfer(geojson) or 0 < len(features) < matched:
        geojson["features"] = _page_features(query_url, query_params, matched,
                                             layer_url, token, features)
    return geojson


def _label_fields(features: list, field_labels: dict | None) -> None:
    """LOCAL PATCH readable popup v1 (2026-09-21): rename properties in place.

    The viewer's popup prints property NAMES, so renaming them here is the
    only way a click can say "Hurricane" rather than "HRCN_RISKR". Applied
    in the order given, so the field that matters reaches the top of the
    popup; anything unlabelled keeps its name and follows.
    """
    if not field_labels:
        return
    for feature in features:
        props = feature.get("properties")
        if not isinstance(props, dict):
            continue
        renamed = {}
        for field, label in field_labels.items():
            if field in props:
                renamed[label] = props.pop(field)
        renamed.update(props)
        feature["properties"] = renamed


def addEsriFeatureService(
    url_or_result: str | dict,
    viz_params: dict | None = None,
    name: str | None = None,
    max_features: int = 1000,
    where: str = "1=1",
    bbox: str | None = None,
    token: str | None = None,
    target_map=None,
    # LOCAL PATCH esri generalise v1 (2026-09-17): server-side geometry
    # thinning, in the units of outSR. None keeps every vertex.
    max_allowable_offset: float | None = None,
    # LOCAL PATCH readable popup v1 (2026-09-21): which fields to fetch, and
    # the words to show them under. None keeps outFields="*" and the
    # service's own field names, which is what every earlier caller
    # gets. A 469-field layer makes an unreadable popup otherwise.
    out_fields: str | None = None,
    field_labels: dict | None = None,
    # LOCAL PATCH layer visible v1 (2026-09-21): added to the map switched
    # off. Three stacked translucent polygon layers is a brown wash in
    # which none of them can be read; the layer still has to BE there,
    # so it is added and left for the viewer to switch on.
    visible: bool = True,
) -> None:
    """Fetch and add an ArcGIS Feature Service layer as a GeoJSON vector layer.

    Hits ``<url>/query?f=geojson&where=<where>&outSR=4326`` and passes the
    returned GeoJSON directly to ``geeViz.geeView.Map.addLayer``.

    .. warning::
        Always performs a ``returnCountOnly=true`` pre-flight before fetching
        geometry.  If the result count exceeds *max_features*, a
        :class:`ValueError` is raised with a concrete remediation message.

    A layer bigger than one server response is paged to completion where
    the layer supports it; where it cannot be, the layer is NAMED
    ``"<name> - N of M drawn"`` so the legend says what the map does not
    show.

    Args:
        url_or_result (str or dict): Feature Service or sub-layer URL
            (e.g. ``".../FeatureServer/0"``), or a :func:`searchPortal`
            result dict.  If the URL points to the FeatureServer root rather
            than a specific layer, ``/0`` is appended automatically.
        viz_params (dict, optional): Passed to ``Map.addLayer`` as the ``viz``
            dict.  Supports all geeViz vector viz keys (``"color"``,
            ``"strokeColor"``, ``"fillColor"``, ``"opacity"``,
            ``"strokeWidth"``, ``"layerType"``, etc.).
        name (str, optional): Layer name.  Defaults to last URL segment.
        max_features (int, optional): Hard cap on feature count.  If the
            service has more than this many features matching *where*, a
            :class:`ValueError` is raised.  Defaults to 1000.  Increase
            with care — very large GeoJSON payloads can slow the viewer.
        where (str, optional): SQL WHERE clause sent to the service for
            server-side filtering.  Defaults to ``"1=1"`` (all features).
            Example: ``where="STATE_FIPS='06'"`` (California only).
        bbox (str, optional): ``"xmin,ymin,xmax,ymax"`` in WGS84. Filters
            the count as well as the fetch.
        token (str, optional): ArcGIS token for secured services.
        max_allowable_offset (float, optional): Server-side geometry
            thinning, in degrees (the units of outSR 4326).
        out_fields (str, optional): Comma-separated fields to fetch.
            Defaults to ``"*"``.
        field_labels (dict, optional): ``{field: label}`` -- the popup shows
            the label, in this order, ahead of any unlabelled field.
        visible (bool, optional): Whether the layer starts switched on.

    Raises:
        ValueError: If the feature count exceeds *max_features*.
        ConnectionError: If the service URL is unreachable.

    Example::

        import geeViz.esriLib as el
        import geeViz.geeView as gv

        # Simple fetch — all features up to default cap
        el.addEsriFeatureService(
            "https://services.arcgis.com/.../FeatureServer/0",
            name="Wildfire Perimeters",
        )

        # Filter server-side to stay under the cap
        el.addEsriFeatureService(
            "https://services.arcgis.com/.../FeatureServer/0",
            where="YEAR_=2023 AND GIS_ACRES > 10000",
            name="Large 2023 Fires",
            max_features=500,
        )

        gv.Map.view()
    """
    import geeViz.geeView as gv

    url = _resolve_url(url_or_result)

    # Ensure we're pointing at a layer (e.g. /0), not the FeatureServer root.
    # The root URL ends in "FeatureServer" (case-insensitive); sub-layers end
    # in a digit.
    if url.lower().endswith("featureserver"):
        url = f"{url}/0"

    if name is None:
        name = url.rstrip("/").split("/")[-1]
        # If name is just "0", walk up for a more descriptive label
        if name.isdigit():
            parts = url.rstrip("/").split("/")
            name = f"{parts[-2]} ({name})" if len(parts) >= 2 else name

    from georest.restesri import services as _gs

    # geeViz's SSRF guard, which georest does not have and should not:
    # it is this package's policy, not a REST client's. Applied here,
    # before anything is requested -- georest's own requests below go to
    # this same host, and every other request re-checks in _fetch_json.
    _check_url(url)

    query_url = f"{url}{_FEATURE_QUERY_SUFFIX}"
    bbox_params = _bbox_params(bbox)
    # The count comes first, and from here, because it is what proves the
    # drawn layer whole (see _count_features). It also carries the
    # max_features overflow guard.
    matched = _count_features(query_url, where, bbox_params, token,
                              max_features)
    query_params = _query_params(where, bbox_params, out_fields,
                                 max_allowable_offset, token)

    if not max_allowable_offset and max_features <= _ONE_RESPONSE:
        # The plain case, and georest's: spatial filter, overflow guard and
        # one GeoJSON fetch in a single call. What is added is only the
        # retry ladder around it and the booth's timeout.
        try:
            geojson = _with_retries(lambda: _gs.queryFeatureService(
                url,
                where=where,
                geometry=bbox,
                out_fields=out_fields or "*",
                max_features=max_features,
                token=token,
                timeout=_TIMEOUT,
            ))
        except ValueError:
            # An Esri error body, or the overflow guard. Both mean the same
            # on either side of the boundary -- pass them through rather
            # than flattening them into the network case.
            raise
        except (RuntimeError, OSError) as exc:
            # georest reports an unreachable host or a bad HTTP status as
            # RuntimeError. This module has always raised ConnectionError
            # and its callers catch that, so translate at the boundary --
            # the same thing _fetch_json does, for the same reason.
            raise ConnectionError(
                f"Could not reach Feature Service at {url!r}: {exc}"
            ) from exc
        # LOCAL PATCH esri paging v1 (2026-09-21): georest returns the first
        # page of a truncated answer as if it were the answer.
        features = geojson.get("features") or []
        if _exceeded_transfer(geojson) or 0 < len(features) < matched:
            geojson["features"] = _page_features(
                query_url, query_params, matched, url, token, features)
    else:
        # Thinned, or bigger than one response: things georest's call has
        # no parameter for. Same requests, through georest's transport.
        geojson = _fetch_features(url, query_url, query_params, matched,
                                  token)

    # LOCAL PATCH esri paging v1 (2026-09-21): when paging still cannot
    # finish, put the shortfall in the LAYER NAME - the legend is where a
    # person at the booth would see it, not the console.
    features = geojson.get("features") or []
    actual = len(features)
    if actual < matched:
        name = f"{name} - {actual:,} of {matched:,} drawn"
        print(f"Adding Esri Feature Service: {name} (PARTIAL - the "
              f"service would not return the rest)")
    else:
        print(f"Adding Esri Feature Service: {name} ({actual:,} features)")

    _label_fields(features, field_labels)

    viz = dict(viz_params or {})
    # The viewer needs layerType=geoJSONVector; addLayer sets it automatically
    # when passed a dict, but be explicit so callers can mix it with other keys.
    viz.setdefault("layerType", "geoJSONVector")

    # LOCAL PATCH layer visible v1 (2026-09-21)
    (target_map or gv.Map).addLayer(geojson, viz, name, visible)


# ---------------------------------------------------------------------------
# LOCAL PATCH classed fetch v1 (2026-09-21)
# ---------------------------------------------------------------------------
def _class_row_matches(props: dict, match: dict) -> bool:
    """Does one feature satisfy one class spec?

    Mirrors `agent_tools._row_matches`. The NULL rules are the whole reason
    this is written out rather than done with a set membership test: FEMA
    encodes the regulatory floodway as a SUBTYPE of Zone AE, so an ordinary
    AE polygon carries ZONE_SUBTY NULL and IS the row that the "everything
    except the floodway" class has to keep.
    """
    for field, rule in (match or {}).items():
        value = props.get(field)
        text = None if value is None else str(value)
        wanted = rule.get("in") if isinstance(rule, dict) else rule
        unwanted = rule.get("not_in") if isinstance(rule, dict) else None
        if unwanted is not None:
            if text is None:
                if None in unwanted or "null" in [str(u).lower()
                                                  for u in unwanted]:
                    return False
            elif text in [str(u) for u in unwanted]:
                return False
        if wanted is not None:
            allow_null = any(w is None for w in wanted)
            if text is None:
                if not allow_null:
                    return False
            elif text not in [str(w) for w in wanted if w is not None]:
                return False
    return True


def addEsriFeatureServiceClassed(
    url: str,
    classes: list,
    bbox: str | None = None,
    where: str = "1=1",
    other: dict | None = None,
    out_fields: str | None = None,
    field_labels: dict | None = None,
    max_allowable_offset: float | None = None,
    max_features: int = 1000,
    token: str | None = None,
    target_map=None,
) -> None:
    """Fetch a layer ONCE and add one map layer per class.

    Every class asks the same layer over the same box, so asking six times
    is six count preflights, six metadata lookups and six paging runs.
    Measured over the Houston urban area: 79.0 s that way, 27.7 s this way,
    the same features either way.

    Args:
        classes: ``[{"name", "viz", "match", "visible"}]``. `match` is the
            JSON form evaluated by :func:`_class_row_matches`.
        other: optional ``{"name", "viz", "visible"}`` for rows matching no
            class. Without it those rows are DROPPED and the count is
            reported, because a class we have not modelled must never
            vanish in silence.
        where: the outer filter - typically the source's exclude clause.
    """
    import geeViz.geeView as gv

    url = _resolve_url(url)
    if url.lower().endswith("featureserver"):
        url = f"{url}/0"
    _check_url(url)

    query_url = f"{url}{_FEATURE_QUERY_SUFFIX}"
    bbox_params = _bbox_params(bbox)
    matched = _count_features(query_url, where, bbox_params, token,
                              max_features)
    geojson = _fetch_features(
        url, query_url,
        _query_params(where, bbox_params, out_fields, max_allowable_offset,
                      token),
        matched, token)
    features = geojson.get("features") or []
    _label_fields(features, field_labels)

    # Split. One pass, first matching class wins, exactly as the per-class
    # SQL did - the classes are written to be disjoint and the floodway
    # deliberately sits last so it beats the AE it is inside.
    buckets = [[] for _ in classes]
    leftovers = []
    for feature in features:
        props = feature.get("properties") or {}
        for index, spec in enumerate(classes):
            if _class_row_matches(props, spec.get("match") or {}):
                buckets[index].append(feature)
                break
        else:
            leftovers.append(feature)

    short = len(features) < matched
    for spec, rows in zip(classes, buckets):
        name = spec.get("name") or "layer"
        if short:
            name = f"{name} - {len(features):,} of {matched:,} drawn"
        viz = dict(spec.get("viz") or {})
        viz.setdefault("layerType", "geoJSONVector")
        print(f"Adding Esri Feature Service: {name} ({len(rows):,} features)")
        (target_map or gv.Map).addLayer(
            {"type": "FeatureCollection", "features": rows}, viz, name,
            bool(spec.get("visible", True)))

    if leftovers:
        if other:
            viz = dict(other.get("viz") or {})
            viz.setdefault("layerType", "geoJSONVector")
            name = other.get("name") or "Other classes"
            print(f"Adding Esri Feature Service: {name} "
                  f"({len(leftovers):,} features)")
            (target_map or gv.Map).addLayer(
                {"type": "FeatureCollection", "features": leftovers}, viz,
                name, bool(other.get("visible", True)))
        else:
            print(f"WARNING: {len(leftovers):,} features matched no class and "
                  f"were NOT drawn - pass `other=` to keep them.")


# ---------------------------------------------------------------------------
# addEsriService — auto-dispatch
# ---------------------------------------------------------------------------

def addEsriService(
    url_or_result: str | dict,
    viz_params: dict | None = None,
    name: str | None = None,
    token: str | None = None,
    max_features: int = 1000,
    where: str = "1=1",
    target_map=None
) -> None:
    """Auto-detect the Esri service type and call the appropriate add helper.

    Inspects the URL path (and falls back to the service metadata) to
    determine whether *url_or_result* is an Image Service, Feature Service,
    or Map Service, then delegates to :func:`addEsriImageService`,
    :func:`addEsriFeatureService`, or :func:`addEsriMapService`.

    Args:
        url_or_result (str or dict): Service URL or :func:`searchPortal`
            result dict.
        viz_params (dict, optional): Visualization parameters forwarded to
            the typed helper.
        name (str, optional): Layer name.
        token (str, optional): ArcGIS token.
        max_features (int, optional): Forwarded to :func:`addEsriFeatureService`.
        where (str, optional): SQL WHERE clause forwarded to
            :func:`addEsriFeatureService`.

    Raises:
        ValueError: If the service type cannot be determined.

    Example::

        import geeViz.esriLib as el

        results = el.searchPortal("naip 2023", limit=5)
        for r in results:
            el.addEsriService(r)  # dispatches by type automatically
    """
    url = _resolve_url(url_or_result)
    stype = _detect_service_type(url)

    # Pass the original url_or_result so name resolution works with dicts too
    if stype == "ImageServer":
        addEsriImageService(url_or_result, viz_params=viz_params, name=name, token=token, target_map=target_map)
    elif stype == "FeatureServer":
        addEsriFeatureService(
            url_or_result,
            viz_params=viz_params,
            name=name,
            max_features=max_features,
            where=where,
            token=token,
        )
    elif stype == "MapServer":
        addEsriMapService(url_or_result, name=name, token=token, viz_params=viz_params, target_map=target_map)
    else:
        raise ValueError(
            f"Could not determine service type for URL {url!r}.  "
            f"Use addEsriImageService / addEsriFeatureService / "
            f"addEsriMapService directly, or inspect the service manually "
            f"with getServiceMetadata()."
        )
