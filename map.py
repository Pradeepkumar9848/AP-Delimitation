import re
import json
from typing import List

import geopandas as gpd
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

st.set_page_config(layout="wide", page_title="AP Vote Share")

# ============
# CONFIG
# ============
SHP_PATH = "Villages_Guntur.shp"                      # your .shp (keep .dbf/.shx/.prj/.cpg beside it)
SHP_REGION_FIELD = "id"                                # village id column in shapefile
SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRcpJ7af2Qox1haajC9iKrxDaWmtQ8fBmrICAnDqlMYoFHyi_32ebtSNz_6eKRHZkSb97RQeTscwnkf/pub?output=csv"

CSV_KEY = "region_code"                                # key in Google Sheet
VILLAGE_FIELD = "village_name"
AC_FIELD = "AC_name"
DUMMY_AC_FIELD = "dummy_ac"

TOTAL_VOTES_COL = "total_votes"
PARTY_COUNT_COLS = ["TDP_votes", "YSRCP_votes", "BJP_votes", "Others_votes"]
PARTY_SHARE_COLS = ["TDP_vote_share", "YSRCP_vote_share", "BJP_vote_share", "Others"]
DEFAULT_CASTE_COLS = ["SC_pct", "ST_pct", "OBC_pct", "BC_pct", "OC_pct", "Minority_pct"]

# =========
# HELPERS
# =========
def _normalize_key(s):
    if pd.isna(s): return None
    s = str(s).strip().replace("\u00A0", " ")
    s = re.sub(r"\s+", " ", s).upper()
    return s

@st.cache_data(show_spinner=True)
def load_shapefile(path: str, key_field: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    # normalize id
    gdf[key_field] = gdf[key_field].map(_normalize_key)
    # ensure WGS84
    try:
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
    except Exception:
        pass
    return gdf

@st.cache_data(show_spinner=True)
def load_sheet(url: str) -> pd.DataFrame:
    df = pd.read_csv(url, dtype={CSV_KEY: str})
    df[CSV_KEY] = df[CSV_KEY].map(_normalize_key)
    # ensure dummy exists
    if DUMMY_AC_FIELD not in df.columns:
        df[DUMMY_AC_FIELD] = df.get(AC_FIELD, "")
    return df

def ac_aggregate(df_in: pd.DataFrame, group_key: str, caste_cols: List[str]) -> pd.DataFrame:
    work = df_in.copy()
    # coerce numerics
    for c in [TOTAL_VOTES_COL] + PARTY_COUNT_COLS + PARTY_SHARE_COLS:
        if c in work.columns:
            work[c] = pd.to_numeric(work[c], errors="coerce")

    agg = work.groupby(group_key, dropna=False).agg({
        TOTAL_VOTES_COL: "sum",
        PARTY_COUNT_COLS[0]: "sum",
        PARTY_COUNT_COLS[1]: "sum",
        PARTY_COUNT_COLS[2]: "sum",
        PARTY_COUNT_COLS[3]: "sum",
    }).reset_index().rename(columns={group_key: "AC_key"})

    # recompute shares from sums
    share_map = {
        "TDP_share": PARTY_COUNT_COLS[0],
        "YSRCP_share": PARTY_COUNT_COLS[1],
        "BJP_share": PARTY_COUNT_COLS[2],
        "Others_share": PARTY_COUNT_COLS[3],
    }
    for out, cnt in share_map.items():
        agg[out] = (agg[cnt] / agg[TOTAL_VOTES_COL] * 100).round(2).fillna(0)

    # weighted caste %
    for c in caste_cols:
        if c in work.columns:
            wdf = work[[group_key, c, TOTAL_VOTES_COL]].copy()
            wdf[c] = pd.to_numeric(wdf[c], errors="coerce")
            wdf[TOTAL_VOTES_COL] = pd.to_numeric(wdf[TOTAL_VOTES_COL], errors="coerce")
            num = (wdf[c] * wdf[TOTAL_VOTES_COL]).groupby(wdf[group_key]).sum(min_count=1)
            den = wdf.groupby(group_key)[TOTAL_VOTES_COL].sum(min_count=1)
            agg[c + "_weighted"] = (num / den * 1.0).round(2)
    return agg

# =========
# LOAD
# =========
gdf = load_shapefile(SHP_PATH, SHP_REGION_FIELD)
df = load_sheet(SHEET_URL)

# Auto-detect caste cols present in sheet
CASTE_COLS = [c for c in DEFAULT_CASTE_COLS if c in df.columns]

# Normalize/shield keys
df[CSV_KEY] = df[CSV_KEY].map(_normalize_key)
gdf[SHP_REGION_FIELD] = gdf[SHP_REGION_FIELD].map(_normalize_key)

# Merge (allow m:1 in case village has multipart polygons)
merge_mode = "m:1" if gdf[SHP_REGION_FIELD].duplicated().any() else "1:1"
merged = gdf.merge(df, left_on=SHP_REGION_FIELD, right_on=CSV_KEY, how="left", validate=merge_mode)

# =====
#  UI
# =====
t1, t2 = st.columns([2, 1])
with t1:
    st.title("AP Vote Share — AC & VIllage Level")
with t2:
    st.write("")

c1, c2= st.columns([1.2, 1])
with c1:
    level = st.radio("Map Level", ["Village", "Assembly Constituency"], horizontal=True)
with c2:
    use_dummy = st.checkbox("Use Dummy Constituencies", value=True,
                            help="AC dissolve by dummy_ac when ON; by AC_name when OFF.")
# with c3:
#     if st.button("Reset dummy_ac (session)"):
#         merged[DUMMY_AC_FIELD] = merged.get(AC_FIELD, merged.get(DUMMY_AC_FIELD))
#         st.success("dummy_ac reset to original AC_name for this session.")

# ==========
# MAP RENDER
# ==========
m = folium.Map(location=[16.3, 80.5], zoom_start=9, tiles="OpenStreetMap")

if level == "Village":
    # village layer (as-is)
    tooltip_fields = [VILLAGE_FIELD, "subdistrict", "district", AC_FIELD, DUMMY_AC_FIELD] + PARTY_SHARE_COLS + CASTE_COLS
    for f in tooltip_fields:
        if f not in merged.columns:
            merged[f] = None

    gj = json.loads(merged.to_json(drop_id=True))
    folium.GeoJson(
        data=gj,
        style_function=lambda x: {"fillColor": "#D7F4E3", "color": "#222", "weight": 0.6, "fillOpacity": 0.55},
        highlight_function=lambda x: {"weight": 3, "color": "#D33"},
        tooltip=folium.features.GeoJsonTooltip(
            fields=tooltip_fields,
            aliases=["Village","Subdistrict","District","AC (orig.)","AC (dummy)"] +
                    ["TDP %","YSRCP %","BJP %","Others %"] + CASTE_COLS,
            sticky=True
        ),
        name="Villages"
    ).add_to(m)

else:
    # ===== AC DISSOLVE =====
    group_key = DUMMY_AC_FIELD if use_dummy else AC_FIELD

    # 1) dissolve polygons by AC
    # keep only geometry + grouping key to avoid dissolve trying to aggregate non-geometry columns
    g_ac = merged[[group_key, "geometry"]].copy()
    g_ac = g_ac.dissolve(by=group_key, as_index=False)  # one row per AC_key

    # 2) aggregate votes/caste by AC
    ac_stats = ac_aggregate(merged, group_key, CASTE_COLS)   # AC_key, shares, weighted caste
    ac_stats.rename(columns={"AC_key": group_key}, inplace=True)

    # 3) merge geometry + stats into an AC GeoDataFrame
    ac_gdf = g_ac.merge(ac_stats, on=group_key, how="left")

    # 4) render dissolved AC polygons
    tooltip_fields = [group_key, "TDP_share", "YSRCP_share", "BJP_share", "Others_share"] + \
                     [c + "_weighted" for c in CASTE_COLS if (c + "_weighted") in ac_gdf.columns]
    aliases = ["AC", "TDP %", "YSRCP %", "BJP %", "Others %"] + \
              [c.replace("_pct", "").upper() + " % (weighted)" for c in CASTE_COLS]

    gj = json.loads(ac_gdf.to_json(drop_id=True))

    folium.GeoJson(
        data=gj,
        style_function=lambda x: {"fillColor": "#C8E6FF", "color": "#1B4D8C", "weight": 2.5, "fillOpacity": 0.35},
        highlight_function=lambda x: {"weight": 4, "color": "#FF4B4B"},
        tooltip=folium.features.GeoJsonTooltip(fields=tooltip_fields, aliases=aliases, sticky=True),
        name="AC (dissolved)"
    ).add_to(m)

# Fit bounds
try:
    if level == "Assembly Constituency":
        bounds = ac_gdf.total_bounds
    else:
        bounds = merged.total_bounds
    m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])
except Exception:
    pass

folium.LayerControl(collapsed=False).add_to(m)
st_folium(m, width=1150, height=680)

# ===== Summary (optional)
with st.expander("Show current aggregation summary"):
    if level == "Assembly Constituency":
        st.dataframe(ac_stats.sort_values(group_key))
    else:
        show_cols = [CSV_KEY, VILLAGE_FIELD, "subdistrict", "district", AC_FIELD, DUMMY_AC_FIELD] + PARTY_SHARE_COLS + CASTE_COLS
        show_cols = [c for c in show_cols if c in merged.columns]
        st.dataframe(merged[show_cols].head(200))


