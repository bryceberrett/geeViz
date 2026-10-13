"""LCMS API client — Landscape Change Monitoring System.

Wall-to-wall 30 m maps of land cover, land use, and change across the
conterminous US, Alaska, Hawaii and Puerto Rico, currently spanning
1985-2025. Public, no authentication.

Three things about this API shape the client:

**It answers errors with HTTP 200.** An unknown summary area returns a
``ParameterError`` inside a 200 body, so a client checking ``.ok`` reads
failure as success and returns nothing. :mod:`geeViz.fsInsights._http`
inspects the payload; that check is the reason it exists.

**Trailing slashes matter.** ``/release`` 301-redirects while
``/release/`` returns data. Every path here carries its slash.

**Products carry their own palettes.** Each class comes back with a
``ClassValue`` and a ``ClassPalette``, which means visualization
parameters can be built *from the API* rather than from constants that
drift out of sync at every release. See :func:`lcms_vis_params`.

Areas are precomputed — 3,643 of them (counties, ranger districts, plus
CONUS and All-Lands rollups). There is no runtime zonal-statistics
engine behind this API: ``bbox``, ``geojson`` and ``geometry`` are all
rejected, and ``POST`` returns 403. For an arbitrary polygon, use the
Earth Engine copies of the same products, which is what
:func:`lcms_summary` does when handed a geometry.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ._http import get_json
from .vocab import LCMS_BASE

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Any] = {}

#: Columns every summary frame carries, empty or not. An empty
#: DataFrame built from [] has NO columns, so the caller's next line
#: -- almost always a column selection -- raises a bare KeyError that
#: names none of the real problem.
_SUMMARY_COLUMNS = ("year", "class_name", "square_meters", "acres",
                    "hectares", "product", "area", "source")


def _result(payload: Any) -> Any:
    """Unwrap the ``{"Result": ...}`` envelope and normalize its nesting.

    The envelope's inner shape varies by endpoint and is undocumented:

    * ``/summaryareas/`` -> ``Result`` is a flat list of dicts.
    * ``/release/``      -> ``Result`` is a list of **single-element
      lists**, one per release: ``[[rel], [rel], ...]``.
    * ``/products/``     -> ``Result`` wraps its list one level deep.

    So the rule is "flatten one level when *every* element is a list",
    which normalizes all three without touching an already-flat result.
    Special-casing only the single-wrapper form silently returned lists
    where dicts were expected for the five-release case.
    """
    if isinstance(payload, dict) and "Result" in payload:
        payload = payload["Result"]
    if (isinstance(payload, list) and payload
            and all(isinstance(x, list) for x in payload)):
        payload = [item for sub in payload for item in sub]
    return payload


def lcms_releases(*, product: str = "", refresh: bool = False) -> List[dict]:
    """Every published LCMS release, newest first.

    Each entry carries ``VersionNumber``, ``StartYear``, ``EndYear``,
    ``Products`` and ``StudyAreas``.

    **Releases are not interchangeable**, in two ways that bite:

    * **Products differ.** ``2025-11`` carries ``Change`` /
      ``Land_Cover`` / ``Land_Use``; ``2025-6`` is a tree-canopy release
      carrying only ``NLCD_Percent_Tree_Canopy_Cover``. "The latest
      release" is therefore ambiguous unless you say latest *of what* —
      see :func:`latest_release`.
    * **The API's summaries cover less than the imagery.** The API's
      pre-computed summaries for ``2025-11`` list only CONUS and AK,
      while the ``2025-11`` Earth Engine asset itself covers CONUS, AK,
      HI and PRUSVI through 2025. For Hawaii or Puerto Rico, summarize
      the asset over a geometry (the Earth Engine path) rather than pin
      an older release for the API.

    Note also that ``2022-8`` reports ``SummaryAreaCount = 0`` — it has
    no summary areas, so the API cannot answer area queries against it
    even though it lists products.

    Args:
        product: Keep only releases that publish this product.
    """
    if refresh or "releases" not in _CACHE:
        _CACHE["releases"] = _result(get_json(f"{LCMS_BASE}/release/")) or []
    rels = _CACHE["releases"]
    if product:
        rels = [r for r in rels
                if any(str(p.get("Name", "")).lower() == product.lower()
                       for p in (r.get("Products") or []))]
    return rels


def release_products(release: str = "") -> List[str]:
    """Product names published by one release (default: newest overall)."""
    for r in lcms_releases():
        if not release or str(r.get("VersionNumber")) == str(release):
            return [str(p.get("Name")) for p in (r.get("Products") or [])]
    return []


def latest_release(product: str = "") -> str:
    """Resolve to a concrete version string, e.g. ``2025-11``.

    Args:
        product: Resolve to the newest release **carrying this
            product**. Without it you get the newest release overall,
            which may not publish what you are about to ask for —
            ``2025-6`` is newer than ``2024-10`` but has no
            ``Land_Cover``.

    Pinning matters beyond that. Caching under the key ``latest`` means
    a new release silently changes the answer to a question asked last
    year; resolving to a version once keeps an old analysis
    reproducible.
    """
    for r in lcms_releases(product=product):
        v = r.get("VersionNumber")
        if v:
            return str(v)
    if product:
        raise ValueError(
            f"no LCMS release publishes product {product!r}. Available "
            f"products by release: "
            + "; ".join(f"{r.get('VersionNumber')}="
                        f"{[p.get('Name') for p in (r.get('Products') or [])]}"
                        for r in lcms_releases())
        )
    return "latest"


def _check_product(product: str, release: str = "") -> None:
    """Raise if ``release`` does not publish ``product``.

    Caught locally because the API's answer is an "Invalid Summary Area"
    ParameterError, which points at the *area* — sending the reader to
    check a county name when the real problem is that they asked a
    tree-canopy release for land cover.
    """
    # A release can publish the product and still be unable to answer,
    # because it has no summary areas at all — 2022-8 reports
    # SummaryAreaCount = 0. Left to the API that surfaces as "Invalid
    # Summary Area for LCMS Release 2022-8", which blames the county and
    # is wrong in the same way the product mismatch was.
    for r in lcms_releases():
        if release and str(r.get("VersionNumber")) == str(release):
            # Only an EXPLICIT zero counts. A missing key means the
            # release list did not report a count, which is unknown
            # rather than empty — treating it as empty would reject
            # perfectly good releases on incomplete metadata.
            count = r.get("SummaryAreaCount")
            if count is not None and int(count) == 0:
                usable = [str(x.get("VersionNumber"))
                          for x in lcms_releases(product=product)
                          if x.get("SummaryAreaCount")]
                raise ValueError(
                    f"release {release!r} has no summary areas "
                    f"(SummaryAreaCount=0), so it cannot answer area "
                    f"queries even though it publishes {product!r}. "
                    + (f"Usable releases: {usable} "
                       f"(pass release='{usable[0]}')" if usable else "")
                )
            break

    have = release_products(release)
    if not have:
        return  # unknown release; let the API speak
    if not any(p.lower() == product.lower() for p in have):
        carriers = [str(r.get("VersionNumber"))
                    for r in lcms_releases(product=product)]
        raise ValueError(
            f"release {release or latest_release()!r} does not publish "
            f"{product!r} - it has {have}. "
            + (f"Releases with {product!r}: {carriers} "
               f"(pass release='{carriers[0]}')"
               if carriers else f"No release publishes {product!r}.")
        )


def _release_path(release: str = "") -> str:
    return f"{LCMS_BASE}/release/{release or 'latest'}"


def lcms_products(release: str = "", *, refresh: bool = False) -> List[dict]:
    """Products in a release, each with its full class list.

    Currently ``Change``, ``Land_Cover`` and ``Land_Use``.
    """
    key = f"products:{release or 'latest'}"
    if refresh or key not in _CACHE:
        _CACHE[key] = _result(
            get_json(f"{_release_path(release)}/products/")) or []
    return _CACHE[key]


def lcms_classes(product: str, release: str = "") -> "Any":
    """Class table for one product — name, pixel value, and palette hex.

    Returns a ``pandas.DataFrame`` with ``class_name``, ``class_value``
    and ``palette``.

    Raises:
        ValueError: The release does not publish this product, naming
            the releases that do.
    """
    _check_product(product, release)
    for p in lcms_products(release):
        if str(p.get("Name", "")).lower() == product.lower():
            return _frame([{
                "class_name": c.get("Name"),
                "class_value": c.get("ClassValue"),
                "palette": c.get("ClassPalette"),
            } for c in (p.get("Classes") or [])])
    names = [p.get("Name") for p in lcms_products(release)]
    raise ValueError(f"unknown product {product!r}; available: {names}")


def lcms_vis_params(product: str, release: str = "") -> dict:
    """Build geeViz/Earth Engine visualization parameters for a product.

    Returns a dict with ``min``, ``max``, ``palette`` and
    ``classLegendDict``, ready to hand to ``Map.addLayer``::

        Map.addLayer(lcms_change, fs.lcms_vis_params("Change"), "LCMS Change")

    Taking the palette from the API rather than a hardcoded constant
    means the legend cannot drift out of sync with the release — the
    classes and their colors arrive from the same place as the data.

    The palette is emitted as a dense, value-ordered list so it lines up
    with a contiguous ``min``..``max`` range. Gaps in the class values
    are filled with a neutral grey rather than silently shifting every
    subsequent color, which is the failure mode that makes a legend look
    right and read wrong.
    """
    df = lcms_classes(product, release)
    rows = df.to_dict("records") if hasattr(df, "to_dict") else list(df)
    rows = [r for r in rows if r.get("class_value") is not None]
    if not rows:
        raise ValueError(f"product {product!r} exposes no classes")

    by_value = {int(r["class_value"]): r for r in rows}
    lo, hi = min(by_value), max(by_value)

    palette, legend = [], {}
    for v in range(lo, hi + 1):
        r = by_value.get(v)
        palette.append(str(r["palette"]).lstrip("#") if r else "cccccc")
        if r:
            legend[str(r["class_name"])] = str(r["palette"]).lstrip("#")

    return {"min": lo, "max": hi, "palette": palette,
            "classLegendDict": legend}


def lcms_summary_areas(release: str = "", *, type: str = "",
                       refresh: bool = False) -> "Any":
    """The precomputed areas this API can summarize.

    3,643 of them in release 2025-11: 3,137 US counties, 502 ranger
    districts, and four rollups (political and Forest Service, CONUS and
    All-Lands).

    Args:
        release: Release version; defaults to latest.
        type: Filter on area type, e.g. ``"US-COUNTIES"`` or
            ``"RANGER-DISTRICTS"``.
    """
    key = f"areas:{release or 'latest'}"
    if refresh or key not in _CACHE:
        _CACHE[key] = _result(
            get_json(f"{_release_path(release)}/summaryareas/")) or []
    rows = _CACHE[key]
    if type:
        rows = [r for r in rows if str(r.get("Type", "")).upper() == type.upper()]
    return _frame([{"name": r.get("Name"), "type": r.get("Type")}
                   for r in rows])


def lcms_summary(product: str = "Land_Cover", *,
                 state: str = "", county: str = "",
                 region: str = "", forest: str = "", district: str = "",
                 year: Optional[int] = None,
                 startyear: Optional[int] = None,
                 endyear: Optional[int] = None,
                 geometry: Any = None,
                 scale: int = 30,
                 release: str = "") -> "Any":
    """Class areas for an area and period, as a tidy frame.

    Dispatches on what it is given:

    * A **named area** (``state``/``county``, or ``region``/``forest``/
      ``district``) goes to the LCMS API — instant, precomputed, and no
      Earth Engine authentication required.
    * A **geometry** goes to Earth Engine, computing the same class
      areas over an arbitrary polygon. Slower, needs EE initialized, and
      only available when ``geometry`` is passed.

    The returned frame records which backend produced it, because the
    two are not guaranteed to agree to the pixel and a reader should not
    have to guess.

    Args:
        product: ``"Land_Cover"``, ``"Land_Use"`` or ``"Change"``.
        state: State name. Required when ``county`` is given.
        county: County name.
        region, forest, district: Forest Service units. ``district``
            requires ``forest``.
        year: Single year. Omit for the full time series — the whole
            1985-2025 range is ~64 KB, small enough to fetch eagerly.
        startyear, endyear: Inclusive year range.
        geometry: An ``ee.Geometry`` / ``ee.Feature`` /
            ``ee.FeatureCollection``. Routes to the Earth Engine backend.
        scale: Reduction scale in meters for the EE backend. LCMS is a
            30 m product; coarsening trades accuracy for speed.
        release: Release version; defaults to latest.

    Returns:
        ``pandas.DataFrame`` with ``year``, ``class_name``,
        ``square_meters``, ``acres``, ``hectares``, ``product``,
        ``area``, ``source``.
    """
    # Product/release compatibility first. The API answers a mismatch
    # with an "Invalid Summary Area" ParameterError, which points at the
    # area name and sends the reader off checking a county spelling when
    # the real problem is asking a tree-canopy release for land cover.
    _check_product(product, release)

    if geometry is not None:
        return _summary_from_ee(product, geometry, scale=scale,
                                year=year, startyear=startyear,
                                endyear=endyear, release=release)

    # District without forest, or county without state, is rejected by
    # the API in a way that reads as "invalid summary area" rather than
    # "you omitted the parent" — clearer to catch it here.
    if county and not state:
        raise ValueError("county= requires state=")
    if district and not forest:
        raise ValueError("district= requires forest=")
    if not any((state, county, region, forest, district)):
        raise ValueError(
            "name an area (state=/county=, or region=/forest=/district=) "
            "or pass geometry= to compute one from Earth Engine"
        )

    params: Dict[str, Any] = {"product": product}
    for k, v in (("state", state), ("county", county), ("region", region),
                 ("forest", forest), ("district", district)):
        if v:
            params[k] = v
    if year is not None:
        params["year"] = year
    if startyear is not None:
        params["startyear"] = startyear
    if endyear is not None:
        params["endyear"] = endyear

    payload = get_json(f"{_release_path(release)}/", params=params)
    result = payload.get("Result", payload) if isinstance(payload, dict) else payload
    area_name = (result or {}).get("SummaryArea", "") if isinstance(result, dict) else ""

    records = []
    for rec in (result or {}).get("ProductRecords", []) if isinstance(result, dict) else []:
        yr = rec.get("Year")
        for prod in rec.get("Products", []):
            pname = prod.get("Product")
            for cls in prod.get("Classes", []):
                sqm = float(cls.get("SquareMeters") or 0.0)
                records.append({
                    "year": int(yr) if str(yr).isdigit() else yr,
                    "class_name": cls.get("Class"),
                    "square_meters": sqm,
                    "acres": sqm / 4046.8564224,
                    "hectares": sqm / 10000.0,
                    "product": pname,
                    "area": area_name,
                    "source": "lcms-api",
                })
    return _frame(records, columns=_SUMMARY_COLUMNS)


def _summary_from_ee(product: str, geometry: Any, *, scale: int,
                     year: Optional[int], startyear: Optional[int],
                     endyear: Optional[int], release: str) -> "Any":
    """Class areas over an arbitrary geometry, via Earth Engine.

    The API only serves its 3,643 precomputed areas, so anything else has
    to be computed. LCMS is published as Earth Engine assets, which makes
    this a grouped area reduction rather than a reimplementation.

    Deliberately imported lazily: Earth Engine is an *optional* path
    here, and the API path must keep working on a machine with no EE
    credentials — which is the normal case for the MCP tool.
    """
    try:
        import ee
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "geometry= needs earthengine-api. Use a named area "
            "(state=/county=/forest=...) for the API-only path."
        ) from exc

    from .lcms_ee import lcms_ee_collection  # local import; see module note

    coll = lcms_ee_collection(product, release=release)
    years = _year_filter(year, startyear, endyear)

    region = geometry
    if hasattr(geometry, "geometry"):
        region = geometry.geometry()

    classes = lcms_classes(product, release)
    crows = classes.to_dict("records") if hasattr(classes, "to_dict") else list(classes)
    name_by_value = {int(c["class_value"]): c["class_name"]
                     for c in crows if c.get("class_value") is not None}

    records = []
    for yr, image in _iter_year_images(coll, years):
        areas = ee.Image.pixelArea().addBands(image.rename("cls")).reduceRegion(
            reducer=ee.Reducer.sum().group(groupField=1, groupName="cls"),
            geometry=region, scale=scale, maxPixels=1e13, bestEffort=True,
        ).getInfo()
        for grp in (areas or {}).get("groups", []):
            v = int(grp.get("cls"))
            sqm = float(grp.get("sum") or 0.0)
            records.append({
                "year": yr,
                "class_name": name_by_value.get(v, f"class_{v}"),
                "square_meters": sqm,
                "acres": sqm / 4046.8564224,
                "hectares": sqm / 10000.0,
                "product": product,
                "area": "(geometry)",
                "source": "earth-engine",
            })

    if not records:
        # Almost always an AOI outside the release's coverage. Say so —
        # and still return a frame WITH the expected columns, because an
        # empty DataFrame has no columns at all and the caller's very
        # next line is usually a column selection that would raise a
        # bare KeyError naming none of the real problem.
        logger.warning(
            "fsInsights: LCMS/%s returned no pixels for this geometry. "
            "Check it falls inside the release's study areas (release "
            "2025-11 in Earth Engine covers CONUS, AK, HI and PRUSVI).", product,
        )
    return _frame(records or [], columns=_SUMMARY_COLUMNS)


def _year_filter(year, startyear, endyear):
    if year is not None:
        return [int(year)]
    if startyear is not None or endyear is not None:
        lo = int(startyear) if startyear is not None else 1985
        hi = int(endyear) if endyear is not None else 2025
        return list(range(lo, hi + 1))
    return None


def _iter_year_images(coll, years):
    """Yield ``(year, ee.Image)`` for the requested years.

    **Mosaics across study areas rather than taking ``.first()``.** A
    single year holds one image per study area — filtering 2024 in the
    2025-11 release returns two, AK and CONUS — so ``.first()`` picks an
    arbitrary one. It picked AK, which meant an Oregon geometry reduced
    to nothing and produced an empty frame. The study areas do not
    overlap, so mosaicking them is both correct and the only way a
    geometry can select its own coverage.

    ``year`` is an integer property; filtering with the string ``"2024"``
    silently matches zero images.
    """
    import ee

    if years is None:
        info = coll.aggregate_array("year").getInfo()
        years = sorted({int(y) for y in (info or [])})
    for y in years:
        subset = coll.filter(ee.Filter.eq("year", int(y)))
        yield int(y), subset.mosaic()


def _frame(records: List[dict], columns=None):
    """Build a DataFrame, keeping the column contract even when empty."""
    try:
        import pandas as pd
        if not records and columns:
            return pd.DataFrame(columns=list(columns))
        return pd.DataFrame(records)
    except Exception:  # pragma: no cover
        return records
