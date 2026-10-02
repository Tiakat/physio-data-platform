"""
Device parsers.

Every parser has the same contract:

    parse(path, profile, device_cfg) -> (frame, meta)

    frame : pandas DataFrame indexed by a timezone aware UTC timestamp,
            columns named with standard variable names only.
    meta  : dict with at least {rows, duration_s, interval_s, source_columns}

The parser is responsible for the strange parts of one file format and nothing
else. Everything downstream sees the same rectangular, standard shape.
"""

from . import bettercare, bis, ett, infinity, nol_medasense, pump  # noqa: F401

REGISTRY = {
    "infinity": infinity.parse,
    "bettercare": bettercare.parse,
    "nol_medasense": nol_medasense.parse,
    "pump": pump.parse,
    "ett": ett.parse,
    "bis": bis.parse,
}

SNIFFERS = {
    "infinity": infinity.sniff_header,
    "bettercare": bettercare.sniff_header,
    "nol_medasense": nol_medasense.sniff_header,
    "pump": pump.sniff_header,
    "ett": ett.sniff_header,
    "bis": bis.sniff_header,
}


def get(name: str):
    if name not in REGISTRY:
        raise KeyError(f"No parser named {name!r}. Known: {sorted(REGISTRY)}")
    return REGISTRY[name]


def sniff(name: str):
    """
    Header-only structural check used by validation when the file is not
    parsed. Each parser knows its own layout (stacked headers, mixed
    delimiters, compressed exports), so the check lives with the parser.
    """
    if name not in SNIFFERS:
        raise KeyError(f"No header sniffer named {name!r}. Known: {sorted(SNIFFERS)}")
    return SNIFFERS[name]
